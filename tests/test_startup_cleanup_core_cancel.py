"""Core resource cleanup CancelledError and retry worker stop supervision.

Split from ``test_startup_cleanup_stop_supervision.py``.  Covers:

- CancelledError from retry_worker.stop() during _cleanup_core_resources
  does NOT skip pipeline_runner.stop() or storage.close().
- CancelledError from retry_worker.stop() during MedreApp.stop() cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from tests.helpers.startup_cleanup import (
    FailingAdapter,
    _build_app,
    _config_with_one_fake_adapter,
    _make_cancel_retry_worker,
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
# Retry worker CancelledError during stop()
# ===================================================================


class TestRetryWorkerCancelledErrorDuringStop:
    """Verify that a CancelledError raised by the retry worker's stop()
    does NOT skip pipeline_runner.stop() or storage.close().

    The retry worker stop is in Phase 1 of ``MedreApp.stop()``.  Before
    the fix, ``asyncio.CancelledError`` (a BaseException, not caught by
    ``except Exception``) would unwind the entire ``stop()`` method,
    bypassing pipeline runner and storage cleanup.
    """

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_stop_does_not_skip_cleanup(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When retry_worker.stop() raises CancelledError, the deferred
        cancellation path runs pipeline_runner.stop() and storage.close()
        before re-raising."""
        import asyncio

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Bring the app to a state where stop() will proceed past the
        # early-return guard.  We set STOPPING directly to avoid the
        # full startup lifecycle.
        app._set_state(RuntimeState.RUNNING)

        worker = _make_cancel_retry_worker("simulated cancel from retry worker stop")
        app._retry_worker = worker

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # stop() should re-raise the CancelledError after cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app.stop()

        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


# ===================================================================
# _cleanup_core_resources CancelledError tests
# ===================================================================


class TestCleanupCoreResourcesCancelledError:
    """Verify that CancelledError from retry_worker.stop() during
    _cleanup_core_resources does NOT skip pipeline_runner.stop() or
    storage.close()."""

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_stop_runs_pipeline_and_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Direct invocation of _cleanup_core_resources with a stub worker
        that raises CE must still run pipeline_runner.stop() and
        storage.close() and then re-raise the CE."""
        import asyncio

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        # Bypass the full startup lifecycle: we want to test
        # _cleanup_core_resources in isolation.
        app._set_state(RuntimeState.STARTING)

        worker = _make_cancel_retry_worker("simulated cancel from retry worker stop")
        app._retry_worker = worker

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # The cleanup should re-raise the CE after pipeline/storage cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app._cleanup_core_resources()

        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_end_to_end_via_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Full start() flow: one adapter that fails to start, plus a retry
        worker that raises CE on stop.  Pipeline and storage cleanup must
        still run; CE re-raises from start()."""
        import asyncio

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Adapter that fails on start with a regular Exception.
        app.adapters["fake_matrix"] = FailingAdapter(adapter_id="fake_matrix")

        worker = _make_cancel_retry_worker("simulated cancel from retry worker stop")
        app._retry_worker = worker

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # start() should re-raise the CE from cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app.start()

        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_no_pending_cancel_no_drain(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When retry_worker.stop() raises CE but the task has no pending
        cancellation, _drain_pending_cancellations returns 0 and the CE
        still re-raises.  Verifies the no-drain case doesn't break."""
        import asyncio

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        app._set_state(RuntimeState.STARTING)

        worker = _make_cancel_retry_worker("no-drain case")
        app._retry_worker = worker

        # Pipeline stop should still run even with no pending cancellation.
        pipeline_called = _make_tracking_pipeline_stop(app)

        with pytest.raises(asyncio.CancelledError, match="no-drain case"):
            await app._cleanup_core_resources()

        assert pipeline_called[0]
        assert app.state == RuntimeState.FAILED
