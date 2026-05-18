"""Tests for adapter lifecycle state machine terminal semantics.

Covers:
- STOPPING→STOPPED is a valid clean-stop transition.
- STOPPING→FAILED is valid (error during shutdown).
- STOPPED is terminal: no outgoing transitions.
- FAILED is terminal: no outgoing transitions.
- FAILED→INITIALIZING is now invalid (restart-like transition removed).
- STOPPED health maps to "unknown".
- STOPPED counts as dead for runtime health classification.
"""

from __future__ import annotations

import pytest

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterInfo,
    AdapterRole,
)
from medre.core.lifecycle.states import (
    VALID_TRANSITIONS,
    AdapterState,
    InvalidStateTransition,
    is_valid_transition,
    require_valid_transition,
)
from medre.core.runtime.health import normalize_adapter_health
from medre.core.runtime.supervision import (
    RuntimeHealth,
    classify_runtime_health,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_info(**overrides: object) -> AdapterInfo:
    defaults = {
        "adapter_id": "test-adapter",
        "platform": "test_platform",
        "role": AdapterRole.TRANSPORT,
        "version": "0.1.0",
        "capabilities": AdapterCapabilities(),
        "health": "healthy",
    }
    defaults.update(overrides)
    return AdapterInfo(**defaults)  # type: ignore[arg-type]


# ===================================================================
# Terminal semantics: STOPPED
# ===================================================================


class TestStoppedIsTerminal:
    """STOPPED has no outgoing transitions."""

    def test_stopped_has_empty_transitions(self) -> None:
        assert VALID_TRANSITIONS[AdapterState.STOPPED] == frozenset()

    def test_stopped_to_any_is_invalid(self) -> None:
        for target in AdapterState:
            if target is AdapterState.STOPPED:
                continue
            assert not is_valid_transition(
                AdapterState.STOPPED, target
            ), f"STOPPED→{target.value} should be invalid"

    def test_stopped_to_initializing_raises(self) -> None:
        with pytest.raises(InvalidStateTransition):
            require_valid_transition(AdapterState.STOPPED, AdapterState.INITIALIZING)

    def test_stopped_to_ready_raises(self) -> None:
        with pytest.raises(InvalidStateTransition):
            require_valid_transition(AdapterState.STOPPED, AdapterState.READY)


# ===================================================================
# Terminal semantics: FAILED
# ===================================================================


class TestFailedIsTerminal:
    """FAILED has no outgoing transitions."""

    def test_failed_has_empty_transitions(self) -> None:
        assert VALID_TRANSITIONS[AdapterState.FAILED] == frozenset()

    def test_failed_to_any_is_invalid(self) -> None:
        for target in AdapterState:
            if target is AdapterState.FAILED:
                continue
            assert not is_valid_transition(
                AdapterState.FAILED, target
            ), f"FAILED→{target.value} should be invalid"

    def test_failed_to_initializing_raises(self) -> None:
        """FAILED→INITIALIZING was previously valid; now invalid (no restart)."""
        with pytest.raises(InvalidStateTransition):
            require_valid_transition(AdapterState.FAILED, AdapterState.INITIALIZING)

    def test_failed_to_ready_raises(self) -> None:
        with pytest.raises(InvalidStateTransition):
            require_valid_transition(AdapterState.FAILED, AdapterState.READY)


# ===================================================================
# STOPPING transitions
# ===================================================================


class TestStoppingTransitions:
    """STOPPING can transition to STOPPED (clean) or FAILED (error)."""

    def test_stopping_to_stopped(self) -> None:
        assert is_valid_transition(AdapterState.STOPPING, AdapterState.STOPPED)

    def test_stopping_to_failed(self) -> None:
        assert is_valid_transition(AdapterState.STOPPING, AdapterState.FAILED)

    def test_stopping_to_stopped_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.STOPPING, AdapterState.STOPPED)

    def test_stopping_to_failed_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.STOPPING, AdapterState.FAILED)

    def test_stopping_to_ready_is_invalid(self) -> None:
        assert not is_valid_transition(AdapterState.STOPPING, AdapterState.READY)

    def test_stopping_to_initializing_is_invalid(self) -> None:
        assert not is_valid_transition(AdapterState.STOPPING, AdapterState.INITIALIZING)


