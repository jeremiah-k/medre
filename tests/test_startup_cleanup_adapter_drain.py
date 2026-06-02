"""Direct _cleanup_started_adapters unit tests and drain-site verification.

Split from ``test_startup_cleanup_stop_supervision.py``.  Covers started-
adapter and never-started-adapter timeout/cancel paths through
``_cleanup_started_adapters``, plus integration verification that the
cancellation drain allows subsequent adapter stops to run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.core.lifecycle.states import AdapterState
from medre.runtime.app import RuntimeState
from tests.helpers.startup_cleanup import (
    CancelledStopDouble,
    SlowStopDouble,
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
    _make_tracking_pipeline_stop,
    _make_tracking_storage_close,
    _set_shutdown_timeout,
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
# Direct _cleanup_started_adapters unit tests
# ===================================================================


class TestCleanupStartedAdaptersDirect:
    """Directly exercise _cleanup_started_adapters to cover started-adapter
    and never-started-adapter timeout/cancel paths that are unreachable
    through the normal TOTAL_FAILURE start() flow (because TOTAL_FAILURE
    implies started_adapter_ids is empty)."""

    @pytest.mark.asyncio
    async def test_started_adapter_timeout(self, tmp_paths: MedrePaths) -> None:
        """A started adapter that times out during _cleanup_started_adapters
        is marked FAILED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        slow = SlowStopDouble(adapter_id="slow_started")
        app.adapters["slow_started"] = slow
        app.started_adapter_ids.append("slow_started")
        app._adapter_states["slow_started"] = AdapterState.READY

        with _set_shutdown_timeout(app, 0.2):
            await app._cleanup_started_adapters()

        assert slow.stop_called
        assert app._adapter_states["slow_started"] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_started_adapter_cancelled(self, tmp_paths: MedrePaths) -> None:
        """A started adapter whose stop() raises CancelledError during
        _cleanup_started_adapters is marked FAILED; CancelledError is
        suppressed so that _cleanup_core_resources always runs."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        cancelled = CancelledStopDouble(adapter_id="cancelled_started")
        app.adapters["cancelled_started"] = cancelled
        app.started_adapter_ids.append("cancelled_started")
        app._adapter_states["cancelled_started"] = AdapterState.READY

        await app._cleanup_started_adapters()

        assert cancelled.stop_called
        assert app._adapter_states["cancelled_started"] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_never_started_adapter_timeout(self, tmp_paths: MedrePaths) -> None:
        """A never-started adapter that times out during
        _cleanup_started_adapters is marked FAILED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        slow = SlowStopDouble(adapter_id="never_started_slow")
        app.adapters["never_started_slow"] = slow
        # NOT in started_adapter_ids — simulates built-but-never-started.
        app._adapter_states["never_started_slow"] = AdapterState.INITIALIZING

        with _set_shutdown_timeout(app, 0.2):
            await app._cleanup_started_adapters()

        assert slow.stop_called
        assert app._adapter_states["never_started_slow"] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_never_started_adapter_cancelled(self, tmp_paths: MedrePaths) -> None:
        """A never-started adapter whose stop() raises CancelledError during
        _cleanup_started_adapters is marked FAILED; CancelledError is
        suppressed so that _cleanup_core_resources always runs."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        cancelled = CancelledStopDouble(adapter_id="never_started_cancel")
        app.adapters["never_started_cancel"] = cancelled
        # NOT in started_adapter_ids.
        app._adapter_states["never_started_cancel"] = AdapterState.INITIALIZING

        await app._cleanup_started_adapters()

        assert cancelled.stop_called
        assert app._adapter_states["never_started_cancel"] is AdapterState.FAILED


# ===================================================================
# Startup cleanup drain-site tests
# ===================================================================


class TestStartupCleanupDrainSites:
    """Verify that _drain_pending_cancellations in _cleanup_started_adapters
    allows subsequent adapter stops in the loop to actually run."""

    @pytest.mark.asyncio
    async def test_external_cancel_during_first_adapter_stop_allows_second(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Two adapters that have started.  External CE arrives during the
        first adapter's cleanup stop.  The second adapter's stop must
        still run because the cancellation state is drained."""

        class _StopOnCancel(AdapterContract):
            """stop() that yields to the event loop, allowing external
            cancellation to arrive."""

            adapter_id: str = "stop_on_cancel"
            platform: str = "test"
            role: AdapterRole = AdapterRole.TRANSPORT

            def __init__(self, adapter_id: str) -> None:
                self.adapter_id = adapter_id
                self.stop_called = False

            async def start(self, ctx: AdapterContext) -> None:
                pass

            async def stop(self, timeout: float = 5.0) -> None:
                self.stop_called = True
                # Yield long enough for an external cancel to land here.
                try:
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise

            async def health_check(self) -> AdapterInfo:
                return AdapterInfo(
                    adapter_id=self.adapter_id,
                    platform=self.platform,
                    role=self.role,
                    version="0.0.0",
                    capabilities=AdapterCapabilities(),
                    health="ok",
                )

            async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
                return None

        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        alpha = _StopOnCancel(adapter_id="alpha")
        beta = _StopOnCancel(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta
        app.started_adapter_ids.extend(["alpha", "beta"])
        for aid in ("alpha", "beta"):
            app._adapter_states[aid] = AdapterState.READY

        # Cancel the cleanup task externally while it's processing the
        # first adapter.  Wrap in a helper so we can use pytest.raises.
        async def _run_with_external_cancel() -> None:
            task = asyncio.create_task(app._cleanup_started_adapters())
            await asyncio.sleep(0)
            task.cancel()
            # _cleanup_started_adapters suppresses CancelledError (best-effort
            # cleanup), so the task should complete normally.
            await task

        await _run_with_external_cancel()

        # Both adapters should have had stop() called, even though CE
        # arrived during the first one's stop.
        assert alpha.stop_called, "alpha.stop() was not called"
        assert beta.stop_called, "beta.stop() was not called"


# ===================================================================
# _startup_cleanup_drained accumulation tests
# ===================================================================


class TestStartupCleanupDrainAccounting:
    """Verify that _startup_cleanup_drained is correctly accumulated
    when external cancellation arrives during per-adapter start-failure
    cleanup stops, and that the outer CancelledError handler restores
    the full cancellation depth."""

    @pytest.mark.asyncio
    async def test_per_adapter_cleanup_drain_count_included_in_outer_cancel(
        self, tmp_paths: MedrePaths
    ) -> None:
        """External cancel during per-adapter start-failure cleanup stop
        is accumulated in _startup_cleanup_drained.  A second cancel
        triggers the outer CancelledError handler which restores the
        full count."""

        # -- Alpha: start() fails, stop() yields -------------------------
        class _FailingYieldingStop(AdapterContract):
            adapter_id: str = "alpha"
            platform: str = "test"
            role: AdapterRole = AdapterRole.TRANSPORT

            def __init__(self, adapter_id: str) -> None:
                self.adapter_id = adapter_id
                self.stop_called = False

            async def start(self, ctx: AdapterContext) -> None:
                raise RuntimeError(f"Start failure: {self.adapter_id}")

            async def stop(self, timeout: float = 5.0) -> None:
                self.stop_called = True
                # Yield to allow external cancel to arrive.
                await asyncio.sleep(0.5)

            async def health_check(self) -> AdapterInfo:
                return AdapterInfo(
                    adapter_id=self.adapter_id,
                    platform=self.platform,
                    role=self.role,
                    version="0.0.0",
                    capabilities=AdapterCapabilities(),
                    health="failed",
                )

            async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
                return None

        # -- Beta: start() blocks until cancelled ------------------------
        class _BlockingStart(AdapterContract):
            adapter_id: str = "beta"
            platform: str = "test"
            role: AdapterRole = AdapterRole.TRANSPORT

            def __init__(self, adapter_id: str) -> None:
                self.adapter_id = adapter_id
                self.started = asyncio.Event()
                self.stop_called = False

            async def start(self, ctx: AdapterContext) -> None:
                self.started.set()
                # Block forever until cancelled.
                await asyncio.Event().wait()

            async def stop(self, timeout: float = 5.0) -> None:
                self.stop_called = True

            async def health_check(self) -> AdapterInfo:
                return AdapterInfo(
                    adapter_id=self.adapter_id,
                    platform=self.platform,
                    role=self.role,
                    version="0.0.0",
                    capabilities=AdapterCapabilities(),
                    health="ok",
                )

            async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
                return None

        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        alpha = _FailingYieldingStop(adapter_id="alpha")
        beta = _BlockingStart(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        pipeline_stop_called = _make_tracking_pipeline_stop(app)
        storage_close_called = _make_tracking_storage_close(app)

        with _set_shutdown_timeout(app, 0.2):

            async def _run() -> None:
                start_task = asyncio.create_task(app.start())
                await asyncio.sleep(0.05)  # let start() reach alpha
                start_task.cancel()  # first cancel — during alpha's cleanup stop
                await beta.started.wait()  # wait for beta's start to be reached
                start_task.cancel()  # second cancel — triggers outer CE handler
                try:
                    await start_task
                except asyncio.CancelledError:
                    pass

            await _run()

        assert pipeline_stop_called[0], "pipeline_runner.stop() was not called"
        assert storage_close_called[0], "storage.close() was not called"
        assert app.state is RuntimeState.FAILED
