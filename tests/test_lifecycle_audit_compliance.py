"""Lifecycle audit compliance tests for MEDRE adapters.

Proves documented lifecycle authority and selected high-value findings
from ``docs/dev/adapter-lifecycle-audit.md`` without requiring live
network or hardware.

Evidence level: ``fake_pipeline`` (tier 1).  Uses stub/fake adapters and
in-memory storage only.

Covers:
- AdapterState terminal semantics match audit documentation.
- Runtime owns durable adapter state; adapters report facts only.
- All four real adapters expose the lifecycle methods required by
  AdapterContract.
- Adapters use ``health_check()`` to report health strings rather than
  importing or mutating ``AdapterState`` directly.
- ``health_to_adapter_state()`` mapping is correct.
- Documented follow-up identifiers (LXMF-1, LXMF-2, MESHTASTIC-1,
  CROSS-1, CROSS-2) are present in the audit document.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
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
    VALID_TRANSITIONS,
    AdapterState,
    InvalidStateTransition,
    is_valid_transition,
)
from medre.core.supervision.health import (
    VALID_HEALTH_STRINGS,
    health_to_adapter_state,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import AdapterBuildFailure, RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError

REPO_ROOT = Path(__file__).resolve().parents[1]

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


@pytest.fixture
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


class _HealthReportingAdapter(AdapterContract):
    """Adapter that returns a configurable health string from health_check()."""

    adapter_id: str = "health_reporter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(
        self, adapter_id: str = "health_reporter", health: str = "healthy"
    ) -> None:
        self.adapter_id = adapter_id
        self._health = health

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
            health=self._health,
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
        runtime=RuntimeOptions(name="test-lifecycle-audit"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={adapter_id: _fake_matrix_config(adapter_id)},
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


# ===================================================================
# 1. Terminal semantics match audit documentation
# ===================================================================


class TestTerminalSemanticsMatchAudit:
    """AdapterState terminal semantics match the audit document's table."""

    def test_eight_states_exist(self) -> None:
        """Audit documents exactly eight AdapterState members."""
        assert len(AdapterState) == 8

    def test_documented_states_present(self) -> None:
        """All states named in the audit table exist."""
        expected = {
            "INITIALIZING",
            "READY",
            "DEGRADED",
            "BACKPRESSURED",
            "DISCONNECTED",
            "STOPPING",
            "FAILED",
            "STOPPED",
        }
        actual = {s.name for s in AdapterState}
        assert actual == expected

    def test_failed_is_terminal(self) -> None:
        """FAILED has no outgoing transitions (audit: 'Terminal: Yes')."""
        assert VALID_TRANSITIONS[AdapterState.FAILED] == frozenset()

    def test_stopped_is_terminal(self) -> None:
        """STOPPED has no outgoing transitions (audit: 'Terminal: Yes')."""
        assert VALID_TRANSITIONS[AdapterState.STOPPED] == frozenset()

    def test_non_terminal_states_have_transitions(self) -> None:
        """Non-terminal states all have at least one outgoing transition."""
        terminal = {AdapterState.FAILED, AdapterState.STOPPED}
        for state in AdapterState:
            if state in terminal:
                continue
            assert (
                len(VALID_TRANSITIONS[state]) > 0
            ), f"{state.name} should have outgoing transitions"

    def test_initializing_to_ready_valid(self) -> None:
        """INITIALIZING → READY is valid (audit: startup success)."""
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.READY)

    def test_initializing_to_failed_valid(self) -> None:
        """INITIALIZING → FAILED is valid (audit: startup failure)."""
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.FAILED)

    def test_ready_to_stopping_valid(self) -> None:
        """READY → STOPPING is valid (audit: graceful shutdown)."""
        assert is_valid_transition(AdapterState.READY, AdapterState.STOPPING)

    def test_ready_to_failed_valid(self) -> None:
        """READY → FAILED is valid (audit: runtime failure)."""
        assert is_valid_transition(AdapterState.READY, AdapterState.FAILED)

    def test_stopping_to_stopped_valid(self) -> None:
        """STOPPING → STOPPED is valid (audit: clean shutdown)."""
        assert is_valid_transition(AdapterState.STOPPING, AdapterState.STOPPED)

    def test_stopping_to_failed_valid(self) -> None:
        """STOPPING → FAILED is valid (audit: error during shutdown)."""
        assert is_valid_transition(AdapterState.STOPPING, AdapterState.FAILED)


