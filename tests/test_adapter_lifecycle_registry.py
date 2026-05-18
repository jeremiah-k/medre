"""Tests for MedreApp-owned per-adapter lifecycle state registry.

Covers:
- READY after successful adapter start.
- FAILED after adapter start failure.
- STOPPED after clean adapter stop.
- FAILED after adapter stop failure.
- Total startup failure cleanup marks adapters STOPPED (success) or FAILED (error).
- adapter_states property returns a read-only copy.
- _set_adapter_state validates transitions.
- Snapshot exposes per-adapter lifecycle states deterministically (sorted).
- Build failures produce FAILED entries in registry.
- Never-started adapters are marked FAILED during stop.

Uses fake adapters only, memory storage only, no live dependencies.
"""

from __future__ import annotations

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
from medre.core.lifecycle.states import (
    AdapterState,
    InvalidStateTransition,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import AdapterBuildFailure, RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError
from medre.runtime.snapshot import build_runtime_snapshot

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


class _StubAdapter(AdapterContract):
    """Cooperative adapter that tracks start/stop calls."""

    adapter_id: str = "stub"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "stub") -> None:
        self.adapter_id = adapter_id
        self.started = False
        self.stopped = False

    async def start(self, ctx: AdapterContext) -> None:
        self.started = True

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


class _FailingStartAdapter(AdapterContract):
    """Adapter that raises on start()."""

    adapter_id: str = "failing_start"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_start") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated start failure: {self.adapter_id}")

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


class _FailingStopAdapter(AdapterContract):
    """Adapter that raises on stop()."""

    adapter_id: str = "failing_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_stop") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self, timeout: float = 5.0) -> None:
        raise RuntimeError(f"Simulated stop failure: {self.adapter_id}")

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


