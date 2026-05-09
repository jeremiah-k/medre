"""Protocol-neutral adapter health normalization.

Provides a pure helper that projects :class:`~medre.adapters.base.AdapterInfo`
and optional :class:`~medre.core.lifecycle.states.AdapterState` into a
JSON-safe diagnostic dictionary with a fixed health vocabulary.

The six valid health strings are defined in :data:`VALID_HEALTH_STRINGS`:

* ``"healthy"`` – adapter is fully operational.
* ``"degraded"`` – adapter is partially functional.
* ``"failed"`` – adapter has encountered an unrecoverable error.
* ``"unknown"`` – adapter state cannot be determined.
* ``"starting"`` – adapter is being set up (lifecycle transitional).
* ``"stopping"`` – adapter is shutting down (lifecycle transitional).

This module does **not** add health polling, circuit breakers, or
auto-degrade logic.  It is a read-only projection for diagnostics.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.base import AdapterInfo
from medre.core.lifecycle.states import AdapterState
from medre.core.runtime.capabilities import serialize_adapter_capabilities

# ---------------------------------------------------------------------------
# Health vocabulary
# ---------------------------------------------------------------------------

VALID_HEALTH_STRINGS: frozenset[str] = frozenset(
    {
        "healthy",
        "degraded",
        "failed",
        "unknown",
        "starting",
        "stopping",
    }
)
"""The six protocol-neutral health strings used for diagnostics."""

# Mapping from AdapterState to normalized health string.
_STATE_TO_HEALTH: dict[AdapterState, str] = {
    AdapterState.INITIALIZING: "starting",
    AdapterState.READY: "healthy",
    AdapterState.DEGRADED: "degraded",
    AdapterState.BACKPRESSURED: "degraded",
    AdapterState.DISCONNECTED: "degraded",
    AdapterState.STOPPING: "stopping",
    AdapterState.FAILED: "failed",
}

# Lifecycle states that represent transitions only the lifecycle manager
# can observe.  When present, these override the adapter's self-reported
# health because the adapter itself may not yet be running (or may
# already be shutting down).
_TRANSITIONAL_STATES: dict[AdapterState, str] = {
    AdapterState.INITIALIZING: "starting",
    AdapterState.STOPPING: "stopping",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_adapter_mode(adapter: Any | None, info: AdapterInfo) -> str:
    """Infer ``"fake"``, ``"live"``, or ``"unknown"`` from existing signals.

    Detection strategy (checked in order):

    1. If *adapter* is provided and its class name starts with ``"Fake"`` →
       ``"fake"``.
    2. If *adapter* has a ``_config`` attribute with a ``connection_type``
       field equal to ``"fake"`` → ``"fake"``; any other value → ``"live"``.
    3. If ``info.platform`` starts with ``"fake_"`` or ``"faulty_"`` →
       ``"fake"``.
    4. Conservative fallback → ``"unknown"``.
    """
    if adapter is not None:
        cls_name = type(adapter).__name__
        if cls_name.startswith("Fake") or cls_name.startswith("Faulty"):
            return "fake"

        config = getattr(adapter, "_config", None)
        if config is not None:
            conn_type = getattr(config, "connection_type", None)
            if conn_type == "fake":
                return "fake"
            if conn_type is not None:
                return "live"

    platform = info.platform
    if platform.startswith("fake_") or platform.startswith("faulty_"):
        return "fake"

    return "unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_adapter_health(
    info: AdapterInfo,
    *,
    lifecycle_state: AdapterState | None = None,
    adapter: Any | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Normalize adapter health into a JSON-safe diagnostic dictionary.

    Accepts an :class:`~medre.adapters.base.AdapterInfo` (the canonical
    output of :meth:`~medre.adapters.base.BaseAdapter.health_check`) and
    optional lifecycle state or adapter reference, and returns a flat
    dictionary suitable for structured logging.

    Parameters
    ----------
    info:
        Fresh health snapshot from
        :meth:`~medre.adapters.base.BaseAdapter.health_check`.
    lifecycle_state:
        Optional lifecycle state from the
        :class:`~medre.core.lifecycle.manager.LifecycleManager`.  When the
        state is transitional (``INITIALIZING`` → ``"starting"``,
        ``STOPPING`` → ``"stopping"``) it overrides the adapter's
        self-reported health string.
    adapter:
        Optional adapter instance used to detect fake/live mode from
        class name or ``_config.connection_type``.
    details:
        Optional protocol-specific details to embed under the
        ``"details"`` key.  Values must be JSON-serialisable.

    Returns
    -------
    dict[str, Any]
        Dictionary with the following top-level keys:

        * ``adapter_id`` – unique adapter identifier.
        * ``platform`` – human-readable platform name.
        * ``role`` – adapter role as lowercase string.
        * ``health`` – one of :data:`VALID_HEALTH_STRINGS`.
        * ``fake_or_live`` – ``"fake"``, ``"live"``, or ``"unknown"``.
        * ``capabilities`` – deterministic, JSON-safe capability summary
          projected from :class:`~medre.adapters.base.AdapterCapabilities`.
          Contains only boolean, integer, and ``None`` values; no private
          state or transport objects.
        * ``details`` – dict with version, raw health strings, and any
          caller-supplied details.
    """
    # -- Derive health string -------------------------------------------
    health = info.health
    if health not in VALID_HEALTH_STRINGS:
        health = "unknown"

    # Transitional lifecycle states override adapter self-report.
    if lifecycle_state is not None and lifecycle_state in _TRANSITIONAL_STATES:
        health = _TRANSITIONAL_STATES[lifecycle_state]

    # -- Detect fake / live mode ----------------------------------------
    mode = _detect_adapter_mode(adapter, info)

    # -- Assemble details -----------------------------------------------
    out_details: dict[str, object] = {
        "version": info.version,
        "adapter_health_raw": info.health,
    }
    if lifecycle_state is not None:
        out_details["lifecycle_state_raw"] = lifecycle_state.value
    if details is not None:
        out_details.update(details)

    return {
        "adapter_id": info.adapter_id,
        "platform": info.platform,
        "role": info.role.value,
        "health": health,
        "fake_or_live": mode,
        "capabilities": serialize_adapter_capabilities(info.capabilities),
        "details": out_details,
    }
