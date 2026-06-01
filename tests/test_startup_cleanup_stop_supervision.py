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
from medre.runtime.app import MedreApp, RuntimeState
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
        cleanup (after start failure) is caught; pipeline and storage cleanup
        proceed.  The CancelledError here is raised in the per-adapter
        best-effort stop (line ~697), not in _cleanup_started_adapters()."""
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
