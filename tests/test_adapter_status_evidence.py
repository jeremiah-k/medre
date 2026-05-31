"""Tests for adapter runtime-status evidence helpers.

Covers every operator status derivation path, input tolerance (dict vs
dataclass vs None), serialisation, and failure metadata propagation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from medre.core.evidence.adapter_status import (
    OPERATOR_STATUSES,
    build_adapter_status_evidence,
    derive_operator_status,
)
from medre.core.lifecycle.states import VALID_TRANSITIONS, AdapterState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    enabled: bool | None = True,
    adapter_kind: str | None = "real",
    has_config: bool = True,
) -> SimpleNamespace:
    """Build a minimal config namespace object."""
    return SimpleNamespace(
        enabled=enabled,
        adapter_kind=adapter_kind,
        config=SimpleNamespace() if has_config else None,
    )


def _config_dict(
    *,
    enabled: bool = True,
    adapter_kind: str = "real",
    has_config: bool = True,
) -> dict[str, Any]:
    """Build a minimal config dict."""
    return {
        "enabled": enabled,
        "adapter_kind": adapter_kind,
        "config": {"dummy": True} if has_config else None,
    }


# ---------------------------------------------------------------------------
# derive_operator_status — unit tests
# ---------------------------------------------------------------------------


class TestDeriveOperatorStatus:
    """Unit tests for the pure derivation function."""

    def test_disabled_when_enabled_false(self) -> None:
        assert (
            derive_operator_status(
                enabled=False,
                configured=True,
                current_state=None,
            )
            == "disabled"
        )

    def test_disabled_takes_priority_over_state(self) -> None:
        """Even if lifecycle says 'ready', disabled config wins."""
        assert (
            derive_operator_status(
                enabled=False,
                configured=True,
                current_state="ready",
            )
            == "disabled"
        )

    def test_not_configured_when_configured_false(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=False,
                current_state=None,
            )
            == "not_configured"
        )

    def test_not_configured_when_enabled_none(self) -> None:
        """enabled=None + configured=False → not_configured."""
        assert (
            derive_operator_status(
                enabled=None,
                configured=False,
                current_state=None,
            )
            == "not_configured"
        )

    def test_configured_when_no_state(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state=None,
            )
            == "configured"
        )

    def test_starting_from_initializing(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="initializing",
            )
            == "starting"
        )

    def test_connected_from_ready(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="ready",
            )
            == "connected"
        )

    def test_degraded_from_degraded_state(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="degraded",
            )
            == "degraded"
        )

    def test_degraded_from_backpressured_state(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="backpressured",
            )
            == "degraded"
        )

    def test_unavailable_from_disconnected(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="disconnected",
            )
            == "unavailable"
        )

    def test_stopping_from_stopping(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="stopping",
            )
            == "stopping"
        )

    def test_failed_from_failed(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="failed",
            )
            == "failed"
        )

    def test_stopped_from_stopped(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="stopped",
            )
            == "stopped"
        )

    def test_fallback_configured_for_unknown_state(self) -> None:
        assert (
            derive_operator_status(
                enabled=True,
                configured=True,
                current_state="mystery",
            )
            == "configured"
        )

    def test_all_none_inputs_fall_to_configured(self) -> None:
        assert (
            derive_operator_status(
                enabled=None,
                configured=None,
                current_state=None,
            )
            == "configured"
        )


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — disabled
# ---------------------------------------------------------------------------


class TestDisabled:
    """Adapter disabled in configuration."""

    def test_namespace_config(self) -> None:
        cfg = _config(enabled=False)
        ev = build_adapter_status_evidence("mx-1", config=cfg, transport="matrix")
        assert ev.operator_status == "disabled"
        assert ev.enabled is False
        assert ev.connected is False

    def test_dict_config(self) -> None:
        cfg = _config_dict(enabled=False)
        ev = build_adapter_status_evidence("mx-1", config=cfg, transport="matrix")
        assert ev.operator_status == "disabled"
        assert ev.enabled is False

    def test_no_config_no_state(self) -> None:
        """No config at all — enabled defaults to None, so not disabled."""
        ev = build_adapter_status_evidence("mx-1")
        assert ev.operator_status == "configured"
        assert ev.enabled is None


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — not_configured
# ---------------------------------------------------------------------------


class TestNotConfigured:
    """Adapter enabled but transport config is missing."""

    def test_config_none_in_namespace(self) -> None:
        cfg = _config(enabled=True, has_config=False)
        ev = build_adapter_status_evidence("mt-1", config=cfg, transport="meshtastic")
        assert ev.operator_status == "not_configured"
        assert ev.configured is False
        assert ev.enabled is True

    def test_config_none_in_dict(self) -> None:
        cfg = _config_dict(enabled=True, has_config=False)
        ev = build_adapter_status_evidence("mt-1", config=cfg, transport="meshtastic")
        assert ev.operator_status == "not_configured"
        assert ev.configured is False


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — configured (pre-startup)
# ---------------------------------------------------------------------------


class TestConfigured:
    """Adapter has config but no lifecycle state yet."""

    def test_configured_real(self) -> None:
        cfg = _config(enabled=True, adapter_kind="real", has_config=True)
        ev = build_adapter_status_evidence("mx-1", config=cfg, transport="matrix")
        assert ev.operator_status == "configured"
        assert ev.configured is True
        assert ev.adapter_kind == "real"
        assert ev.current_state is None
        assert ev.connected is None

    def test_configured_fake(self) -> None:
        cfg = _config(enabled=True, adapter_kind="fake", has_config=True)
        ev = build_adapter_status_evidence("mx-1", config=cfg, transport="matrix")
        assert ev.operator_status == "configured"
        assert ev.adapter_kind == "fake"


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — starting
# ---------------------------------------------------------------------------


class TestStarting:
    """Adapter is initialising."""

    def test_from_enum(self) -> None:
        cfg = _config(enabled=True)
        ev = build_adapter_status_evidence(
            "mx-1",
            config=cfg,
            lifecycle_state=AdapterState.INITIALIZING,
        )
        assert ev.operator_status == "starting"
        assert ev.current_state == "initializing"
        assert ev.connected is None
        # INITIALIZING can transition to READY, STOPPING, STOPPED, FAILED
        assert "ready" in ev.valid_transitions
        assert "failed" in ev.valid_transitions

    def test_from_string(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state="initializing",
        )
        assert ev.operator_status == "starting"
        assert ev.current_state == "initializing"


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — connected
# ---------------------------------------------------------------------------


class TestConnected:
    """Adapter is ready and healthy."""

    def test_from_ready_state(self) -> None:
        cfg = _config(enabled=True)
        ev = build_adapter_status_evidence(
            "mx-1",
            config=cfg,
            lifecycle_state=AdapterState.READY,
            health="healthy",
        )
        assert ev.operator_status == "connected"
        assert ev.connected is True
        assert ev.current_state == "ready"
        assert ev.health == "healthy"
        # READY can transition to DEGRADED, BACKPRESSURED, DISCONNECTED, STOPPING, FAILED
        assert "degraded" in ev.valid_transitions
        assert "disconnected" in ev.valid_transitions

    def test_from_string_state_and_dict_health(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state="ready",
            health={"health": "healthy"},
        )
        assert ev.operator_status == "connected"
        assert ev.health == "healthy"


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — unavailable / disconnected
# ---------------------------------------------------------------------------


class TestUnavailable:
    """Adapter has lost transport connection."""

    def test_from_disconnected_state(self) -> None:
        ev = build_adapter_status_evidence(
            "mc-1",
            lifecycle_state=AdapterState.DISCONNECTED,
            transport="meshcore",
        )
        assert ev.operator_status == "unavailable"
        assert ev.connected is False
        assert ev.current_state == "disconnected"

    def test_from_string(self) -> None:
        ev = build_adapter_status_evidence("mc-1", lifecycle_state="disconnected")
        assert ev.operator_status == "unavailable"


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — failed
# ---------------------------------------------------------------------------


class TestFailed:
    """Adapter has failed."""

    def test_from_failed_state(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.FAILED,
            health="failed",
            failure_category="transport_error",
            failure_reason="Connection refused",
        )
        assert ev.operator_status == "failed"
        assert ev.connected is False
        assert ev.current_state == "failed"
        assert ev.failure_category == "transport_error"
        assert ev.failure_reason == "Connection refused"
        # FAILED is terminal — no valid transitions.
        assert ev.valid_transitions == []

    def test_failure_metadata_without_state(self) -> None:
        """Failure category/reason can be supplied even without state."""
        ev = build_adapter_status_evidence(
            "mx-1",
            failure_category="missing_credentials",
            failure_reason="No Matrix access token configured",
        )
        assert ev.failure_category == "missing_credentials"
        assert ev.failure_reason == "No Matrix access token configured"


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — stopped
# ---------------------------------------------------------------------------


class TestStopped:
    """Adapter has shut down cleanly."""

    def test_from_stopped_state(self) -> None:
        ev = build_adapter_status_evidence(
            "lx-1",
            lifecycle_state=AdapterState.STOPPED,
            transport="lxmf",
        )
        assert ev.operator_status == "stopped"
        assert ev.connected is False
        assert ev.current_state == "stopped"
        # STOPPED is terminal.
        assert ev.valid_transitions == []


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — degraded / backpressured
# ---------------------------------------------------------------------------


class TestDegraded:
    """Adapter is partially functional."""

    def test_degraded_state(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.DEGRADED,
            health="degraded",
        )
        assert ev.operator_status == "degraded"
        assert ev.current_state == "degraded"
        assert ev.connected is None  # indeterminate

    def test_backpressured_state_maps_to_degraded(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.BACKPRESSURED,
            health="degraded",
        )
        assert ev.operator_status == "degraded"
        # current_state preserves the actual value.
        assert ev.current_state == "backpressured"
        assert ev.connected is None

    def test_backpressured_valid_transitions(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.BACKPRESSURED,
        )
        # BACKPRESSURED can transition to READY, DEGRADED, DISCONNECTED, STOPPING, FAILED
        assert "ready" in (ev.valid_transitions or [])
        assert "degraded" in (ev.valid_transitions or [])
        assert "disconnected" in (ev.valid_transitions or [])


# ---------------------------------------------------------------------------
# build_adapter_status_evidence — stopping
# ---------------------------------------------------------------------------


class TestStopping:
    """Adapter is shutting down."""

    def test_from_stopping_state(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.STOPPING,
        )
        assert ev.operator_status == "stopping"
        assert ev.connected is False
        assert ev.current_state == "stopping"
        # STOPPING → STOPPED, FAILED
        assert ev.valid_transitions == ["failed", "stopped"]


# ---------------------------------------------------------------------------
# Input tolerance tests
# ---------------------------------------------------------------------------


class TestInputTolerance:
    """Verify graceful handling of dict, dataclass, enum, and None inputs."""

    def test_health_from_string(self) -> None:
        ev = build_adapter_status_evidence("x", health="healthy")
        assert ev.health == "healthy"

    def test_health_from_dict(self) -> None:
        ev = build_adapter_status_evidence("x", health={"health": "degraded"})
        assert ev.health == "degraded"

    def test_health_from_namespace(self) -> None:
        ev = build_adapter_status_evidence("x", health=SimpleNamespace(health="failed"))
        assert ev.health == "failed"

    def test_health_none(self) -> None:
        ev = build_adapter_status_evidence("x", health=None)
        assert ev.health is None

    def test_lifecycle_from_enum(self) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state=AdapterState.READY)
        assert ev.current_state == "ready"

    def test_lifecycle_from_string(self) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state="failed")
        assert ev.current_state == "failed"

    def test_lifecycle_none(self) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state=None)
        assert ev.current_state is None

    def test_config_none(self) -> None:
        ev = build_adapter_status_evidence("x", config=None)
        assert ev.enabled is None
        assert ev.configured is None

    def test_config_dict_missing_keys(self) -> None:
        ev = build_adapter_status_evidence("x", config={})
        assert ev.enabled is None
        # No "config" key → configured is None (cannot determine).
        assert ev.configured is None

    def test_transport_preserved(self) -> None:
        ev = build_adapter_status_evidence("x", transport="lxmf")
        assert ev.transport == "lxmf"


# ---------------------------------------------------------------------------
# Serialisation tests
# ---------------------------------------------------------------------------


class TestSerialisation:
    """Verify to_dict() output structure and types."""

    def test_to_dict_keys_sorted(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            config=_config(enabled=True),
            lifecycle_state=AdapterState.READY,
            health="healthy",
            transport="matrix",
        )
        d = ev.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_to_dict_json_safe_types(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            config=_config(enabled=True),
            lifecycle_state=AdapterState.READY,
            health="healthy",
        )
        d = ev.to_dict()
        # All values must be str, int, float, bool, None, list, or dict.
        for v in d.values():
            assert isinstance(v, (str, int, float, bool, list, dict)) or v is None

    def test_to_dict_roundtrip(self) -> None:
        ev = build_adapter_status_evidence(
            "mt-1",
            config=_config(enabled=True, adapter_kind="fake"),
            lifecycle_state=AdapterState.INITIALIZING,
            health="starting",
            transport="meshtastic",
            failure_category="test",
            failure_reason="unit test",
        )
        d = ev.to_dict()
        assert d["adapter_id"] == "mt-1"
        assert d["operator_status"] == "starting"
        assert d["current_state"] == "initializing"
        assert d["transport"] == "meshtastic"
        assert d["adapter_kind"] == "fake"
        assert d["enabled"] is True
        assert d["configured"] is True
        assert d["failure_category"] == "test"
        assert d["failure_reason"] == "unit test"
        assert isinstance(d["valid_transitions"], list)


# ---------------------------------------------------------------------------
# OPERATOR_STATUSES constant
# ---------------------------------------------------------------------------


class TestOperatorStatusesConstant:
    """Verify the exported constant is complete."""

    def test_contains_all_expected(self) -> None:
        expected = {
            "disabled",
            "not_configured",
            "configured",
            "starting",
            "connected",
            "degraded",
            "unavailable",
            "stopping",
            "failed",
            "stopped",
        }
        assert set(OPERATOR_STATUSES) == expected

    def test_covers_all_adapter_states(self) -> None:
        """Every AdapterState maps to a known operator status."""
        for state in AdapterState:
            from medre.core.evidence.adapter_status import _STATE_TO_OPERATOR

            assert (
                state.value in _STATE_TO_OPERATOR
            ), f"AdapterState.{state.name} not mapped"


# ---------------------------------------------------------------------------
# Valid transitions coverage
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Verify valid_transitions for each lifecycle state."""

    @pytest.mark.parametrize(
        "state",
        list(AdapterState),
        ids=lambda s: s.name,
    )
    def test_transitions_match_lifecycle_graph(self, state: AdapterState) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state=state)
        expected = sorted(t.value for t in VALID_TRANSITIONS.get(state, frozenset()))
        assert ev.valid_transitions == expected

    def test_unknown_state_gives_none_transitions(self) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state="nonexistent_state")
        assert ev.valid_transitions is None

    def test_none_state_gives_none_transitions(self) -> None:
        ev = build_adapter_status_evidence("x", lifecycle_state=None)
        assert ev.valid_transitions is None