# ===================================================================
# INITIALIZING transitions
# ===================================================================


class TestInitializingTransitions:
    """INITIALIZING can transition to READY, STOPPING, STOPPED, or FAILED."""

    def test_initializing_to_ready(self) -> None:
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.READY)

    def test_initializing_to_failed(self) -> None:
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.FAILED)

    def test_initializing_to_stopping(self) -> None:
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.STOPPING)

    def test_initializing_to_stopped(self) -> None:
        assert is_valid_transition(AdapterState.INITIALIZING, AdapterState.STOPPED)

    def test_initializing_to_ready_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.INITIALIZING, AdapterState.READY)

    def test_initializing_to_failed_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.INITIALIZING, AdapterState.FAILED)

    def test_initializing_to_stopping_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.INITIALIZING, AdapterState.STOPPING)

    def test_initializing_to_stopped_does_not_raise(self) -> None:
        require_valid_transition(AdapterState.INITIALIZING, AdapterState.STOPPED)

    def test_initializing_to_running_is_invalid(self) -> None:
        assert not is_valid_transition(
            AdapterState.INITIALIZING, AdapterState.BACKPRESSURED
        )

    def test_initializing_to_degraded_is_invalid(self) -> None:
        assert not is_valid_transition(AdapterState.INITIALIZING, AdapterState.DEGRADED)

    def test_initializing_to_disconnected_is_invalid(self) -> None:
        assert not is_valid_transition(
            AdapterState.INITIALIZING, AdapterState.DISCONNECTED
        )


# ===================================================================
# Health mapping for STOPPED
# ===================================================================


class TestStoppedHealthMapping:
    """STOPPED lifecycle state maps to "unknown" health."""

    def test_stopped_health_is_unknown(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(info, lifecycle_state=AdapterState.STOPPED)
        assert result["health"] == "unknown"

    def test_stopped_overrides_adapter_self_report(self) -> None:
        """Even if adapter reports healthy, STOPPED state overrides."""
        info = _make_info(health="healthy")
        result = normalize_adapter_health(info, lifecycle_state=AdapterState.STOPPED)
        assert result["health"] == "unknown"


# ===================================================================
# Supervision: STOPPED counts as dead
# ===================================================================


class TestStoppedInRuntimeHealth:
    """STOPPED adapters are classified as dead (unavailable) for runtime health."""

    def test_single_stopped_is_failed(self) -> None:
        assert classify_runtime_health([AdapterState.STOPPED]) == RuntimeHealth.FAILED

    def test_all_stopped_is_failed(self) -> None:
        assert (
            classify_runtime_health([AdapterState.STOPPED, AdapterState.STOPPED])
            == RuntimeHealth.FAILED
        )

    def test_ready_plus_stopped_is_degraded(self) -> None:
        assert (
            classify_runtime_health([AdapterState.READY, AdapterState.STOPPED])
            == RuntimeHealth.DEGRADED
        )

    def test_mixed_failed_and_stopped_is_failed(self) -> None:
        assert (
            classify_runtime_health([AdapterState.FAILED, AdapterState.STOPPED])
            == RuntimeHealth.FAILED
        )

    def test_ready_failed_stopped_is_degraded(self) -> None:
        assert (
            classify_runtime_health(
                [AdapterState.READY, AdapterState.FAILED, AdapterState.STOPPED]
            )
            == RuntimeHealth.DEGRADED
        )


# ===================================================================
# AdapterState enum completeness
# ===================================================================


class TestAdapterStateEnum:
    """AdapterState has all expected members."""

    def test_has_stopped(self) -> None:
        assert AdapterState.STOPPED.value == "stopped"

    def test_member_count(self) -> None:
        assert len(AdapterState) == 8

    def test_all_states_have_transitions(self) -> None:
        """Every AdapterState member has an entry in VALID_TRANSITIONS."""
        for state in AdapterState:
            assert (
                state in VALID_TRANSITIONS
            ), f"{state.value} missing from VALID_TRANSITIONS"
