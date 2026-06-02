"""Startup-failure cleanup stop-timeout supervision tests.

Focused split from ``test_startup_build_failure_and_cleanup.py`` so that
module stays under the CI 1500-line limit.  These tests cover only the
scenario where an adapter's ``stop()`` hangs or is cancelled during
startup-failure cleanup — verifying that pipeline runner stop and storage
close still proceed regardless.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
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
from medre.runtime.app import MedreApp, RuntimeState, _drain_pending_cancellations
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _config_with_one_fake_adapter() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-startup-cleanup-stop-supervision"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
        ),
    )


def _config_with_two_fake_adapters() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-startup-cleanup-stop-supervision-two"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "alpha": MatrixRuntimeConfig(
                    adapter_id="alpha",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
                "beta": MatrixRuntimeConfig(
                    adapter_id="beta",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
            },
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    return RuntimeBuilder(config, paths).build()


# ---------------------------------------------------------------------------
# Adapter doubles
# ---------------------------------------------------------------------------


class _SlowStopOnStartFailure(AdapterContract):
    """Fails on start(); stop() sleeps longer than any reasonable timeout.

    Verifies that the runtime's wait_for cuts a hung adapter.stop() short
    during startup failure cleanup so that pipeline runner stop and storage
    close still proceed.
    """

    adapter_id: str = "slow_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "slow_stop") -> None:
        self.adapter_id = adapter_id
        self.stop_called = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Start failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True
        await asyncio.sleep(300.0)

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


class _CancelledStopOnStartFailure(AdapterContract):
    """Fails on start(); stop() raises CancelledError.

    Verifies that CancelledError during startup cleanup stop is caught
    and does not prevent pipeline/storage cleanup.
    """

    adapter_id: str = "cancelled_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "cancelled_stop") -> None:
        self.adapter_id = adapter_id
        self.stop_called = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Start failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True
        raise asyncio.CancelledError("simulated cancel during startup cleanup stop")

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


class _FailingAdapter(AdapterContract):
    """Adapter that raises on start()."""

    adapter_id: str = "failing_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_adapter") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated adapter failure: {self.adapter_id}")

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


# ===================================================================
# Startup cleanup stop-timeout supervision
# ===================================================================


class TestStartupCleanupStopTimeout:
    """Startup failure cleanup uses wait_for so slow/hung adapter stops
    don't block pipeline runner stop or storage close."""

    @pytest.mark.asyncio
    async def test_slow_adapter_stop_during_startup_cleanup_does_not_block_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A slow-stopping adapter during startup failure cleanup doesn't
        prevent storage close."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace adapter with one that fails start and sleeps forever on stop.
        app.adapters["fake_matrix"] = _SlowStopOnStartFailure(adapter_id="fake_matrix")

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        # Short timeout so the slow stop gets cut short quickly.
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        # Adapter stop should have been called.
        assert app.adapters["fake_matrix"].stop_called

        # Storage MUST have been closed despite the slow adapter.
        assert storage_close_called, "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_slow_adapter_stop_during_startup_cleanup_does_not_block_pipeline(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Pipeline runner is stopped even when adapter stop times out during
        startup failure cleanup."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = _SlowStopOnStartFailure(adapter_id="fake_matrix")

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert pipeline_stop_called, "pipeline_runner.stop() did not complete"

    @pytest.mark.asyncio
    async def test_cancelled_error_during_startup_cleanup_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """CancelledError from adapter stop during startup best-effort
        cleanup (after start failure) is caught and suppressed (pattern C);
        pipeline and storage cleanup proceed.  The CancelledError is
        raised in the per-adapter best-effort stop in ``start()``, NOT
        in ``_cleanup_started_adapters()``.  The cancellation state is
        drained so subsequent adapter stops in the loop can still run."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = _CancelledStopOnStartFailure(
            adapter_id="fake_matrix"
        )

        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.adapters["fake_matrix"].stop_called
        assert pipeline_stop_called, "pipeline_runner.stop() did not complete"
        assert storage_close_called, "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_multiple_slow_adapters_all_attempted(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Multiple slow adapters during startup cleanup: all are attempted,
        pipeline/storage cleanup still happens."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Both adapters fail to start and have slow stops.
        alpha = _SlowStopOnStartFailure(adapter_id="alpha")
        beta = _SlowStopOnStartFailure(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        storage_close_called = False
        assert app.storage is not None
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        # Both adapters should have had stop called.
        assert alpha.stop_called, "alpha stop() not called"
        assert beta.stop_called, "beta stop() not called"

        # Storage close must still happen.
        assert storage_close_called, "storage.close() did not complete"

    @pytest.mark.asyncio
    async def test_mixed_slow_and_fast_adapters_during_startup_cleanup(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One slow + one fast adapter during startup cleanup: both attempted,
        pipeline/storage still cleaned up."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Alpha: slow stop, start fails.
        alpha = _SlowStopOnStartFailure(adapter_id="alpha")
        # Beta: fast stop (FailingAdapter), start fails.
        beta = _FailingAdapter(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        storage_close_called = False
        assert app.storage is not None
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert alpha.stop_called
        assert storage_close_called, "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED


# ===================================================================
# Direct _cleanup_started_adapters unit tests
# ===================================================================


class _SlowStopDouble:
    """Minimal adapter double whose stop() sleeps past any reasonable timeout."""

    def __init__(self, adapter_id: str = "slow", sleep_seconds: float = 300.0) -> None:
        self.adapter_id = adapter_id
        self.platform = "test"
        self.stop_called = False
        self._sleep = sleep_seconds

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        await asyncio.sleep(self._sleep)


class _CancelledStopDouble:
    """Minimal adapter double whose stop() raises CancelledError."""

    def __init__(self, adapter_id: str = "cancelled") -> None:
        self.adapter_id = adapter_id
        self.platform = "test"
        self.stop_called = False

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        raise asyncio.CancelledError("simulated cancel")


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

        slow = _SlowStopDouble(adapter_id="slow_started")
        app.adapters["slow_started"] = slow
        app.started_adapter_ids.append("slow_started")
        app._adapter_states["slow_started"] = AdapterState.READY

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)
        try:
            await app._cleanup_started_adapters()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert slow.stop_called
        assert app._adapter_states["slow_started"] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_started_adapter_cancelled(self, tmp_paths: MedrePaths) -> None:
        """A started adapter whose stop() raises CancelledError during
        _cleanup_started_adapters is marked FAILED; CancelledError is
        suppressed so that _cleanup_core_resources always runs."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        cancelled = _CancelledStopDouble(adapter_id="cancelled_started")
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

        slow = _SlowStopDouble(adapter_id="never_started_slow")
        app.adapters["never_started_slow"] = slow
        # NOT in started_adapter_ids — simulates built-but-never-started.
        app._adapter_states["never_started_slow"] = AdapterState.INITIALIZING

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)
        try:
            await app._cleanup_started_adapters()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert slow.stop_called
        assert app._adapter_states["never_started_slow"] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_never_started_adapter_cancelled(self, tmp_paths: MedrePaths) -> None:
        """A never-started adapter whose stop() raises CancelledError during
        _cleanup_started_adapters is marked FAILED; CancelledError is
        suppressed so that _cleanup_core_resources always runs."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        cancelled = _CancelledStopDouble(adapter_id="never_started_cancel")
        app.adapters["never_started_cancel"] = cancelled
        # NOT in started_adapter_ids.
        app._adapter_states["never_started_cancel"] = AdapterState.INITIALIZING

        await app._cleanup_started_adapters()

        assert cancelled.stop_called
        assert app._adapter_states["never_started_cancel"] is AdapterState.FAILED


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
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Bring the app to a state where stop() will proceed past the
        # early-return guard.  We set STOPPING directly to avoid the
        # full startup lifecycle.
        app._set_state(RuntimeState.RUNNING)

        # Stub retry worker whose stop() raises CancelledError.
        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _cancel_on_stop() -> None:
            raise asyncio.CancelledError("simulated cancel from retry worker stop")

        worker.stop = _cancel_on_stop
        app._retry_worker = worker

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        # stop() should re-raise the CancelledError after cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app.stop()

        assert pipeline_stop_called, "pipeline_runner.stop() was skipped"
        assert storage_close_called, "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


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
    async def test_drain_returns_zero_outside_task(self) -> None:
        """The helper returns 0 when called outside an asyncio task."""
        # _drain_pending_cancellations checks current_task().  Inside a
        # coroutine running as a task, current_task() is not None, so
        # we test that the function returns 0 when there are no pending
        # cancellations.
        result = _drain_pending_cancellations()
        assert result == 0

    @pytest.mark.asyncio
    async def test_single_cancel_drain_restore_roundtrip(self) -> None:
        """A single cancel/drain/restore cycle preserves CancelledError
        propagation to the next await."""
        started = asyncio.Event()

        async def _target() -> None:
            # Signal that the task has started before any cancel arrives.
            started.set()
            # Yield to the event loop so the caller's cancel() can be
            # delivered.  Without this yield, _drain_pending_cancellations
            # would run before the cancel request is latched.
            await asyncio.sleep(0)
            # Drain the cancellation (there should be 1).
            count = _drain_pending_cancellations()
            assert count == 1
            # Restore it.
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


# ---------------------------------------------------------------------------
# TestCleanupCoreResourcesCancelledError
# ---------------------------------------------------------------------------


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
        from medre.runtime.app import RuntimeState
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        # Bypass the full startup lifecycle: we want to test
        # _cleanup_core_resources in isolation.
        app._set_state(RuntimeState.STARTING)

        # Stub retry worker whose stop() raises CE.
        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _cancel_on_stop() -> None:
            raise asyncio.CancelledError("simulated cancel from retry worker stop")

        worker.stop = _cancel_on_stop
        app._retry_worker = worker

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        # The cleanup should re-raise the CE after pipeline/storage cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app._cleanup_core_resources()

        assert pipeline_stop_called, "pipeline_runner.stop() was skipped"
        assert storage_close_called, "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_end_to_end_via_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Full start() flow: one adapter that fails to start, plus a retry
        worker that raises CE on stop.  Pipeline and storage cleanup must
        still run; CE re-raises from start()."""
        from medre.runtime.app import RuntimeState
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Adapter that fails on start with a regular Exception.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")

        # Stub retry worker whose stop() raises CE.
        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _cancel_on_stop() -> None:
            raise asyncio.CancelledError("simulated cancel from retry worker stop")

        worker.stop = _cancel_on_stop
        app._retry_worker = worker

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

        # start() should re-raise the CE from cleanup.
        with pytest.raises(asyncio.CancelledError, match="simulated cancel"):
            await app.start()

        assert pipeline_stop_called, "pipeline_runner.stop() was skipped"
        assert storage_close_called, "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cancelled_retry_worker_no_pending_cancel_no_drain(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When retry_worker.stop() raises CE but the task has no pending
        cancellation, _drain_pending_cancellations returns 0 and the CE
        still re-raises.  Verifies the no-drain case doesn't break."""
        from medre.runtime.app import RuntimeState
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        app._set_state(RuntimeState.STARTING)

        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _cancel_on_stop() -> None:
            raise asyncio.CancelledError("no-drain case")

        worker.stop = _cancel_on_stop
        app._retry_worker = worker

        # Pipeline stop should still run even with no pending cancellation.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        with pytest.raises(asyncio.CancelledError, match="no-drain case"):
            await app._cleanup_core_resources()

        assert pipeline_stop_called
        assert app.state == RuntimeState.FAILED


# ---------------------------------------------------------------------------
# TestStartCatastrophicCancelledError
# ---------------------------------------------------------------------------


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
        from medre.runtime.app import RuntimeState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

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
        assert pipeline_stop_called, "pipeline_runner.stop() was skipped"
        assert storage_close_called, "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


# ---------------------------------------------------------------------------
# TestDrainRestoreIntegration
# ---------------------------------------------------------------------------


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
        from medre.runtime.app import RuntimeState
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        app._set_state(RuntimeState.RUNNING)

        # Stub retry worker that raises CE AND takes a moment to do so
        # (so we can cancel externally before stop() finishes).
        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _slow_cancel_on_stop() -> None:
            await asyncio.sleep(0.1)  # give outer code time to cancel us
            raise asyncio.CancelledError("simulated cancel from retry worker stop")

        worker.stop = _slow_cancel_on_stop
        app._retry_worker = worker

        # Track pipeline runner stop.
        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]

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
        assert pipeline_stop_called, "pipeline_runner.stop() was skipped"
        assert storage_close_called, "storage.close() was skipped"
        assert app.state == RuntimeState.FAILED


# ---------------------------------------------------------------------------
# TestStartupCleanupDrainSites
# ---------------------------------------------------------------------------


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