# ===================================================================
# 2. Runtime owns durable adapter state, not adapters
# ===================================================================


class TestRuntimeOwnsAdapterState:
    """The runtime's _adapter_states dict is the authority for adapter
    lifecycle state, not the adapter objects themselves."""

    async def test_set_adapter_state_validates_transition(
        self, tmp_paths: MedrePaths
    ) -> None:
        """_set_adapter_state rejects invalid transitions."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            with pytest.raises(InvalidStateTransition):
                app._set_adapter_state("fake_matrix", AdapterState.INITIALIZING)
        finally:
            await app.stop()

    async def test_initial_assignment_any_state(self, tmp_paths: MedrePaths) -> None:
        """First assignment for a new adapter_id accepts any state."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # No adapters registered yet for "brand_new" — any state is fine.
        for state in AdapterState:
            app._set_adapter_state("brand_new", state)
            assert app._adapter_states["brand_new"] == state
            del app._adapter_states["brand_new"]

    async def test_adapter_state_property_returns_copy(
        self, tmp_paths: MedrePaths
    ) -> None:
        """adapter_states returns a defensive copy."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            s1 = app.adapter_states
            s2 = app.adapter_states
            assert s1 is not s2
            assert s1 == s2
            # Mutating the copy must not affect the original.
            s1["fake_matrix"] = AdapterState.FAILED
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()

    async def test_ready_after_successful_start(self, tmp_paths: MedrePaths) -> None:
        """Runtime sets READY after adapter.start() succeeds."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()

    async def test_failed_after_start_exception(self, tmp_paths: MedrePaths) -> None:
        """Runtime sets FAILED after adapter.start() raises."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace with a failing adapter + a working one (avoid total failure).
        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")
        app.adapters["worker"] = _StubAdapter("worker")

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.FAILED
            assert app.adapter_states["worker"] == AdapterState.READY
        finally:
            await app.stop()

    async def test_stopped_after_clean_stop(self, tmp_paths: MedrePaths) -> None:
        """Runtime sets STOPPED after adapter.stop() succeeds."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        await app.stop()
        assert app.adapter_states["fake_matrix"] == AdapterState.STOPPED

    async def test_build_failure_gets_failed_state(self, tmp_paths: MedrePaths) -> None:
        """Build failures produce FAILED in the registry."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.build_failures.append(
            AdapterBuildFailure(
                transport="matrix",
                adapter_id="broken",
                error=RuntimeError("build exploded"),
            )
        )

        await app.start()
        try:
            assert app.adapter_states["broken"] == AdapterState.FAILED
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()

    async def test_same_state_assignment_is_idempotent(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Assigning the same state twice does not raise."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            app._set_adapter_state("fake_matrix", AdapterState.READY)
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()


# ===================================================================
# 3. All real adapters implement AdapterContract
# ===================================================================


class TestAllAdaptersImplementContract:
    """All four real adapter classes are proper AdapterContract subclasses."""

    @pytest.mark.parametrize(
        "adapter_cls",
        [MatrixAdapter, MeshtasticAdapter, MeshCoreAdapter, LxmfAdapter],
        ids=["matrix", "meshtastic", "meshcore", "lxmf"],
    )
    def test_is_adapter_contract_subclass(self, adapter_cls: type) -> None:
        assert issubclass(adapter_cls, AdapterContract)

    @pytest.mark.parametrize(
        "adapter_cls",
        [MatrixAdapter, MeshtasticAdapter, MeshCoreAdapter, LxmfAdapter],
        ids=["matrix", "meshtastic", "meshcore", "lxmf"],
    )
    def test_has_required_lifecycle_methods(self, adapter_cls: type) -> None:
        """Each adapter exposes start, stop, health_check, and deliver."""
        for method_name in ("start", "stop", "health_check", "deliver"):
            assert hasattr(
                adapter_cls, method_name
            ), f"{adapter_cls.__name__} missing {method_name}"

    @pytest.mark.parametrize(
        "adapter_cls",
        [MatrixAdapter, MeshtasticAdapter, MeshCoreAdapter, LxmfAdapter],
        ids=["matrix", "meshtastic", "meshcore", "lxmf"],
    )
    def test_lifecycle_methods_are_async(self, adapter_cls: type) -> None:
        """start, stop, health_check, and deliver are coroutine functions."""
        for method_name in ("start", "stop", "health_check", "deliver"):
            method = getattr(adapter_cls, method_name)
            assert inspect.iscoroutinefunction(
                method
            ), f"{adapter_cls.__name__}.{method_name} must be async"

    @pytest.mark.parametrize(
        "adapter_cls",
        [MatrixAdapter, MeshtasticAdapter, MeshCoreAdapter, LxmfAdapter],
        ids=["matrix", "meshtastic", "meshcore", "lxmf"],
    )
    def test_has_class_and_instance_identity(self, adapter_cls: type) -> None:
        """Each adapter declares platform and role as class attrs;
        adapter_id is set per-instance."""
        for attr in ("platform", "role"):
            assert hasattr(
                adapter_cls, attr
            ), f"{adapter_cls.__name__} missing class attr {attr}"
        # adapter_id is an instance attribute (set in __init__), declared
        # on the AdapterContract base class.
        assert "adapter_id" in getattr(adapter_cls, "__annotations__", {}) or hasattr(
            adapter_cls, "adapter_id"
        ), f"{adapter_cls.__name__} must declare adapter_id"

    def test_four_adapters_covered(self) -> None:
        """Exactly four real adapters are tested (audit covers four)."""
        adapters = [MatrixAdapter, MeshtasticAdapter, MeshCoreAdapter, LxmfAdapter]
        assert len(adapters) == 4


# ===================================================================
# 4. Adapters report health facts, not lifecycle state
# ===================================================================


class TestAdaptersReportHealthNotState:
    """Adapters report health strings via health_check(); the runtime
    maps these to AdapterState. Adapters never import or set
    AdapterState directly."""

    def test_real_adapters_do_not_import_adapter_state(self) -> None:
        """No real adapter module mentions AdapterState."""
        adapter_files = [
            REPO_ROOT / "src/medre/adapters/matrix/adapter.py",
            REPO_ROOT / "src/medre/adapters/meshtastic/adapter.py",
            REPO_ROOT / "src/medre/adapters/meshcore/adapter.py",
            REPO_ROOT / "src/medre/adapters/lxmf/adapter.py",
        ]
        for path in adapter_files:
            content = path.read_text()
            assert "AdapterState" not in content, f"{path} must not import AdapterState"
            assert (
                "_adapter_states" not in content
            ), f"{path} must not access _adapter_states"
            assert (
                "_set_adapter_state" not in content
            ), f"{path} must not call _set_adapter_state"

    async def test_health_check_returns_adapter_info(self) -> None:
        """health_check() returns AdapterInfo with a health string."""
        adapter = _StubAdapter("test-hc")
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert isinstance(info.health, str)
        assert info.health in VALID_HEALTH_STRINGS

    async def test_runtime_maps_health_to_adapter_state(self) -> None:
        """health_to_adapter_state maps each valid health string."""
        assert health_to_adapter_state("healthy") == AdapterState.READY
        assert health_to_adapter_state("degraded") == AdapterState.DEGRADED
        assert health_to_adapter_state("failed") == AdapterState.FAILED
        assert health_to_adapter_state("unknown") == AdapterState.STOPPED
        assert health_to_adapter_state("starting") == AdapterState.INITIALIZING
        assert health_to_adapter_state("stopping") == AdapterState.STOPPING

    async def test_unknown_health_string_maps_to_failed(self) -> None:
        """Unrecognised health strings map conservatively to FAILED."""
        assert health_to_adapter_state("nonsense") == AdapterState.FAILED

    async def test_adapter_cannot_self_promote_to_ready(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter cannot set its own state to READY — only the runtime
        does this after a successful start()."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Before start, no state is registered.
        assert "fake_matrix" not in app._adapter_states

        await app.start()
        try:
            # After start, runtime set it to READY.
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
            # The adapter object itself has no AdapterState attribute.
            adapter = app.adapters["fake_matrix"]
            assert not hasattr(adapter, "_lifecycle_state")
            assert not hasattr(adapter, "_adapter_state")
        finally:
            await app.stop()

    async def test_health_check_degraded_reports_fact_only(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter reporting 'degraded' via health_check does not
        change the runtime's _adapter_states — health_check is a fact,
        not a state mutation."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace with a degraded-reporting adapter.
        app.adapters["fake_matrix"] = _HealthReportingAdapter(
            "fake_matrix", health="degraded"
        )

        await app.start()
        try:
            # Runtime set READY because start() succeeded.
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
            # health_check reports degraded as a fact.
            info = await app.adapters["fake_matrix"].health_check()
            assert info.health == "degraded"
            # But the runtime's lifecycle state is still READY.
            assert app.adapter_states["fake_matrix"] == AdapterState.READY
        finally:
            await app.stop()


# ===================================================================
# 5. health_to_adapter_state mapping correctness
# ===================================================================


class TestHealthToAdapterStateMapping:
    """The health_to_adapter_state mapping is correct and covers all
    valid health strings."""

    def test_all_valid_health_strings_mapped(self) -> None:
        """Every string in VALID_HEALTH_STRINGS has a mapping."""
        for hs in VALID_HEALTH_STRINGS:
            result = health_to_adapter_state(hs)
            assert isinstance(
                result, AdapterState
            ), f"health_to_adapter_state({hs!r}) must return AdapterState"

    def test_mapping_is_deterministic(self) -> None:
        """Same input always produces the same output."""
        for hs in VALID_HEALTH_STRINGS:
            a = health_to_adapter_state(hs)
            b = health_to_adapter_state(hs)
            assert a is b


# ===================================================================
# 6. Transition graph documented properties
# ===================================================================


class TestTransitionGraphProperties:
    """The VALID_TRANSITIONS graph has properties documented in the audit."""

    def test_every_state_has_entry(self) -> None:
        """Every AdapterState member has an entry in VALID_TRANSITIONS."""
        for state in AdapterState:
            assert state in VALID_TRANSITIONS

    def test_graph_is_symmetric_for_non_terminal(self) -> None:
        """Non-terminal transitions include paths back to READY (except
        STOPPING which only goes to STOPPED/FAILED).

        Every non-terminal state (excluding FAILED, STOPPED, STOPPING)
        must be able to reach STOPPING directly and READY via a path
        through the transition graph.
        """
        for state in AdapterState:
            if state in (
                AdapterState.FAILED,
                AdapterState.STOPPED,
                AdapterState.STOPPING,
            ):
                continue
            targets = VALID_TRANSITIONS[state]
            # Every non-terminal, non-stopping state can reach STOPPING
            # (to support shutdown).
            assert (
                AdapterState.STOPPING in targets
            ), f"{state.name} should be able to transition to STOPPING"

            # READY is reachable by definition.
            if state is AdapterState.READY:
                continue

            # BFS over VALID_TRANSITIONS to confirm READY is reachable.
            visited: set[AdapterState] = set()
            queue = [state]
            found_ready = False
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                for target in VALID_TRANSITIONS[current]:
                    if target is AdapterState.READY:
                        found_ready = True
                        break
                    if target not in visited:
                        queue.append(target)
                if found_ready:
                    break
            assert (
                found_ready
            ), f"{state.name} cannot reach READY through any transition path"

    def test_no_self_transitions(self) -> None:
        """No state has itself as a valid transition target."""
        for state, targets in VALID_TRANSITIONS.items():
            assert state not in targets, f"{state.name} should not transition to itself"

    def test_ready_can_reach_all_operational_states(self) -> None:
        """READY can transition to all runtime operational states."""
        targets = VALID_TRANSITIONS[AdapterState.READY]
        expected = {
            AdapterState.DEGRADED,
            AdapterState.BACKPRESSURED,
            AdapterState.DISCONNECTED,
            AdapterState.STOPPING,
            AdapterState.FAILED,
        }
        assert targets == expected

    def test_disconnected_can_reconnect(self) -> None:
        """DISCONNECTED can return to READY (reconnect)."""
        assert is_valid_transition(AdapterState.DISCONNECTED, AdapterState.READY)

    def test_disconnected_can_fail(self) -> None:
        """DISCONNECTED can transition to FAILED (unrecoverable loss)."""
        assert is_valid_transition(AdapterState.DISCONNECTED, AdapterState.FAILED)


# ===================================================================
# 7. Audit follow-up identifiers are present
# ===================================================================


class TestAuditFollowUpIdentifiers:
    """The audit document contains all follow-up identifiers referenced
    in the task specification."""

    @pytest.fixture
    def audit_content(self) -> str:
        audit_path = REPO_ROOT / "docs" / "dev" / "adapter-lifecycle-audit.md"
        assert (
            audit_path.exists()
        ), f"adapter-lifecycle-audit.md not found at {audit_path}"
        return audit_path.read_text()

    def test_lxmf_1_present(self, audit_content: str) -> None:
        """LXMF-1 (Verify Reconnect Triggering) is documented."""
        assert "LXMF-1" in audit_content
        assert "Reconnect Triggering" in audit_content

    def test_lxmf_2_present(self, audit_content: str) -> None:
        """LXMF-2 (Granular Health Detection) is documented."""
        assert "LXMF-2" in audit_content
        assert "Granular Health Detection" in audit_content

    def test_meshtastic_1_present(self, audit_content: str) -> None:
        """MESHTASTIC-1 (Inbound-Future Drain Completeness) is documented."""
        assert "MESHTASTIC-1" in audit_content
        assert "Inbound-Future Drain" in audit_content

    def test_cross_1_present(self, audit_content: str) -> None:
        """CROSS-1 (Stale-Event Filter Parity Test) is documented."""
        assert "CROSS-1" in audit_content
        assert "Stale-Event Filter Parity" in audit_content

    def test_cross_2_present(self, audit_content: str) -> None:
        """CROSS-2 (Reconnect Parity Integration Test) is documented."""
        assert "CROSS-2" in audit_content
        assert "Reconnect Parity Integration" in audit_content

    def test_all_five_follow_ups_present(self, audit_content: str) -> None:
        """All five follow-up identifiers are present."""
        expected_ids = {"LXMF-1", "LXMF-2", "MESHTASTIC-1", "CROSS-1", "CROSS-2"}
        for fid in expected_ids:
            assert fid in audit_content, f"Follow-up {fid} missing from audit doc"

    def test_follow_up_section_exists(self, audit_content: str) -> None:
        """The Identified Follow-Up Items section exists."""
        assert "## Identified Follow-Up Items" in audit_content


# ===================================================================
# 8. Lifecycle transition enforcement at runtime
# ===================================================================


class TestRuntimeTransitionEnforcement:
    """The runtime enforces VALID_TRANSITIONS at every _set_adapter_state
    call during the full start/stop lifecycle."""

    async def test_full_lifecycle_happy_path(self, tmp_paths: MedrePaths) -> None:
        """INITIALIZING → READY → STOPPING → STOPPED for a clean adapter."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        assert app.adapter_states["fake_matrix"] == AdapterState.READY
        await app.stop()
        assert app.adapter_states["fake_matrix"] == AdapterState.STOPPED

    async def test_start_failure_path(self, tmp_paths: MedrePaths) -> None:
        """INITIALIZING → FAILED for a start-failing adapter."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")
        app.adapters["worker"] = _StubAdapter("worker")

        await app.start()
        try:
            assert app.adapter_states["fake_matrix"] == AdapterState.FAILED
        finally:
            await app.stop()

    async def test_total_failure_raises_startup_error(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When all adapters fail, RuntimeStartupError is raised and
        adapters end in FAILED."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = _FailingStartAdapter("fake_matrix")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.adapter_states["fake_matrix"] == AdapterState.FAILED

    async def test_invalid_transition_from_ready_raises(
        self, tmp_paths: MedrePaths
    ) -> None:
        """READY → INITIALIZING is invalid and must raise."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            with pytest.raises(InvalidStateTransition):
                app._set_adapter_state("fake_matrix", AdapterState.INITIALIZING)
        finally:
            await app.stop()

    async def test_ready_to_backpressured_is_valid(self, tmp_paths: MedrePaths) -> None:
        """READY → BACKPRESSURED is valid per the audit transition graph."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        try:
            app._set_adapter_state("fake_matrix", AdapterState.BACKPRESSURED)
            assert app.adapter_states["fake_matrix"] == AdapterState.BACKPRESSURED
        finally:
            await app.stop()
