"""Pure, observational adapter runtime-status evidence helpers.

Provides :func:`build_adapter_status_evidence` — a pure function that
projects adapter configuration, lifecycle state, and health inputs into a
JSON-safe :class:`AdapterStatusEvidence` dataclass with a normalised
*operator status* string suitable for structured logging, diagnostic
bundles, and operator dashboards.

Operator status vocabulary
--------------------------
The :data:`OPERATOR_STATUSES` tuple defines the canonical set of
operator-facing status strings:

* ``"disabled"``        — adapter is disabled in configuration.
* ``"not_configured"``  — adapter is enabled but has no transport config.
* ``"configured"``      — adapter has config but has not yet entered the
                          lifecycle (pre-startup).
* ``"starting"``        — adapter is initialising (``INITIALIZING``).
* ``"connected"``       — adapter is ready and healthy (``READY``).
* ``"degraded"``        — adapter is partially functional (``DEGRADED``)
                          or back-pressured (``BACKPRESSURED``).
* ``"unavailable"``     — adapter has lost its transport connection
                          (``DISCONNECTED``).
* ``"stopping"``        — adapter is shutting down gracefully (``STOPPING``).
* ``"failed"``          — adapter has encountered an unrecoverable error
                          (``FAILED``).
* ``"stopped"``         — adapter has shut down cleanly (``STOPPED``).

Design constraints
------------------
* **Pure / observational** — no I/O, no async, no SDK imports, no side
  effects.  All inputs are explicit parameters.
* **Tolerant** — accepts ``dict``, dataclass, or ``None`` inputs for
  config, lifecycle state, and health.
* **Deterministic** — output is fully determined by inputs; no clocks,
  randomness, or hidden state.
* **No credential probing** — ``failure_category`` / ``failure_reason``
  are caller-supplied; this module never inspects secrets or performs
  live validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from medre.core.lifecycle.states import VALID_TRANSITIONS, AdapterState

# ---------------------------------------------------------------------------
# Operator status vocabulary
# ---------------------------------------------------------------------------

OPERATOR_STATUSES: tuple[str, ...] = (
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
)
"""Canonical operator-facing adapter status strings."""

OperatorStatus = Literal[
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
]
"""Type alias for valid operator status values."""

# Mapping from AdapterState.value to operator status string.
_STATE_TO_OPERATOR: dict[str, OperatorStatus] = {
    AdapterState.INITIALIZING.value: "starting",
    AdapterState.READY.value: "connected",
    AdapterState.DEGRADED.value: "degraded",
    AdapterState.BACKPRESSURED.value: "degraded",
    AdapterState.DISCONNECTED.value: "unavailable",
    AdapterState.STOPPING.value: "stopping",
    AdapterState.FAILED.value: "failed",
    AdapterState.STOPPED.value: "stopped",
}

# ---------------------------------------------------------------------------
# Internal helpers — tolerant attribute access
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read *name* from *obj* supporting both dicts and objects."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(obj: Any) -> str | None:
    """Extract a string value from an enum or return the string itself."""
    if obj is None:
        return None
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, str):
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# Input resolvers
# ---------------------------------------------------------------------------


def _resolve_enabled(config: Any) -> bool | None:
    """Derive the *enabled* flag from config (dict or dataclass)."""
    val = _attr(config, "enabled", None)
    if isinstance(val, bool):
        return val
    return None


def _resolve_configured(config: Any) -> bool | None:
    """Derive whether the adapter has a transport config object.

    Returns ``True`` when ``config.config`` (or ``config["config"]``) is
    not ``None``, ``False`` when it is explicitly ``None``, or ``None``
    when the config input itself is ``None``.
    """
    if config is None:
        return None
    inner = _attr(config, "config", None)
    if inner is None:
        # Distinguish "config key present but None" from "no config at all".
        if isinstance(config, dict) and "config" in config:
            return False
        if hasattr(config, "config"):
            # Attribute exists (even if None) → not configured.
            return False
        # No config attribute at all — cannot determine.
        return None
    return True


def _resolve_adapter_kind(config: Any) -> str | None:
    """Derive adapter kind (``"real"`` / ``"fake"``) from config."""
    val = _attr(config, "adapter_kind", None)
    if isinstance(val, str):
        return val
    return None


def _resolve_state_str(lifecycle_state: Any) -> str | None:
    """Normalise a lifecycle state to its string value."""
    return _enum_value(lifecycle_state)


def _resolve_health_str(health: Any) -> str | None:
    """Normalise health input to a string.

    Accepts:
    * ``None``        → ``None``
    * ``str``         → as-is
    * ``dict``        → ``health.get("health")``
    * object with ``.health`` attribute
    """
    if health is None:
        return None
    if isinstance(health, str):
        return health
    if isinstance(health, dict):
        h = health.get("health")
        return h if isinstance(h, str) else None
    h = getattr(health, "health", None)
    if isinstance(h, str):
        return h
    return None


def _resolve_connected(operator_status: OperatorStatus) -> bool | None:
    """Derive *connected* boolean from operator status.

    Returns ``True`` when connected, ``False`` when definitely not
    connected, or ``None`` when indeterminate (starting, configured, etc.).
    """
    if operator_status == "connected":
        return True
    if operator_status in (
        "unavailable",
        "failed",
        "stopped",
        "stopping",
        "disabled",
    ):
        return False
    # "starting", "configured", "not_configured", "degraded" — indeterminate.
    return None


def _resolve_valid_transitions(state_str: str | None) -> list[str] | None:
    """Derive valid transition targets from current state string.

    Returns a sorted list of target state value strings, or ``None``
    when the state is not recognised.
    """
    if state_str is None:
        return None
    try:
        state = AdapterState(state_str)
    except ValueError:
        return None
    targets = VALID_TRANSITIONS.get(state, frozenset())
    if not targets:
        return []
    return sorted(t.value for t in targets)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive_operator_status(
    *,
    enabled: bool | None,
    configured: bool | None,
    current_state: str | None,
) -> OperatorStatus:
    """Pure derivation of operator status from resolved inputs.

    Derivation priority (first match wins):

    1. ``enabled is False`` → ``"disabled"``
    2. ``configured is False`` → ``"not_configured"``
    3. ``current_state is None`` → ``"configured"`` (pre-startup)
    4. Map *current_state* through :data:`_STATE_TO_OPERATOR`
    5. Fallback → ``"configured"``

    Parameters
    ----------
    enabled:
        ``True`` if the adapter is enabled in config, ``False`` if
        explicitly disabled, ``None`` if unknown.
    configured:
        ``True`` if transport config is present, ``False`` if absent,
        ``None`` if unknown.
    current_state:
        Lifecycle state value string (e.g. ``"ready"``), or ``None``.

    Returns
    -------
    OperatorStatus
    """
    # Priority 1: explicitly disabled.
    if enabled is False:
        return "disabled"

    # Priority 2: enabled but no transport config.
    if configured is False:
        return "not_configured"

    # Priority 3: no lifecycle state observed yet (pre-startup).
    if current_state is None:
        return "configured"

    # Priority 4: map lifecycle state to operator status.
    mapped = _STATE_TO_OPERATOR.get(current_state)
    if mapped is not None:
        return mapped

    # Priority 5: fallback.
    return "configured"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterStatusEvidence:
    """JSON-safe, frozen evidence for a single adapter's runtime status.

    Constructed by :func:`build_adapter_status_evidence`.  No SDK
    objects, no secrets, no I/O.

    Attributes
    ----------
    adapter_id:
        Unique adapter identifier.
    transport:
        Transport type (``"matrix"``, ``"meshtastic"``, ``"meshcore"``,
        ``"lxmf"``), or ``None`` if not supplied.
    enabled:
        ``True`` if the adapter is enabled in config.
    configured:
        ``True`` if transport configuration is present.
    adapter_kind:
        ``"real"``, ``"fake"``, or ``None``.
    operator_status:
        Normalised operator-facing status string (one of
        :data:`OPERATOR_STATUSES`).
    current_state:
        Lifecycle state value string (e.g. ``"ready"``), or ``None``.
    health:
        Normalised health string, or ``None``.
    connected:
        ``True`` if connected, ``False`` if definitely not connected,
        ``None`` if indeterminate.
    failure_category:
        Caller-supplied failure category (e.g. ``"missing_credentials"``,
        ``"transport_error"``), or ``None``.
    failure_reason:
        Caller-supplied human-readable failure reason, or ``None``.
    valid_transitions:
        Sorted list of valid target state value strings from the current
        lifecycle state, or ``None`` when state is unknown.  Empty list
        for terminal states (``FAILED``, ``STOPPED``).
    """

    adapter_id: str
    transport: str | None = None
    enabled: bool | None = None
    configured: bool | None = None
    adapter_kind: str | None = None
    operator_status: OperatorStatus = "not_configured"
    current_state: str | None = None
    health: str | None = None
    connected: bool | None = None
    failure_category: str | None = None
    failure_reason: str | None = None
    valid_transitions: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation with sorted keys."""
        return {
            "adapter_id": self.adapter_id,
            "adapter_kind": self.adapter_kind,
            "configured": self.configured,
            "connected": self.connected,
            "current_state": self.current_state,
            "enabled": self.enabled,
            "failure_category": self.failure_category,
            "failure_reason": self.failure_reason,
            "health": self.health,
            "operator_status": self.operator_status,
            "transport": self.transport,
            "valid_transitions": (
                list(self.valid_transitions)
                if self.valid_transitions is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_adapter_status_evidence(
    adapter_id: str,
    *,
    config: Any | dict | None = None,
    lifecycle_state: Any | str | None = None,
    health: Any | str | dict | None = None,
    transport: str | None = None,
    failure_category: str | None = None,
    failure_reason: str | None = None,
) -> AdapterStatusEvidence:
    """Build :class:`AdapterStatusEvidence` from available inputs.

    Accepts config (dict or dataclass with ``enabled``, ``config``,
    ``adapter_kind``), lifecycle state (enum or string), health (string,
    dict, or object with ``.health``), and optional transport name and
    failure metadata.

    All parameters are optional except *adapter_id*.  Missing inputs are
    treated conservatively (``None`` / ``"configured"`` fallback).

    Parameters
    ----------
    adapter_id:
        Unique adapter identifier (required).
    config:
        Adapter runtime config (dict or dataclass).
    lifecycle_state:
        :class:`AdapterState` enum, state value string, or ``None``.
    health:
        Health string, dict with ``"health"`` key, or object with
        ``.health`` attribute.
    transport:
        Transport type string (``"matrix"``, ``"meshtastic"``, etc.).
    failure_category:
        Caller-supplied failure category string.
    failure_reason:
        Caller-supplied failure reason string.

    Returns
    -------
    AdapterStatusEvidence
    """
    enabled = _resolve_enabled(config)
    configured = _resolve_configured(config)
    adapter_kind = _resolve_adapter_kind(config)
    current_state = _resolve_state_str(lifecycle_state)
    health_str = _resolve_health_str(health)

    operator_status = derive_operator_status(
        enabled=enabled,
        configured=configured,
        current_state=current_state,
    )

    connected = _resolve_connected(operator_status)
    valid_transitions = _resolve_valid_transitions(current_state)

    return AdapterStatusEvidence(
        adapter_id=adapter_id,
        transport=transport,
        enabled=enabled,
        configured=configured,
        adapter_kind=adapter_kind,
        operator_status=operator_status,
        current_state=current_state,
        health=health_str,
        connected=connected,
        failure_category=failure_category,
        failure_reason=failure_reason,
        valid_transitions=valid_transitions,
    )