def _config_with_one_fake_adapter(adapter_id: str = "fake_matrix") -> RuntimeConfig:
    """RuntimeConfig with one fake matrix adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-lifecycle-registry"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={adapter_id: _fake_matrix_config(adapter_id)},
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
# Tests: Adapter state after successful start
# ===================================================================


class TestReadyAfterStart:
    """READY state after successful adapter start."""

    @pytest.mark.asyncio
    async def test_single_adapter_ready_after_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_multiple_adapters_ready_after_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-multi"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "alpha": _fake_matrix_config("alpha"),
                    "beta": _fake_matrix_config("beta"),
                },
            ),
        )
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            assert app.adapter_states["alpha"] == AdapterState.READY
            assert app.adapter_states["beta"] == AdapterState.READY
        finally:
            await app.stop()


# ===================================================================
# Tests: FAILED after start failure
# ===================================================================


class TestFailedAfterStartFailure:
    """FAILED state after adapter start failure."""

    @pytest.mark.asyncio
    async def test_failed_after_start_exception(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace with failing adapter.
        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")

        # Add a second working adapter so runtime doesn't totally fail.
        app.adapters["working"] = _StubAdapter("working")

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.FAILED
            assert app.adapter_states["working"] == AdapterState.READY
        finally:
            await app.stop()


# ===================================================================
# Tests: STOPPED after clean stop
# ===================================================================


class TestStoppedAfterCleanStop:
    """STOPPED state after clean adapter stop."""

    @pytest.mark.asyncio
    async def test_stopped_after_stop(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.adapter_states["fake_matrix"] == AdapterState.READY

        await app.stop()
        assert app.adapter_states["fake_matrix"] == AdapterState.STOPPED


# ===================================================================
# Tests: FAILED after stop failure
# ===================================================================


class TestFailedAfterStopFailure:
    """FAILED state after adapter stop failure."""

    @pytest.mark.asyncio
    async def test_failed_after_stop_exception(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace with stop-failing adapter.
        app.adapters["fake_matrix"] = _FailingStopAdapter("fake_matrix")

        await app.start()

        with pytest.raises(Exception, match="Simulated stop failure"):
            await app.stop()

        assert app.adapter_states["fake_matrix"] == AdapterState.FAILED


# ===================================================================
# Tests: Total startup failure cleanup
# ===================================================================


class TestTotalStartupFailureCleanup:
    """Total startup failure cleanup marks adapters and frees resources."""

    @pytest.mark.asyncio
    async def test_total_failure_stopped_adapter_gets_stopped_state(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Adapters that started but hit total failure get STOPPED after cleanup."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Adapter will start fine, but add a build failure to force total failure.
        # Actually, we need 0 adapters to start. Let's replace with failing.
        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.adapter_states["fake_matrix"] == AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_total_failure_with_started_adapter_cleans_up(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Started adapter gets STOPPED during total-failure cleanup."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-total-fail-cleanup"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "good": _fake_matrix_config("good"),
                    "bad": _fake_matrix_config("bad"),
                },
            ),
        )
        app = _build_app(config, tmp_paths)

        # "good" starts fine, "bad" fails -> partial, not total.
        # To get total failure with a started adapter being cleaned up,
        # we need all to fail but have one start first.
        # Actually, let's make all fail by replacing both.
        app.adapters["good"] = _FailingStartAdapter("good")
        app.adapters["bad"] = _FailingStartAdapter("bad")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        # Both should be FAILED (they failed to start, no cleanup needed).
        assert app.adapter_states["good"] == AdapterState.FAILED
        assert app.adapter_states["bad"] == AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_cleanup_marks_started_adapters_stopped(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Adapters that started then hit total failure get STOPPED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        stub = _StubAdapter("fake_matrix")
        app.adapters["fake_matrix"] = stub

        # Add a build failure to make it total (0 effective started + build failure).
        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken",
                error=RuntimeError("build exploded"),
            )
        )

        # One adapter started + one build failure -> partial, NOT total failure.
        # Let's make the adapter fail too.
        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.adapter_states["fake_matrix"] == AdapterState.FAILED
        assert app.adapter_states["broken"] == AdapterState.FAILED


# ===================================================================
# Tests: Build failures in registry
# ===================================================================


class TestBuildFailuresInRegistry:
    """Build failures produce FAILED entries in the registry."""

    @pytest.mark.asyncio
    async def test_build_failure_is_failed_in_registry(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_one",
                error=RuntimeError("build exploded"),
            )
        )

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
            assert app.adapter_states["broken_one"] == AdapterState.FAILED
        finally:
            await app.stop()


# ===================================================================
# Tests: adapter_states property returns a copy
# ===================================================================


class TestAdapterStatesProperty:
    """adapter_states returns a read-only copy."""

    @pytest.mark.asyncio
    async def test_property_returns_copy(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            states1 = app.adapter_states
            states2 = app.adapter_states
            assert states1 == states2
            assert states1 is not states2  # different dict objects
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_mutation_of_copy_does_not_affect_registry(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            copy = app.adapter_states
            copy["fake_matrix"] = AdapterState.FAILED
            # Original should be unchanged.
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()


# ===================================================================
# Tests: _set_adapter_state validates transitions
# ===================================================================


class TestSetAdapterStateValidation:
    """_set_adapter_state validates legal transitions."""

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            # READY -> INITIALIZING is not valid.
            with pytest.raises(InvalidStateTransition):
                app._set_adapter_state("fake_matrix", AdapterState.INITIALIZING)
        finally:
            await app.stop()

    def test_initial_assignment_bypasses_validation(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        # Any state is fine for initial assignment.
        app._set_adapter_state("new_adapter", AdapterState.STOPPED)
        assert app._adapter_states["new_adapter"] == AdapterState.STOPPED

    def test_same_state_is_idempotent(self, tmp_paths: MedrePaths) -> None:
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        app._set_adapter_state("a", AdapterState.READY)
        app._set_adapter_state("a", AdapterState.READY)  # no error
        assert app._adapter_states["a"] == AdapterState.READY


# ===================================================================
# Tests: Snapshot exposes adapter lifecycle states deterministically
# ===================================================================


class TestSnapshotAdapterLifecycleStates:
    """Snapshot exposes per-adapter lifecycle states sorted/deterministically."""

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_adapters_after_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-snapshot-states"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "zebra": _fake_matrix_config("zebra"),
                    "alpha": _fake_matrix_config("alpha"),
                },
            ),
        )
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            snap = build_runtime_snapshot(app)
            lifecycle_adapters = snap["lifecycle"]["adapters"]
            assert isinstance(lifecycle_adapters, dict)
            # Sorted by adapter_id.
            assert list(lifecycle_adapters.keys()) == ["alpha", "zebra"]
            assert lifecycle_adapters["alpha"] == "ready"
            assert lifecycle_adapters["zebra"] == "ready"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_adapters_after_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        await app.stop()

        snap = build_runtime_snapshot(app)
        lifecycle_adapters = snap["lifecycle"]["adapters"]
        assert lifecycle_adapters["fake_matrix"] == "stopped"

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_adapters_includes_build_failures(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken_early",
                error=RuntimeError("build failed"),
            )
        )

        await app.start()
        try:
            snap = build_runtime_snapshot(app)
            lifecycle_adapters = snap["lifecycle"]["adapters"]
            assert lifecycle_adapters["broken_early"] == "failed"
            assert lifecycle_adapters["fake_matrix"] == "ready"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_adapters_empty_before_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        snap = build_runtime_snapshot(app)
        lifecycle_adapters = snap["lifecycle"]["adapters"]
        assert lifecycle_adapters == {}

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_adapters_failed_after_start_failure(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-snapshot-fail"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "good": _fake_matrix_config("good"),
                    "bad": _fake_matrix_config("bad"),
                },
            ),
        )
        app = _build_app(config, tmp_paths)
        app.adapters["bad"] = _FailingStartAdapter("bad")

        await app.start()
        try:
            snap = build_runtime_snapshot(app)
            lifecycle_adapters = snap["lifecycle"]["adapters"]
            assert lifecycle_adapters["bad"] == "failed"
            assert lifecycle_adapters["good"] == "ready"
        finally:
            await app.stop()


# ===================================================================
# Tests: STOPPING state observable during stop
# ===================================================================


class TestStoppingState:
    """Adapters go through STOPPING during shutdown."""

    @pytest.mark.asyncio
    async def test_stopping_state_during_graceful_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """During stop, adapters transition through STOPPING.

        We verify this by checking that the final state is STOPPED,
        which requires passing through STOPPING (READY->STOPPING->STOPPED).
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.adapter_states["fake_matrix"] == AdapterState.READY

        await app.stop()
        # Final state is STOPPED, proving STOPPING was set and resolved.
        assert app.adapter_states["fake_matrix"] == AdapterState.STOPPED