# ---------------------------------------------------------------------------
# Missing credentials / failure metadata
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    """Failure category/reason supplied by caller (no credential probing)."""

    def test_missing_credentials_on_failed_state(self) -> None:
        ev = build_adapter_status_evidence(
            "mx-1",
            lifecycle_state=AdapterState.FAILED,
            health="failed",
            failure_category="missing_credentials",
            failure_reason="No Matrix access token found in config",
        )
        assert ev.operator_status == "failed"
        assert ev.failure_category == "missing_credentials"
        assert "access token" in ev.failure_reason

    def test_missing_credentials_on_configured_state(self) -> None:
        """Failure metadata can be attached before runtime failure."""
        ev = build_adapter_status_evidence(
            "mx-1",
            config=_config(enabled=True, has_config=True),
            failure_category="missing_credentials",
            failure_reason="LXMF identity key not configured",
        )
        assert ev.operator_status == "configured"
        assert ev.failure_category == "missing_credentials"

    def test_no_failure_metadata_by_default(self) -> None:
        ev = build_adapter_status_evidence("x")
        assert ev.failure_category is None
        assert ev.failure_reason is None


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


class TestFrozen:
    """Verify immutability."""

    def test_frozen_raises_on_setattr(self) -> None:
        ev = build_adapter_status_evidence("x")
        with pytest.raises(AttributeError):
            ev.adapter_id = "y"  # type: ignore[misc]
