"""CancelledError deferral in MedreApp.stop() pipeline/storage cleanup.

Covers four ``except asyncio.CancelledError`` blocks that defer cancellation
so pipeline runner stop and storage close always run:

1. Pipeline runner stop CE (app.py lines 1271-1275)
2. Storage close CE (app.py lines 1285-1289)
3. Drain loop CE (app.py lines 1123-1128)
4. Persist drain abandoned CE (app.py lines 1141-1147)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_one_fake_adapter,
    _make_tracking_pipeline_stop,
    _make_tracking_storage_close,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ===================================================================
# Test 1: Pipeline runner CE in stop()
# ===================================================================


class TestPipelineRunnerCancelledError:
    """When ``pipeline_runner.stop()`` raises CancelledError, the CE is
    deferred, storage.close() still runs, and the CE is re-raised."""

    @pytest.mark.asyncio
    async def test_pipeline_runner_cancel_defers_and_still_closes_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.state == RuntimeState.RUNNING

        # Replace pipeline_runner.stop with one that raises CancelledError.
        original_pipeline_stop = app.pipeline_runner.stop

        async def _cancel_pipeline_stop() -> None:
            raise asyncio.CancelledError("pipeline cancel")

        app.pipeline_runner.stop = _cancel_pipeline_stop  # type: ignore[assignment]

        storage_called = _make_tracking_storage_close(app)

        with pytest.raises(asyncio.CancelledError, match="pipeline cancel"):
            await app.stop()

        assert storage_called[0], "storage.close() was skipped despite pipeline CE"
        assert app.state == RuntimeState.FAILED

        # Restore so teardown doesn't break.
        app.pipeline_runner.stop = original_pipeline_stop  # type: ignore[assignment]
        # Ensure aiosqlite connection is fully cleaned up.
        if app.storage is not None and not app.storage._closed:
            await app.storage.close()
        await asyncio.sleep(0)


# ===================================================================
# Test 2: Storage close CE in stop()
# ===================================================================


class TestStorageCloseCancelledError:
    """When ``storage.close()`` raises CancelledError, the CE is deferred
    and re-raised after cleanup."""

    @pytest.mark.asyncio
    async def test_storage_close_cancel_defers_and_reraises(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.state == RuntimeState.RUNNING

        pipeline_called = _make_tracking_pipeline_stop(app)

        # Replace storage.close with one that raises CancelledError.
        assert app.storage is not None
        original_storage_close = app.storage.close

        async def _cancel_storage_close() -> None:
            raise asyncio.CancelledError("storage cancel")

        app.storage.close = _cancel_storage_close  # type: ignore[assignment]

        with pytest.raises(asyncio.CancelledError, match="storage cancel"):
            await app.stop()

        assert pipeline_called[
            0
        ], "pipeline_runner.stop() was skipped despite storage CE"
        assert app.state == RuntimeState.FAILED

        # Restore and clean up the unclosed storage connection.
        app.storage.close = original_storage_close  # type: ignore[assignment]
        if not app.storage._closed:
            await app.storage.close()
        await asyncio.sleep(0)


# ===================================================================
# Test 3: Drain loop CE in stop()
# ===================================================================


class TestDrainLoopCancelledError:
    """When external cancellation arrives during the drain loop's
    ``asyncio.sleep(0.1)``, the CE is caught, drain count accumulated,
    and the loop breaks — cleanup still runs."""

    @pytest.mark.asyncio
    async def test_drain_loop_cancel_breaks_and_cleanup_still_runs(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.state == RuntimeState.RUNNING

        # Mock capacity controller reporting in-flight work so drain loop enters.
        cc = MagicMock()
        cc.snapshot.return_value = {"delivery_current": 1, "replay_current": 0}
        cc.accepting_work = False
        app._capacity_controller = cc

        # Short drain window.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(app.config.limits, "shutdown_drain_timeout_seconds", 1)

        pipeline_called = _make_tracking_pipeline_stop(app)
        # Do NOT wrap storage.close — the real close must complete so
        # the aiosqlite connection is cleaned up without ResourceWarning.
        assert app.storage is not None

        try:
            # Run stop() in a task, then cancel it once it's inside the drain loop.
            stop_task = asyncio.create_task(app.stop())

            # Give stop() enough time to enter the drain loop and hit sleep(0.1).
            await asyncio.sleep(0.3)
            stop_task.cancel()

            # The task should finish (CE is deferred internally) or raise
            # CancelledError depending on re-raise timing.
            try:
                await stop_task
            except asyncio.CancelledError:
                pass  # Expected — deferred CE is re-raised.

            assert pipeline_called[
                0
            ], "pipeline_runner.stop() skipped after drain loop CE"
            assert app.storage._closed, "storage.close() did not complete"
            assert app.state == RuntimeState.FAILED
        finally:
            object.__setattr__(
                app.config.limits, "shutdown_drain_timeout_seconds", original_drain
            )
            # Safety net: ensure aiosqlite connection is closed.
            if app.storage is not None and not app.storage._closed:
                await app.storage.close()
            # Give the event loop a chance to process aiosqlite's internal
            # cleanup callbacks so ResourceWarning is not emitted.
            await asyncio.sleep(0)


# ===================================================================
# Test 4: Persist drain abandoned CE
# ===================================================================


class TestPersistDrainAbandonedCancelledError:
    """When external cancellation arrives during
    ``_persist_drain_abandoned_evidence()``, the CE is caught and deferred."""

    @pytest.mark.asyncio
    async def test_persist_drain_abandoned_cancel_defers(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.state == RuntimeState.RUNNING

        # Mock capacity controller with in-flight work so drain times out.
        cc = MagicMock()
        cc.snapshot.return_value = {"delivery_current": 1, "replay_current": 0}
        cc.accepting_work = False
        app._capacity_controller = cc

        # Very short drain timeout so drain times out immediately.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(app.config.limits, "shutdown_drain_timeout_seconds", 1)

        # Mock _persist_drain_abandoned_evidence to raise CE.
        async def _cancel_on_persist() -> None:
            raise asyncio.CancelledError("persist drain abandoned cancel")

        app._persist_drain_abandoned_evidence = _cancel_on_persist  # type: ignore[assignment]

        # Mock drain_abandoned_deliveries to return a non-empty list so
        # _persist_drain_abandoned_evidence is actually called.
        mock_inflight = MagicMock()
        mock_inflight.event_id = "evt-1"
        mock_inflight.delivery_plan_id = "plan-1"
        mock_inflight.target_adapter = "fake"
        mock_inflight.target_channel = "ch-1"
        mock_inflight.route_id = "route-1"
        mock_inflight.source = "src"
        mock_inflight.replay_run_id = None

        app.pipeline_runner.drain_abandoned_deliveries = (  # type: ignore[assignment]
            lambda: [mock_inflight]
        )

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        try:
            # stop() should re-raise the deferred CE.
            with pytest.raises(
                asyncio.CancelledError, match="persist drain abandoned cancel"
            ):
                await app.stop()

            assert pipeline_called[0], "pipeline_runner.stop() skipped after persist CE"
            assert storage_called[0], "storage.close() skipped after persist CE"
            assert app.state == RuntimeState.FAILED
        finally:
            object.__setattr__(
                app.config.limits, "shutdown_drain_timeout_seconds", original_drain
            )
            if app.storage is not None and not app.storage._closed:
                await app.storage.close()
            # Give the event loop a chance to process aiosqlite's internal
            # cleanup callbacks so ResourceWarning is not emitted.
            await asyncio.sleep(0)
