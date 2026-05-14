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

This module also defines :class:`AdapterLiveHealth` and
:class:`LiveHealthSnapshot` — lightweight, frozen dataclasses that
represent per-adapter and aggregate live health from a single manual
refresh cycle.  These types are populated by
:meth:`~medre.runtime.app.MedreApp.refresh_live_health`; they do not
implement background polling, scheduling, or automatic refresh.

This module does **not** add background health polling, circuit breakers,
or auto-degrade logic.  Live health refresh is explicitly manual and
caller-initiated.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    AdapterState.STOPPED: "unknown",
}

# Lifecycle states that represent transitions only the runtime lifecycle
# can observe.  When present, these override the adapter's self-reported
# health because the adapter itself may not yet be running (or may
# already be shutting down).
_TRANSITIONAL_STATES: dict[AdapterState, str] = {
    AdapterState.INITIALIZING: "starting",
    AdapterState.STOPPING: "stopping",
}


# Reverse mapping from normalised health string to AdapterState.
# Used by live-health refresh to derive AdapterState values from
# health_check() results for aggregate runtime health classification.
_HEALTH_TO_ADAPTER_STATE: dict[str, AdapterState] = {
    "healthy": AdapterState.READY,
    "degraded": AdapterState.DEGRADED,
    "failed": AdapterState.FAILED,
    "unknown": AdapterState.STOPPED,
    "starting": AdapterState.INITIALIZING,
    "stopping": AdapterState.STOPPING,
}

# Maximum length for error strings stored in AdapterLiveHealth.
_MAX_LIVE_HEALTH_ERROR_LEN: int = 256


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def health_to_adapter_state(health: str) -> AdapterState:
    """Map a normalised health string to an :class:`AdapterState`.

    Used by live-health refresh to derive per-adapter lifecycle states
    from ``health_check()`` results.  Unrecognised health strings map
    conservatively to ``FAILED``.

    Parameters
    ----------
    health:
        One of :data:`VALID_HEALTH_STRINGS` (or any string).

    Returns
    -------
    AdapterState
    """
    return _HEALTH_TO_ADAPTER_STATE.get(health, AdapterState.FAILED)


def truncate_health_error(error: str) -> str:
    """Truncate an error string for safe storage in live-health records.

    Public helper used by the runtime to bound error strings before
    storing them in :class:`AdapterLiveHealth` entries.
    """
    if len(error) <= _MAX_LIVE_HEALTH_ERROR_LEN:
        return error
    return error[: _MAX_LIVE_HEALTH_ERROR_LEN - 3] + "..."


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
        Optional lifecycle state from
        :class:`~medre.core.lifecycle.states.AdapterState`.  When the
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
    # STOPPED is terminal: the adapter is no longer running so its
    # self-reported health is stale.  Override to "unknown".
    elif lifecycle_state is AdapterState.STOPPED:
        health = "unknown"

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


# ---------------------------------------------------------------------------
# Live health types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterLiveHealth:
    """Per-adapter live health result from a single ``health_check()`` call.

    Lightweight, JSON-safe, frozen dataclass.  No SDK objects, no secrets.
    Constructed by the runtime during ``refresh_live_health()`` by calling
    :meth:`~medre.adapters.base.BaseAdapter.health_check` and normalizing
    through :func:`normalize_adapter_health`.

    Per-adapter failures during refresh are isolated — a failure on one
    adapter is recorded in ``error`` and does not prevent other adapters
    from being polled.

    Attributes
    ----------
    adapter_id:
        Unique adapter identifier.
    health:
        One of :data:`VALID_HEALTH_STRINGS`.
    adapter_state:
        Lifecycle state inferred from the poll result.
    fake_or_live:
        ``"fake"``, ``"live"``, or ``"unknown"``.
    poll_timestamp_monotonic:
        ``time.monotonic()`` value at poll completion (seconds).
        Primary timestamp for ordering and deduplication.
    poll_timestamp_wall:
        ISO-8601 UTC string at poll completion.
        Human-readable timestamp for operators and log correlation.
    error:
        Non-``None`` when ``health_check()`` raised an exception.
        Contains the stringified error; never contains secrets.
    """

    adapter_id: str
    health: str
    adapter_state: AdapterState
    fake_or_live: str
    poll_timestamp_monotonic: float
    poll_timestamp_wall: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation."""
        return {
            "adapter_id": self.adapter_id,
            "adapter_state": self.adapter_state.value,
            "error": self.error,
            "fake_or_live": self.fake_or_live,
            "health": self.health,
            "poll_timestamp_monotonic": self.poll_timestamp_monotonic,
            "poll_timestamp_wall": self.poll_timestamp_wall,
        }


@dataclass(frozen=True)
class LiveHealthSnapshot:
    """Aggregate runtime live health from a single manual refresh cycle.

    Frozen, JSON-safe, deterministic.  Populated by
    ``MedreApp.refresh_live_health()`` which iterates adapters in
    deterministic order, calls ``health_check()``, reclassifies runtime
    health, and builds this snapshot.

    The snapshot is stored as ``MedreApp._live_health_state`` and
    read by :func:`~medre.runtime.snapshot.build_runtime_snapshot` to
    populate the ``health.live_health`` section.  Before the first
    refresh call, ``health.live_health`` is ``None``.

    There is no background polling loop, scheduler, or automatic
    refresh.  Refresh is explicitly manual and caller-initiated.

    Attributes
    ----------
    runtime_health:
        Classified :class:`~medre.core.runtime.supervision.RuntimeHealth`.
    adapter_summary:
        Counts by state category (``healthy``, ``degraded``, ``failed``,
        ``transitional``, ``total``).
    adapters:
        Per-adapter live health entries, keyed by adapter ID (sorted).
    poll_timestamp_monotonic:
        ``time.monotonic()`` at poll-cycle completion.
    poll_timestamp_wall:
        ISO-8601 UTC string at poll-cycle completion.
    poll_count:
        Monotonically increasing counter — one increment per successful
        poll cycle.  Used for quick staleness checks without float
        comparison.
    """

    runtime_health: str
    adapter_summary: dict[str, int]
    adapters: dict[str, AdapterLiveHealth]
    poll_timestamp_monotonic: float
    poll_timestamp_wall: str
    poll_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation with sorted keys."""
        return {
            "adapter_summary": dict(sorted(self.adapter_summary.items())),
            "adapters": {
                k: v.to_dict() for k, v in sorted(self.adapters.items())
            },
            "poll_count": self.poll_count,
            "poll_timestamp_monotonic": self.poll_timestamp_monotonic,
            "poll_timestamp_wall": self.poll_timestamp_wall,
            "runtime_health": self.runtime_health,
        }
