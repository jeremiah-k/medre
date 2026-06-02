"""Cancellation depth accounting, drain helper, and catastrophic CE tests.

Split from ``test_startup_cleanup_stop_supervision.py``.  Covers:

- ``_drain_pending_cancellations`` helper unit tests.
- Per-adapter start-failure cleanup drain accounting.
- External startup cancellation + retry_worker CE preserves cancellation depth.
- Local-drain-count gap proof (adapter cleanup drain survives core CE).
- Drain+restore integration in a real MedreApp.stop() call.
- Catastrophic CancelledError during the start() adapter loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio

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
from medre.runtime.app import RuntimeState, _drain_pending_cancellations
from medre.runtime.errors import RuntimeStartupError
from tests.helpers.startup_cleanup import (
    CancelledStopOnStartFailure,
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
    _make_cancel_retry_worker,
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
# _drain_pending_cancellations helper tests
# ===================================================================


class TestDrainPendingCancellations:
    """Verify the cancellation-drain helper restores the exact count."""

    @pytest.mark.asyncio
    async def test_drain_restores_exact_count(self) -> None:
        """Cancelling a task N times, then draining, removes exactly N
        pending cancellation requests.  Restoring N cancels makes the
        next await raise CancelledError again."""
        N = 3
        drain_count: int = 0

        async def _target() -> None:
            nonlocal drain_count
            # Let the task start before the outer cancels arrive.
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                # CancelledError caught; cancelling() still reports N
                # (the framework does not auto-decrement).
                drain_count = _drain_pending_cancellations()
                current = asyncio.current_task()
                assert current is not None
                for _ in range(drain_count):
                    current.cancel()
                # The next await should raise CancelledError again.
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    return  # expected
            raise AssertionError("Should not reach here")

        task = asyncio.create_task(_target())
        # Let the task start before cancelling.
        await asyncio.sleep(0)
        # Cancel N times.
        for _ in range(N):
            task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        assert drain_count == N, f"Expected drain count {N}, got {drain_count}"

    @pytest.mark.asyncio
    async def test_drain_returns_zero_when_no_pending_cancels(self) -> None:
        """The helper returns 0 when called inside a task with no pending
        cancellations.  (The current_task() is None branch requires a
        non-async call and is not exercised here.)"""
        result = _drain_pending_cancellations()
        assert result == 0

    @pytest.mark.asyncio
    async def test_single_cancel_drain_restore_roundtrip(self) -> None:
        """A single cancel/drain/restore cycle preserves CancelledError
        propagation to the next await.

        The cancellation is caught inside ``_target`` via an explicit
        ``try / except CancelledError`` around a long sleep so that
        ``_drain_pending_cancellations()`` is guaranteed to be reached
        after the cancel is latched, not bypassed.
        """

        started = asyncio.Event()

        async def _target() -> None:
            # Signal that the task has started before any cancel arrives.
            started.set()
            # Await inside try so the external cancel() is caught
            # *within* _target, guaranteeing execution reaches
            # _drain_pending_cancellations().
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass  # caught inside _target
            # Now drain the cancellation (there should be exactly 1).
            count = _drain_pending_cancellations()
            assert count == 1
            # Restore it so the cancellation propagates to the caller.
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            # The next await should raise CancelledError.
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                return  # expected path
            raise AssertionError("Expected CancelledError after restore")

        task = asyncio.create_task(_target())
        # Wait until the task has actually started before cancelling.
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # CancelledError propagated as expected


# ===================================================================
# Per-adapter start-failure cleanup drain accounting
# ===================================================================


class TestPerAdapterStartFailureCleanupDrainAccounting:
    """Regression tests for cancellation-accounting during per-adapter
    start-failure cleanup in ``start()``.

    The bug: ``_drain_pending_cancellations()`` was called in the
    per-adapter cleanup ``except asyncio.CancelledError`` branch but the
    returned count was discarded, silently losing external cancellation
    requests.

    The fix: accumulate the drained count in ``_startup_cleanup_drained``
    and include it in the outer CancelledError handler's restore.
    """

    @pytest.mark.asyncio
    async def test_cleanup_drain_no_outer_cancel_non_propagating_path(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter fails to start (normal Exception); its cleanup stop
        receives an external CancelledError.  The per-adapter cleanup
        catches and drains the cancellation.  No further cancellation
        arrives, so the startup loop exits normally and raises
        RuntimeStartupError (total failure), NOT CancelledError.

        This is the intentional non-propagating start-failure path: the
        caller's cancellation intent was to abort startup, which already
        happened (adapter start failed).  The drain is required so
        subsequent adapter stops in the loop can release resources.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Adapter that fails start and has a slow stop (allows external
        # cancel to arrive during _stop_adapter_with_deadline polling).
        alpha = CancelledStopOnStartFailure(adapter_id="fake_matrix")
        app.adapters["fake_matrix"] = alpha

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # The adapter's stop() raises CancelledError directly (no
        # external cancel needed), which is caught by the per-adapter
        # cleanup handler.  The loop exits, total failure is classified,
        # and RuntimeStartupError is raised.
        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert alpha.stop_called
        assert pipeline_called[0], "pipeline_runner.stop() did not complete"
        assert storage_called[0], "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cleanup_drain_count_included_in_outer_cancel_restore(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Two adapters: alpha fails to start, external cancel arrives
        during its cleanup stop (per-adapter drain).  Beta then blocks in
        start(), and a second external cancel triggers the outer
        CancelledError handler.  The handler must include the per-adapter
        cleanup drain count in its restore.

        Without the fix, the per-adapter drain count would be discarded,
        and only the outer handler's own drain would be restored — losing
        one cancellation request.
        """

        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Alpha: start raises RuntimeError, stop is slow (allows external
        # cancel to arrive during _stop_adapter_with_deadline polling).
        class _StartFailSlowStop(AdapterContract):
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
                # Yield long enough for external cancel to arrive
                # during _stop_adapter_with_deadline's polling loop.
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise

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

        # Beta: start blocks on an event (allows second external cancel
        # to arrive during the adapter start call).
        beta_entered = asyncio.Event()

        class _BlockingStart(AdapterContract):
            adapter_id: str = "beta"
            platform: str = "test"
            role: AdapterRole = AdapterRole.TRANSPORT

            def __init__(self, adapter_id: str) -> None:
                self.adapter_id = adapter_id

            async def start(self, ctx: AdapterContext) -> None:
                beta_entered.set()
                # Block until cancelled — the second external cancel
                # triggers CancelledError here.
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise

            async def stop(self, timeout: float = 5.0) -> None:
                pass

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

        app.adapters["alpha"] = _StartFailSlowStop(adapter_id="alpha")
        app.adapters["beta"] = _BlockingStart(adapter_id="beta")

        # Track cleanup.
        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # Short timeout so the slow stop in _stop_adapter_with_deadline
        # doesn't delay the test too much.
        with _set_shutdown_timeout(app, 0.2):

            async def _run_with_double_cancel() -> None:
                start_task = asyncio.create_task(app.start())
                # Let start() reach alpha's start (which fails immediately)
                # and then the cleanup stop (which is slow).
                await asyncio.sleep(0.05)
                # First cancel: arrives during alpha's cleanup stop.
                start_task.cancel()
                # Wait for beta's start to be reached (after alpha's
                # cleanup finishes and the drain is consumed).
                await beta_entered.wait()
                # Second cancel: arrives during beta's start(), triggering
                # the outer CancelledError handler.
                start_task.cancel()
                try:
                    await start_task
                except asyncio.CancelledError:
                    pass  # expected

            await _run_with_double_cancel()

        # Cleanup MUST have run despite double cancellation.
        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


# ===================================================================
# External startup cancellation + retry worker CE depth preservation
# ===================================================================


class TestStartupCleanupRestoreOnCoreResourcesCancel:
    """Verify that external cancellation + retry_worker CE preserves
    cancellation depth, and adapter drain count survives core cleanup CE."""

    @pytest.mark.asyncio
    async def test_outer_drain_restored_when_core_resources_re_raises_ce(
        self, tmp_paths: MedrePaths
    ) -> None:
        """External cancel + retry_worker CE preserves cancellation depth."""

        N = 2  # number of external cancels to verify round-trip
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        adapter_entered = asyncio.Event()

        async def _blocking_start(ctx: AdapterContext) -> None:
            adapter_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        object.__setattr__(app.adapters["fake_matrix"], "start", _blocking_start)

        worker = _make_cancel_retry_worker(
            "simulated cancel from retry worker stop in startup cleanup"
        )
        app._retry_worker = worker

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        restored_depth: int = 0
        propagated_message = ""

        async def _inner_target() -> None:
            nonlocal propagated_message, restored_depth
            try:
                await app.start()
            except asyncio.CancelledError as exc:
                propagated_message = str(exc)
                task = asyncio.current_task()
                assert task is not None
                restored_depth = task.cancelling()
                while task.cancelling() > 0:
                    task.uncancel()
                return
            raise AssertionError("start() should have raised CancelledError")

        start_task = asyncio.create_task(_inner_target())

        await adapter_entered.wait()
        for _ in range(N):
            start_task.cancel("external startup cancel")

        try:
            await start_task
        except asyncio.CancelledError:
            pass

        assert propagated_message == "external startup cancel"
        assert (
            restored_depth == N
        ), f"Expected {N} restored cancellations, got {restored_depth}"
        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_start_failure_cleanup_returns_adapter_drain_when_core_raises_ce(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Adapter drain count survives a later core cleanup CE.

        This is the local-drain-count gap proof: when
        ``_cleanup_started_adapters`` returns a drain count and
        ``_cleanup_core_resources`` subsequently raises CancelledError,
        ``_start_failure_cleanup`` catches the CE, drains the restored
        requests from core cleanup, folds them into the adapter drain
        count, and returns the combined total.  No count is lost.
        """

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        async def _cleanup_started_adapters() -> int:
            return 1

        async def _cleanup_core_resources() -> None:
            raise asyncio.CancelledError("core cleanup cancel")

        object.__setattr__(app, "_cleanup_started_adapters", _cleanup_started_adapters)
        object.__setattr__(app, "_cleanup_core_resources", _cleanup_core_resources)
        drained = await app._start_failure_cleanup()

        assert drained == 1
        assert app.state == RuntimeState.FAILED


# ===================================================================
# Drain+restore integration in MedreApp.stop()
# ===================================================================


class TestDrainRestoreIntegration:
    """Integration test: drain+restore cycle in a real MedreApp.stop() call.

    The existing TestRetryWorkerCancelledErrorDuringStop verifies CE
    deferral, but the retry_worker.stop() raises CE in isolation (no
    external cancellation on the task).  This test exercises the full
    cycle: external task cancellation + CE from retry_worker.stop() +
    drain + restore.
    """

    @pytest.mark.asyncio
    async def test_external_cancel_with_retry_worker_ce_full_drain_restore(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Externally cancel the stop() task while retry_worker.stop()
        raises CE.  Verify drain happens, pipeline/storage cleanup runs,
        cancellation count is restored, and CE propagates."""

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        app._set_state(RuntimeState.RUNNING)

        # Slow cancel retry worker so external cancel has a window.
        worker = _make_cancel_retry_worker(
            "simulated cancel from retry worker stop", slow=True
        )
        app._retry_worker = worker

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        async def _run_and_cancel() -> None:
            stop_task = asyncio.create_task(app.stop())
            # Let stop() start and reach the retry worker await.
            await asyncio.sleep(0)
            # Cancel externally to add a pending cancellation request.
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass

        await _run_and_cancel()

        # Cleanup MUST have run.
        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


# ===================================================================
# Catastrophic CancelledError during start() adapter loop
# ===================================================================


class TestStartCatastrophicCancelledError:
    """Verify that CancelledError arriving during the start() adapter
    loop triggers the same cleanup as a regular Exception."""

    @pytest.mark.asyncio
    async def test_cancelled_during_startup_loop_runs_cleanup(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Externally cancel the task running start() during the adapter
        loop.  The CE handler must run cleanup_started_adapters,
        cleanup_core_resources, set state to FAILED, then re-raise."""

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        # Build a fake adapter that lets the cancellation arrive during
        # its start() coroutine.
        adapter_entered = asyncio.Event()

        class _AdaptersThatWait:
            adapter_id = "blocking"
            platform = "test"
            role = AdapterRole.TRANSPORT

            def __init__(self) -> None:
                self._start_event = asyncio.Event()

            async def start(self, ctx: AdapterContext) -> None:
                # Signal that start() has been reached so the test
                # knows it is safe to cancel.
                adapter_entered.set()
                # Wait until cancelled.  This ensures CE arrives during
                # the loop body, not before it.
                try:
                    await self._start_event.wait()
                except asyncio.CancelledError:
                    raise
                raise AssertionError("should not reach here")

            async def stop(self, timeout: float = 5.0) -> None:
                pass

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

        blocking = _AdaptersThatWait()
        app.adapters["fake_matrix"] = blocking
        # pre-populate started_adapter_ids so cleanup has something to do
        app.started_adapter_ids.append("fake_matrix")
        app._adapter_states["fake_matrix"] = AdapterState.INITIALIZING

        async def _run_and_cancel() -> None:
            start_task = asyncio.create_task(app.start())
            # Wait until start() has reached the adapter's start()
            # coroutine — guarantees CE arrives during the loop body,
            # not during storage/pipeline initialization.
            await adapter_entered.wait()
            start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                pass

        await _run_and_cancel()

        # Cleanup MUST have run despite CE.
        assert pipeline_called[0], "pipeline_runner.stop() was skipped"
        assert storage_called[0], "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED
