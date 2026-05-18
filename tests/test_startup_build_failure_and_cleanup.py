"""Tests for Blocker 1 (build failures in startup outcome),
Blocker 2 (total startup failure resource cleanup), and
Blocker 1 PC Fix (catastrophic adapter-loop failure cleanup).

Blocker 1:
- One build failure + one started -> RUNNING + degraded + partial outcome.
- One build failure + zero started -> RuntimeStartupError.
- boot_summary.adapters_total includes build failures.
- boot_summary shows attempted/failed counts that include build failures.
- build_failure_count remains present.

Blocker 2:
- Empty runtime startup failure stops pipeline runner.
- Empty runtime startup failure closes storage.
- All adapters failing on start stops pipeline runner and closes storage.
- All adapters failing after partial adapter starts cleans up started
  adapters, pipeline, and storage.
- Cleanup errors are logged/suppressed but original startup failure
  remains clear.

Blocker 1 PC Fix (catastrophic adapter-loop failure):
- Catastrophic exception in the adapter startup loop (not an adapter
  start failure) cleans up started adapters, pipeline runner, and
  storage before setting FAILED state and re-raising.

Uses fake adapters only, memory storage only, no live dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from medre.runtime.builder import AdapterBuildFailure, RuntimeBuilder
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
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class _RecordingAdapter(AdapterContract):
    """Adapter that records stop() calls."""

    adapter_id: str = "recording_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "recording_adapter") -> None:
        self.adapter_id = adapter_id
        self.stopped = False

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="healthy",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _config_with_one_fake_adapter() -> RuntimeConfig:
    """RuntimeConfig with one fake matrix adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-build-failure"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
        ),
    )


def _config_with_no_adapters() -> RuntimeConfig:
    """RuntimeConfig with zero adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-no-adapters"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


# ===================================================================
# Blocker 1: Build failures in startup outcome
# ===================================================================


class TestBuildFailureInStartupOutcome:
    """Build failures affect startup outcome classification and runtime health."""

    @pytest.mark.asyncio
    async def test_one_build_failure_one_started_is_partial_degraded(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One build failure + one adapter started -> PARTIAL + DEGRADED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Inject a build failure.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_one",
                error=RuntimeError("build exploded"),
            )
        )

        await app.start()
        try:
            assert app.state == RuntimeState.RUNNING

            boot = app.boot_summary
            assert boot is not None
            # Outcome is partial because started=1, effective_failed=1.
            assert boot.startup_outcome == "partial"
            assert boot.runtime_health == "degraded"

            # Total includes build failures.
            assert boot.adapters_total == 2  # 1 adapter + 1 build failure
            assert boot.adapters_failed == 1  # 0 start failures + 1 build failure
            assert boot.adapters_started == 1
            assert boot.build_failure_count == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_one_build_failure_zero_started_raises(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One build failure + zero started -> RuntimeStartupError."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace the adapter with a failing one and add a build failure.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_one",
                error=RuntimeError("build exploded"),
            )
        )

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED

        # Boot summary is still populated before the error was raised.
        boot = app.boot_summary
        assert boot is not None
        assert boot.startup_outcome == "total_failure"
        assert boot.adapters_total == 2  # 1 adapter + 1 build failure
        assert boot.adapters_failed == 2  # 1 start failed + 1 build failed
        assert boot.adapters_started == 0
        assert boot.build_failure_count == 1

    @pytest.mark.asyncio
    async def test_boot_summary_attempted_includes_build_failures(
        self, tmp_paths: MedrePaths
    ) -> None:
        """boot_summary.adapters_total = built adapters + build failures."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Inject two build failures.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="bf1",
                error=RuntimeError("fail1"),
            )
        )
        app.build_failures.append(
            AdapterBuildFailure(
                transport="meshtastic",
                adapter_id="bf2",
                error=RuntimeError("fail2"),
            )
        )

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            assert boot.adapters_total == 3  # 1 adapter + 2 build failures
            assert boot.adapters_failed == 2  # 0 start + 2 build
            assert boot.adapters_started == 1
            assert boot.build_failure_count == 2
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_build_failures_counted_as_failed_for_health(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Build failures produce FAILED adapter states for health classification."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="bf1",
                error=RuntimeError("fail1"),
            )
        )

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            # One adapter started + one build failure -> degraded.
            assert boot.runtime_health == "degraded"
            assert boot.startup_outcome == "partial"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_only_build_failures_no_adapters_raises(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Only build failures (no built adapters) -> RuntimeStartupError."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        # Inject build failures directly.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="bf1",
                error=RuntimeError("fail1"),
            )
        )

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED
        boot = app.boot_summary
        assert boot is not None
        assert boot.adapters_total == 1  # 0 adapters + 1 build failure
        assert boot.adapters_failed == 1
        assert boot.build_failure_count == 1


# ===================================================================
# Blocker 2: Total startup failure resource cleanup
# ===================================================================


class TestStartupFailureResourceCleanup:
    """Total startup failure cleans up pipeline runner and storage."""

    @pytest.mark.asyncio
    async def test_empty_runtime_failure_stops_pipeline_runner(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Empty runtime startup failure stops the pipeline runner."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        # Mock pipeline_runner.stop to verify it's called.
        original_stop = app.pipeline_runner.stop
        stop_called = False

        async def _tracking_stop() -> None:
            nonlocal stop_called
            stop_called = True
            await original_stop()

        app.pipeline_runner.stop = _tracking_stop  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert (
            stop_called
        ), "pipeline_runner.stop() was not called on total startup failure"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_empty_runtime_failure_closes_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Empty runtime startup failure closes storage."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        # Mock storage.close to verify it's called.
        assert app.storage is not None
        original_close = app.storage.close
        close_called = False

        async def _tracking_close() -> None:
            nonlocal close_called
            close_called = True
            await original_close()

        app.storage.close = _tracking_close  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert close_called, "storage.close() was not called on total startup failure"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_all_adapters_fail_stops_pipeline_and_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All adapters failing on start stops pipeline runner and closes storage."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace adapter with failing one.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")

        # Track pipeline runner stop and storage close.
        pipeline_stop_called = False
        storage_close_called = False

        original_pipeline_stop = app.pipeline_runner.stop

        async def _track_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            pipeline_stop_called = True
            await original_pipeline_stop()

        app.pipeline_runner.stop = _track_pipeline_stop  # type: ignore[assignment]

        assert app.storage is not None
        original_storage_close = app.storage.close

        async def _track_storage_close() -> None:
            nonlocal storage_close_called
            storage_close_called = True
            await original_storage_close()

        app.storage.close = _track_storage_close  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert pipeline_stop_called, "pipeline_runner.stop() was not called"
        assert storage_close_called, "storage.close() was not called"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_partial_then_total_failure_cleans_up_adapters(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Build failure makes total count = 2, adapter starts then build failure
        makes it total_failure, so started adapter gets cleaned up."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Use a failing adapter + a build failure so no adapters start
        # (started=0, effective_failed=2, total=2 -> TOTAL_FAILURE).
        # Verifies pipeline runner and storage are cleaned up.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_build",
                error=RuntimeError("build failed"),
            )
        )

        pipeline_stop_called = False
        storage_close_called = False

        original_pipeline_stop = app.pipeline_runner.stop

        async def _track_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            pipeline_stop_called = True
            await original_pipeline_stop()

        app.pipeline_runner.stop = _track_pipeline_stop  # type: ignore[assignment]

        assert app.storage is not None
        original_storage_close = app.storage.close

        async def _track_storage_close() -> None:
            nonlocal storage_close_called
            storage_close_called = True
            await original_storage_close()

        app.storage.close = _track_storage_close  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert pipeline_stop_called
        assert storage_close_called
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_cleanup_errors_suppressed_original_failure_clear(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Cleanup errors during startup failure are logged but the original
        RuntimeStartupError is still raised."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        # Make pipeline_runner.stop raise an error.
        async def _failing_stop() -> None:
            raise RuntimeError("pipeline cleanup exploded")

        app.pipeline_runner.stop = _failing_stop  # type: ignore[assignment]

        # Make storage.close raise an error too.
        assert app.storage is not None

        async def _failing_close() -> None:
            raise RuntimeError("storage cleanup exploded")

        app.storage.close = _failing_close  # type: ignore[assignment]

        # The original RuntimeStartupError should still be raised.
        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_started_adapters_cleaned_up_on_total_failure(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When one adapter starts but build failures make total failure,
        the started adapter is cleaned up."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Use a failing adapter + build failure to trigger total startup failure.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")

        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_build",
                error=RuntimeError("build failed"),
            )
        )

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        # No adapters started, but cleanup should have been called.
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_pipeline_init_failure_closes_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When pipeline runner fails to start, storage is closed."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Make pipeline_runner.start raise.
        async def _failing_start() -> None:
            raise RuntimeError("pipeline start exploded")

        app.pipeline_runner.start = _failing_start  # type: ignore[assignment]

        # Track storage close.
        assert app.storage is not None
        close_called = False
        original_close = app.storage.close

        async def _track_close() -> None:
            nonlocal close_called
            close_called = True
            await original_close()

        app.storage.close = _track_close  # type: ignore[assignment]

        with pytest.raises(
            RuntimeStartupError, match="Failed to start pipeline runner"
        ):
            await app.start()

        assert close_called, "storage.close() was not called when pipeline init failed"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_caller_does_not_need_stop_after_failed_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        """After start() raises RuntimeStartupError, calling stop() is not required."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        with pytest.raises(RuntimeStartupError):
            await app.start()

        # State is FAILED, not STOPPED. Calling stop() should be safe but not required.
        assert app.state == RuntimeState.FAILED

        # Verify that a second start() fails because state is not INITIALIZED.
        with pytest.raises(RuntimeError, match="already started"):
            await app.start()


# ===================================================================
# Blocker 1 PC Fix: Catastrophic adapter-loop failure cleanup
# ===================================================================


def _config_with_two_fake_adapters() -> RuntimeConfig:
    """RuntimeConfig with two fake matrix adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-catastrophic-loop"),
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


class TestCatastrophicLoopFailureCleanup:
    """Catastrophic exception in the adapter startup loop cleans up
    started adapters, pipeline runner, and storage before re-raising."""

    @pytest.mark.asyncio
    async def test_catastrophic_loop_cleans_adapters_pipeline_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Catastrophic loop failure after one adapter started cleans up
        started adapters, pipeline runner, and storage."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Replace both adapters with recording adapters.
        alpha = _RecordingAdapter(adapter_id="alpha")
        beta = _RecordingAdapter(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        # Track pipeline_runner.stop and storage.close.
        pipeline_stop_called = False
        storage_close_called = False

        original_pipeline_stop = app.pipeline_runner.stop

        async def _track_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            pipeline_stop_called = True
            await original_pipeline_stop()

        app.pipeline_runner.stop = _track_pipeline_stop  # type: ignore[assignment]

        assert app.storage is not None
        original_storage_close = app.storage.close

        async def _track_storage_close() -> None:
            nonlocal storage_close_called
            storage_close_called = True
            await original_storage_close()

        app.storage.close = _track_storage_close  # type: ignore[assignment]

        # Patch _monotonic_ms to raise on call 3 (after alpha adapter starts
        # successfully; calls 1 and 2 are for alpha's t0 and elapsed).
        # Call 3 is beta's t0, which is outside the inner try → outer except.
        import medre.runtime.app as _app_mod

        call_count = 0
        original_monotonic_ms = _app_mod._monotonic_ms

        def _exploding_monotonic_ms() -> float:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise RuntimeError("catastrophic loop failure")
            return original_monotonic_ms()

        with patch.object(
            _app_mod, "_monotonic_ms", side_effect=_exploding_monotonic_ms
        ):
            with pytest.raises(RuntimeError, match="catastrophic loop failure"):
                await app.start()

        # Verify started adapter was stopped.
        assert alpha.stopped, "Started adapter stop() was not called"

        # Verify pipeline runner was stopped.
        assert pipeline_stop_called, "pipeline_runner.stop() was not called"

        # Verify storage was closed.
        assert storage_close_called, "storage.close() was not called"

        # Verify state is FAILED.
        assert app.state == RuntimeState.FAILED


# ===================================================================
# INITIALIZING->STOPPED: adapter.start() failure cleanup
# ===================================================================


class _StartFailsButTracksStop(AdapterContract):
    """Adapter that raises on start() and records whether stop() was called."""

    adapter_id: str = "track_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "track_stop") -> None:
        self.adapter_id = adapter_id
        self.stop_called = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated start failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True

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


class _StopAlsoFailsAdapter(AdapterContract):
    """Adapter whose start() and stop() both raise."""

    adapter_id: str = "double_fail"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "double_fail") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError("start exploded")

    async def stop(self, timeout: float = 5.0) -> None:
        raise RuntimeError("stop also exploded")

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


class TestAdapterStartFailureCleanup:
    """Failed adapter.start() triggers best-effort adapter.stop() for cleanup."""

    @pytest.mark.asyncio
    async def test_failed_start_calls_adapter_stop(self, tmp_paths: MedrePaths) -> None:
        """When adapter.start() fails, adapter.stop() is called for cleanup."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Replace alpha with an adapter that fails start but tracks stop calls.
        tracker = _StartFailsButTracksStop(adapter_id="alpha")
        app.adapters["alpha"] = tracker

        # Beta starts normally (recording adapter).
        beta = _RecordingAdapter(adapter_id="beta")
        app.adapters["beta"] = beta

        await app.start()
        try:
            # Alpha failed start but stop() should have been called.
            assert (
                tracker.stop_called
            ), "adapter.stop() was not called after start failure"

            # Beta should be started fine.
            assert beta.adapter_id in app.started_adapter_ids
            assert "alpha" not in app.started_adapter_ids
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_failed_start_state_is_failed(self, tmp_paths: MedrePaths) -> None:
        """After adapter.start() fails, adapter state is FAILED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        tracker = _StartFailsButTracksStop(adapter_id="fake_matrix")
        app.adapters["fake_matrix"] = tracker

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        states = app.adapter_states
        assert states.get("fake_matrix") == AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_stop_failure_after_start_failure_suppressed(
        self, tmp_paths: MedrePaths
    ) -> None:
        """If both start() and stop() fail, original error is preserved."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        double_fail = _StopAlsoFailsAdapter(adapter_id="fake_matrix")
        app.adapters["fake_matrix"] = double_fail

        # Should raise RuntimeStartupError (not the stop cleanup error).
        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        states = app.adapter_states
        assert states.get("fake_matrix") == AdapterState.FAILED


class TestStopNeverStartedAdapters:
    """stop() on adapters that were never started transitions STOPPING->STOPPED."""

    @pytest.mark.asyncio
    async def test_never_started_adapter_gets_stopped_not_failed(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Adapters in INITIALIZING state transition STOPPING->STOPPED during stop()."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Start only alpha; beta will remain in INITIALIZING.
        alpha = _RecordingAdapter(adapter_id="alpha")
        app.adapters["alpha"] = alpha

        # Beta will fail to start.
        beta_fail = _StartFailsButTracksStop(adapter_id="beta")
        app.adapters["beta"] = beta_fail

        # With one started and one failed, runtime enters RUNNING (partial).
        await app.start()

        # Now stop the runtime.
        await app.stop()

        # Alpha: started and then stopped.
        assert alpha.stopped
        assert app.adapter_states.get("alpha") == AdapterState.STOPPED

        # Beta: failed start (already FAILED), stop() not called again by stop()
        # because beta is not in started_adapter_ids and its state is already FAILED
        # (set during start() failure path).
        assert app.adapter_states.get("beta") == AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_stop_on_never_started_adapter_uses_stopping_to_stopped(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter that was built but start() was never called
        transitions STOPPING->STOPPED in stop()."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Start the app normally — both adapters start.
        await app.start()

        # Manually inject a third adapter that was never started.
        gamma = _RecordingAdapter(adapter_id="gamma")
        app.adapters["gamma"] = gamma
        app._adapter_states["gamma"] = AdapterState.INITIALIZING

        await app.stop()

        # gamma should be STOPPED (went through STOPPING->STOPPED).
        assert app.adapter_states.get("gamma") == AdapterState.STOPPED
        assert gamma.stopped


class TestTotalFailureCleansNeverStartedAdapters:
    """TOTAL_FAILURE path cleans up adapters that never started (INITIALIZING)."""

    @pytest.mark.asyncio
    async def test_total_failure_stops_initializing_adapters(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When all adapters fail, never-started adapters are cleaned up too."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Both fail to start.
        alpha = _StartFailsButTracksStop(adapter_id="alpha")
        beta = _StartFailsButTracksStop(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        # Both adapters should have had stop() called during cleanup.
        assert alpha.stop_called, "alpha stop() not called during total failure cleanup"
        assert beta.stop_called, "beta stop() not called during total failure cleanup"

        # Both should be FAILED (from start failure path).
        assert app.adapter_states.get("alpha") == AdapterState.FAILED
        assert app.adapter_states.get("beta") == AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_total_failure_cleans_mixed_started_and_initializing(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When one adapter starts but total failure is triggered by build failures,
        the started adapter is cleaned up and the never-started adapter is also cleaned up.
        """
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # alpha will start successfully, beta will fail.
        alpha = _RecordingAdapter(adapter_id="alpha")
        beta_fail = _StartFailsButTracksStop(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta_fail

        # Add a build failure to make it total failure:
        # started=1, failed=1 (start) + 1 (build) = 2, total=3 → but 1 started...
        # Actually we need TOTAL_FAILURE: 0 started. Let's make both fail.
        app.adapters["alpha"] = _StartFailsButTracksStop(adapter_id="alpha")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        # Both failed adapters should have stop() called.
        assert app.adapters["alpha"].stop_called
        assert app.adapters["beta"].stop_called


# ===================================================================
# Deterministic start ordering
# ===================================================================


class _OrderRecordingAdapter(AdapterContract):
    """Adapter that records the order in which start() is called."""

    adapter_id: str = "order_recorder"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "order_recorder") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self, timeout: float = 5.0) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="healthy",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


class TestDeterministicStartOrdering:
    """Adapters are started in sorted adapter_id order."""

    @pytest.mark.asyncio
    async def test_adapters_started_in_sorted_id_order(
        self, tmp_paths: MedrePaths
    ) -> None:
        """started_adapter_ids reflects sorted adapter_id start order."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Replace with recording adapters whose IDs sort differently
        # from config declaration order.
        gamma = _OrderRecordingAdapter(adapter_id="gamma")
        alpha = _OrderRecordingAdapter(adapter_id="alpha")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = gamma
        # Rename the key so the dict has "gamma" key
        app.adapters["gamma"] = app.adapters.pop("beta")

        await app.start()
        try:
            # started_adapter_ids should be sorted by adapter_id.
            assert app.started_adapter_ids == ["alpha", "gamma"]
        finally:
            await app.stop()


# ===================================================================
# BootSummary accuracy: route_count, build_failure_ids
# ===================================================================


class TestBootSummaryRouteCountAndBuildFailureIds:
    """BootSummary.route_count counts registered routes;
    build_failure_ids lists build-failure adapter IDs."""

    @pytest.mark.asyncio
    async def test_route_count_excludes_disabled_routes(
        self, tmp_paths: MedrePaths
    ) -> None:
        """BootSummary.route_count counts only registered (active) routes."""
        from medre.runtime.routes import RouteConfig, RouteConfigSet

        rt_a = _fake_matrix_config(adapter_id="adapter_a")
        rt_b = _fake_matrix_config(adapter_id="adapter_b")
        route_enabled = RouteConfig(
            route_id="active_route",
            source_adapters=("adapter_a",),
            dest_adapters=("adapter_b",),
            enabled=True,
        )
        route_disabled = RouteConfig(
            route_id="disabled_route",
            source_adapters=("adapter_a",),
            dest_adapters=("adapter_b",),
            enabled=False,
        )
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-route-count"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"a": rt_a, "b": rt_b},
            ),
            routes=RouteConfigSet(routes=(route_enabled, route_disabled)),
        )
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            # Only the enabled route should be counted.
            assert boot.route_count == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_route_count_zero_when_no_routes_configured(
        self, tmp_paths: MedrePaths
    ) -> None:
        """BootSummary.route_count is 0 when no routes are configured."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            assert boot.route_count == 0
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_build_failure_ids_populated(self, tmp_paths: MedrePaths) -> None:
        """BootSummary.build_failure_ids contains the adapter IDs that failed to build."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Inject build failures.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_alpha",
                error=RuntimeError("build failed"),
            )
        )
        app.build_failures.append(
            AdapterBuildFailure(
                transport="meshtastic",
                adapter_id="broken_beta",
                error=RuntimeError("build failed"),
            )
        )

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            # build_failure_ids should be sorted.
            assert boot.build_failure_ids == ("broken_alpha", "broken_beta")
            assert boot.build_failure_count == 2
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_build_failure_ids_empty_when_no_failures(
        self, tmp_paths: MedrePaths
    ) -> None:
        """BootSummary.build_failure_ids is empty when no build failures."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            assert boot.build_failure_ids == ()
            assert boot.build_failure_count == 0
        finally:
            await app.stop()


# ===================================================================
# Integrated: build failure + start failure + route degradation
# ===================================================================


class TestIntegratedBuildStartRouteDegradation:
    """Combined scenario: build failures, start failures, and route
    degradation in a single startup sequence."""

    @pytest.mark.asyncio
    async def test_build_and_start_failures_with_route_degradation(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Build failure + start failure produces DEGRADED health,
        partial outcome, and route eligibility reflects both."""
        from medre.runtime.routes import RouteConfig, RouteConfigSet

        rt_a = _fake_matrix_config(adapter_id="adapter_a")
        rt_b = _fake_matrix_config(adapter_id="adapter_b")
        # Route from a → b (both should build fine as fakes).
        route1 = RouteConfig(
            route_id="route_a_to_b",
            source_adapters=("adapter_a",),
            dest_adapters=("adapter_b",),
            enabled=True,
        )
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-integrated-degradation"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"a": rt_a, "b": rt_b},
            ),
            routes=RouteConfigSet(routes=(route1,)),
        )
        app = _build_app(config, tmp_paths)

        # Make adapter_b fail on start.
        app.adapters["adapter_b"] = _FailingAdapter(adapter_id="adapter_b")

        # Add a build failure for a phantom adapter.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="phantom_build_fail",
                error=RuntimeError("phantom build failure"),
            )
        )

        await app.start()
        try:
            # Runtime should be RUNNING (adapter_a started).
            assert app.state == RuntimeState.RUNNING

            boot = app.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "partial"
            assert boot.runtime_health == "degraded"
            assert boot.adapters_started == 1
            assert boot.adapters_failed == 2  # 1 start + 1 build
            assert boot.adapters_total == 3  # 2 built + 1 build failure
            assert boot.build_failure_count == 1
            assert boot.build_failure_ids == ("phantom_build_fail",)
            assert boot.failed_adapter_ids == ("adapter_b",)

            # Route eligibility: route_a_to_b is registered but degraded
            # because adapter_b failed to build at build-time eligibility.
            # Actually adapter_b DID build (it's a fake) but failed to start.
            # So build-time eligibility says REGISTERED, but startup readiness
            # should downgrade it.
            eligibility = app.route_eligibility
            assert eligibility is not None
            assert "route_a_to_b" in eligibility.route_states

            # Startup readiness should reflect the start failure.
            readiness = app.startup_readiness
            assert readiness is not None
            assert readiness.route_states.get("route_a_to_b") is not None
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_all_start_failures_with_build_failures_is_total_failure(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All adapters fail to start + build failures → TOTAL_FAILURE + FAILED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Make the single adapter fail to start.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")

        # Add a build failure.
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_build",
                error=RuntimeError("build failed"),
            )
        )

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED
        boot = app.boot_summary
        assert boot is not None
        assert boot.startup_outcome == "total_failure"
        assert boot.build_failure_ids == ("broken_build",)
        assert boot.failed_adapter_ids == ("fake_matrix",)
        assert boot.adapters_total == 2
        assert boot.adapters_started == 0
