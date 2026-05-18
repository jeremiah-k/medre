"""Deterministic runtime snapshot builder for the MEDRE framework.

Provides :func:`build_runtime_snapshot` which reads the current state of a
:class:`~medre.runtime.app.MedreApp` and returns a plain-dict, JSON-safe,
deterministic snapshot.  No SDK objects, no secrets, no async I/O.

Snapshot schema (``schema_version`` 1)
--------------------------------------
Top-level keys are alphabetically sorted for stable serialisation.
The snapshot is structured into intentional sections that separate
stable operator-facing data from unstable/debug internals::

    {
      "schema_version": 1,
      "snapshot_at": str,
      "accounting": {
        "counters": {...} | null,
        "live_refresh": false,
        "scope": "process_local",
      },
      "adapters": {adapter_id: {..., "provenance": "startup"}},
      "capacity": {
        "live_refresh": false,
        "scope": "process_local",
        "state": {...} | null,
      },
      "diagnostics": {
        "live_refresh": false,
        "runtime_events": {...} | null,
        "scope": "process_local",
      },
      "health": {
        "live_health": null,
        "live_refresh": false,
        "scope": "startup",
      },
      "identity": {},
      "lifecycle": {
        "adapters": {adapter_id: str},
        "live_refresh": false,
        "runtime_state": str,
        "scope": "process_local",
        "startup_timestamp": str | null,
        "uptime_seconds": float | null,
      },
      "limits": {...},
      "persistence": {},
      "replay": {"available": bool, "counters": {...} | null},
      "routes": {
        "build_readiness": {...} | null,
        "eligibility": {...} | null,
        "startup_readiness": {...} | null,
        "stats": {
          "live_refresh": false,
          "per_route": {route_id: {...}},
          "scope": "process_local",
        },
      },
      "startup": {
        "boot_summary": {...} | null,
        "build_failures": [...],
        "live_refresh": false,
        "scope": "startup",
        "startup_health": {...} | null,
      },
      "unstable": {},
    }

Key guarantees:

* Deterministic key ordering (sorted dicts at every level).
* No SDK objects — only plain ``dict`` / ``list`` / ``str`` / ``int`` /
  ``float`` / ``bool`` / ``None``.
* No secrets — adapter configs are never introspected.
* Bounded size — adapter and route collections are capped.
* Graceful degradation — absent optional subsystems report ``null``
  rather than raising.

Section purpose
---------------
identity:
    Reserved for future runtime identity metadata (node identity,
    signing keys, provenance).  Currently always ``{}``.
lifecycle:
    Runtime state transitions, startup timing, and uptime.
startup:
    One-time boot summary, health classification, and build failures.
health:
    Health assessment surfaces. ``live_health`` is ``null``
    until :meth:`~medre.runtime.app.MedreApp.refresh_live_health` is
    called; after the first successful refresh it contains a dict with
    per-adapter live health, aggregate classification, and poll metadata.
    ``startup_health`` remains frozen from startup and is not mutated.

    **Live health shape** (populated when ``refresh_live_health()``
    is called)::

        {
          "runtime_health": str,                    # RuntimeHealth value
          "adapter_summary": {                      # counts by category
            "healthy": int, "degraded": int,
            "failed": int, "transitional": int, "total": int,
          },
          "adapters": {                             # per-adapter live health
            adapter_id: {
              "adapter_id": str,
              "adapter_state": str,
              "error": str | null,
              "fake_or_live": str,
              "health": str,                        # VALID_HEALTH_STRINGS
              "poll_timestamp_monotonic": float,
              "poll_timestamp_wall": str,            # ISO-8601 UTC
            },
          },
          "poll_count": int,                        # monotonic counter
          "poll_timestamp_monotonic": float,
          "poll_timestamp_wall": str,                # ISO-8601 UTC
        }

    When populated, ``health.scope`` changes from ``"startup"`` to
    ``"live"`` and ``health.live_refresh`` changes from ``false`` to
    ``true``.  This is an additive change per Contract 63 §4.2
    (``null`` → ``dict`` is non-breaking).  No ``schema_version``
    bump is required.  Calling :meth:`~medre.runtime.app.MedreApp.refresh_live_health`
    triggers this transition.

    The types backing this shape are defined in
    :class:`~medre.core.runtime.health.AdapterLiveHealth` and
    :class:`~medre.core.runtime.health.LiveHealthSnapshot`.
adapters:
    Per-adapter static metadata (capabilities, role, version, health).
routes:
    route delivery stats, eligibility, per-route build readiness, and
    startup-derived readiness.  Each sub-section carries explicit
    ``scope`` and ``live_refresh`` metadata so operators can distinguish
    build-time facts from startup-time facts from live state.
persistence:
    Reserved for future durable-storage status (last-persisted event
    ID, storage health, queue depths).  Currently always ``{}``.
accounting:
    Bounded runtime event counters.  Carries ``scope="process_local"``
    and ``live_refresh=false`` (counters evolve via local runtime state
    transitions).
replay:
    Replay engine availability and counters.
capacity:
    In-flight delivery and replay capacity state.  Carries
    ``scope="process_local"`` and ``live_refresh=false`` (state evolves
    via local runtime transitions).
diagnostics:
    Internal debug/diagnostic surfaces (runtime events buffer).
    Shape may change without a schema version bump.
unstable:
    Reserved for debug/internal data that may evolve freely.
    Content is JSON-safe and bounded.  Currently always ``{}``.

Health freshness and provenance:

Each section carries ``scope`` and ``live_refresh`` metadata (except
reserved/identity/persistence sections).  Operators can determine whether
a value is startup-derived, process-local, or live:

* ``scope``: one of ``"build"``, ``"startup"``, ``"process_local"``,
  ``"live"``.  Indicates *when* the data was captured or computed.
  ``"build"`` = computed once during build (pre-startup); ``"startup"`` =
  computed once during start; ``"process_local"`` = in-memory state at
  snapshot time; ``"live"`` = actively polled from external adapters.
* ``live_refresh``: ``true`` if MEDRE actively calls
  ``adapter.health_check()`` or a transport API to get current state;
  ``false`` if data evolves only from local runtime state transitions
  or was frozen at build/startup.

Section-level provenance:

* ``startup``: ``scope="startup"``, ``live_refresh=false``.  Computed once
  during ``MedreApp.start()`` and frozen.
* ``health``: ``scope="startup"``, ``live_refresh=false`` before first
  refresh; ``scope="live"``, ``live_refresh=true`` after
  :meth:`~medre.runtime.app.MedreApp.refresh_live_health` is called.
  The ``startup_health`` value is a startup-derived snapshot frozen at
  startup; ``live_health`` transitions from ``null`` to a dict on first
  refresh.
* ``lifecycle``: ``scope="process_local"``, ``live_refresh=false``.  State
  values reflect the runtime's current in-process state at snapshot time.
  ``uptime_seconds`` is computed from the monotonic clock on each call.
  Per-adapter lifecycle states in ``lifecycle.adapters`` track the runtime's
  in-memory adapter state registry.
* ``diagnostics``: ``scope="process_local"``, ``live_refresh=false``.  The
  runtime event buffer grows as events are emitted from local runtime
  state transitions — not from external adapter polling.
* ``routes.*``: each sub-section has its own ``scope`` and ``live_refresh``
  (see Contract 63 §5.4).
* Per-adapter entries in ``adapters`` carry ``provenance: "startup"``,
  indicating that adapter metadata (including ``health``) is captured
  during startup and is not automatically refreshed at runtime.

Public symbols
--------------
* :func:`build_runtime_snapshot` — main entry point.
* :data:`SCHEMA_VERSION` — current snapshot schema version.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from medre.observability.sanitization import sanitize_error as _sanitize_error

if TYPE_CHECKING:
    pass

__all__ = ["build_runtime_snapshot", "SCHEMA_VERSION"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Current snapshot schema version.  Frozen at 1 during pre-release; internal
breaking changes update tests and docs but do not bump the version."""

_MAX_ADAPTERS: int = 256
"""Upper bound on the number of adapter entries included in a snapshot."""

_MAX_ROUTES: int = 1024
"""Upper bound on the number of route entries included in a snapshot."""

_MAX_BUILD_FAILURES: int = 64
"""Upper bound on build-failure entries."""

_MAX_ERROR_DETAIL_LEN: int = 512
"""Truncation limit for error strings inside the snapshot."""

# Sentinel for "this subsystem is not yet available".
_NOT_AVAILABLE: dict[str, str] = {"status": "not_available"}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sorted_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with keys sorted alphabetically (recursive)."""
    result: dict[str, Any] = {}
    for key in sorted(d):
        val = d[key]
        if isinstance(val, dict):
            result[key] = _sorted_dict(val)
        elif isinstance(val, list):
            result[key] = [_sorted_dict(v) if isinstance(v, dict) else v for v in val]
        else:
            result[key] = val
    return result


def _utc_iso(dt: datetime) -> str:
    """Return an ISO-8601 string for a UTC datetime."""
    return dt.isoformat()


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


def _monotonic_now() -> float:
    """Return a monotonic timestamp in seconds."""
    import time as _time

    return _time.monotonic()


# ---------------------------------------------------------------------------
# Adapter snapshot
# ---------------------------------------------------------------------------


def _snapshot_adapter(adapter: Any) -> dict[str, Any]:
    """Extract JSON-safe adapter metadata from a :class:`AdapterContract`.

    Reads only static attributes; does **not** call
    :meth:`~medre.core.contracts.adapter.AdapterContract.health_check` (which is async).

    Each adapter entry includes a ``provenance`` field set to ``"startup"``,
    indicating that adapter metadata (including ``health``) is captured during
    startup and is not automatically refreshed at runtime.
    """
    try:
        adapter_id = getattr(adapter, "adapter_id", "unknown")
        if adapter_id is None:
            adapter_id = "unknown"
    except Exception:
        adapter_id = "unknown"

    try:
        platform = getattr(adapter, "platform", "unknown")
        if platform is None:
            platform = "unknown"
    except Exception:
        platform = "unknown"

    # Role — may be an enum or a string.
    try:
        role_attr = getattr(adapter, "role", None)
    except Exception:
        role_attr = None
    if role_attr is not None and hasattr(role_attr, "value"):
        role: str = role_attr.value
    elif role_attr is not None:
        role = str(role_attr)
    else:
        role = "unknown"

    # Version — adapters typically don't expose this as a class attribute;
    # it lives on the AdapterInfo returned by health_check.  Try common names.
    try:
        version = getattr(adapter, "_version", None)
        if version is None:
            version = getattr(adapter, "version", "unknown")
    except Exception:
        version = "unknown"
    version = str(version)

    # Capabilities — may be stored as _capabilities (AdapterCapabilities).
    try:
        caps_raw = getattr(adapter, "_capabilities", None)
    except Exception:
        caps_raw = None
    caps: dict[str, Any]
    if caps_raw is not None and dataclasses.is_dataclass(caps_raw):
        caps = {}
        for f in dataclasses.fields(caps_raw):
            val = getattr(caps_raw, f.name)
            # Convert enums to their values.
            if hasattr(val, "value"):
                val = val.value
            caps[f.name] = val
    else:
        caps = {}

    # Health — static snapshot from startup, not live.  Uses the
    # adapter's _last_health attribute (set during build/startup) rather
    # than calling async health_check().  Health values are startup-derived
    # unless explicitly refreshed by an external caller.
    try:
        health = getattr(adapter, "_last_health", "unknown")
    except Exception:
        health = "unknown"

    return {
        "adapter_id": adapter_id,
        "capabilities": _sorted_dict(caps),
        "health": health,
        "platform": platform,
        "provenance": "startup",
        "role": role,
        "version": version,
    }


# ---------------------------------------------------------------------------
# Limits snapshot
# ---------------------------------------------------------------------------


def _snapshot_limits(limits: Any) -> dict[str, Any]:
    """Extract JSON-safe runtime limits from a :class:`RuntimeLimits`."""
    if limits is None:
        return {}
    result: dict[str, Any] = {}
    if dataclasses.is_dataclass(limits):
        for f in dataclasses.fields(limits):
            result[f.name] = getattr(limits, f.name)
    return _sorted_dict(result)


# ---------------------------------------------------------------------------
# Build-failure snapshot
# ---------------------------------------------------------------------------


def _snapshot_build_failures(failures: list[Any]) -> list[dict[str, Any]]:
    """Extract JSON-safe build-failure records."""
    entries: list[dict[str, Any]] = []
    for bf in failures[:_MAX_BUILD_FAILURES]:
        adapter_id = getattr(bf, "adapter_id", "unknown")
        error = getattr(bf, "error", "unknown error")
        error_str = _sanitize_error(str(error))
        entries.append(
            {
                "adapter_id": adapter_id,
                "error": error_str,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_runtime_snapshot(
    app: Any,
    *,
    now_fn: Callable[[], datetime] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, JSON-safe runtime snapshot.

    Parameters
    ----------
    app:
        A :class:`~medre.runtime.app.MedreApp` (or any object with a
        compatible interface).  Typed as ``Any`` to avoid hard imports
        and circular dependency; structural typing is used throughout.
    now_fn:
        Callable returning the current UTC :class:`~datetime.datetime`.
        Defaults to :func:`datetime.now(timezone.utc)`.  Inject a fixed
        clock for deterministic testing.
    monotonic_fn:
        Callable returning a monotonic float in **seconds**.
        Defaults to :func:`time.monotonic`.  Inject a fixed value for
        deterministic testing.

    Returns
    -------
    dict[str, Any]
        A deterministic snapshot with alphabetically sorted keys at
        every level.  Safe for ``json.dumps(sort_keys=True)``.

    Notes
    -----
    This function performs **no I/O** and calls **no async methods**.
    It reads only synchronous attributes and properties.  Adapter health
    is reported as ``"unknown"`` unless the adapter exposes a
    ``_last_health`` attribute; live health checks are the
     responsibility of the caller via
     :meth:`~medre.runtime.app.MedreApp.refresh_live_health`.

    **Health freshness:** The ``startup_health`` field (inside the
    ``startup`` section) reflects the latest runtime-owned health
    snapshot, which is currently initialized at startup and not
    automatically refreshed.  Adapter-level health values come from
    the adapter's ``_last_health`` attribute (static, set during build)
    rather than from live ``health_check()`` calls.  The ``live_health``
    field (inside ``health`` section) is ``null`` until
    :meth:`~medre.runtime.app.MedreApp.refresh_live_health` is called;
    after the first successful refresh it contains a dict with live
    per-adapter health data.  The ``startup_health`` value is frozen
    at startup and is **not** mutated by live health refresh.
    """
    _now = now_fn or _now_utc
    _mono = monotonic_fn or _monotonic_now

    snapshot_at = _utc_iso(_now())

    # -- Lifecycle section ---------------------------------------------------
    state_attr = getattr(app, "state", None)
    if state_attr is not None and hasattr(state_attr, "value"):
        runtime_state: str = state_attr.value
    elif state_attr is not None:
        runtime_state = str(state_attr)
    else:
        runtime_state = "unknown"

    # -- Startup timestamp & uptime ------------------------------------------
    startup_wall: str | None = getattr(app, "_startup_wall", None)
    startup_mono: float | None = getattr(app, "_startup_monotonic", None)

    uptime_seconds: float | None = None
    if startup_mono is not None:
        try:
            uptime_seconds = round(_mono() - startup_mono, 6)
            if uptime_seconds < 0:
                uptime_seconds = 0.0
        except Exception:
            uptime_seconds = None

    lifecycle: dict[str, Any] = {
        "runtime_state": runtime_state,
        "scope": "process_local",
        "live_refresh": False,
        "startup_timestamp": startup_wall,
        "uptime_seconds": uptime_seconds,
    }

    # -- Per-adapter lifecycle states (sorted deterministically) --------------
    adapter_lifecycle_states: dict[str, str] = {}
    adapter_states_raw: Any = getattr(app, "_adapter_states", None)
    if adapter_states_raw is not None:
        for aid in sorted(adapter_states_raw.keys()):
            state = adapter_states_raw[aid]
            adapter_lifecycle_states[aid] = (
                state.value if hasattr(state, "value") else str(state)
            )
    lifecycle["adapters"] = adapter_lifecycle_states

    # -- Adapters (sorted, bounded) ------------------------------------------
    adapters_raw: dict[str, Any] = getattr(app, "adapters", {}) or {}
    adapters: dict[str, dict[str, Any]] = {}
    for adapter_id in sorted(adapters_raw.keys())[:_MAX_ADAPTERS]:
        adapters[adapter_id] = _snapshot_adapter(adapters_raw[adapter_id])

    # -- Build failures (bounded) --------------------------------------------
    build_failures_raw: list[Any] = getattr(app, "build_failures", [])
    build_failures = _snapshot_build_failures(build_failures_raw)

    # -- Routes (from route_stats) -------------------------------------------
    route_stats: Any = getattr(app, "route_stats", None)
    routes_snapshot: dict[str, Any]
    if route_stats is not None and hasattr(route_stats, "snapshot"):
        raw_routes = route_stats.snapshot()
        # Apply bound and sort.
        routes_snapshot = {}
        for rid in sorted(raw_routes.keys())[:_MAX_ROUTES]:
            routes_snapshot[rid] = raw_routes[rid]
    else:
        routes_snapshot = {}

    # Wrap routes.stats with provenance metadata.
    routes_stats_with_provenance: dict[str, Any] = {
        "live_refresh": False,
        "per_route": routes_snapshot,
        "scope": "process_local",
    }

    # -- Replay availability & counters --------------------------------------
    replay_engine: Any = getattr(app, "_replay_engine", None)
    replay_available = replay_engine is not None

    # Try to get replay counters from an observability collector if wired.
    replay_counters: dict[str, Any] | None = None
    diag: Any = getattr(app, "_diagnostics_collector", None)
    if diag is None:
        diag = getattr(app, "diagnostician", None)
    if diag is not None and hasattr(diag, "snapshot"):
        try:
            diag_snap = diag.snapshot()
            # The diagnostics snapshot has a "replay" key.
            replay_counters = diag_snap.get("replay")
        except Exception:
            replay_counters = None

    # -- Capacity state ------------------------------------------------------
    cap_ctrl: Any = getattr(app, "_capacity_controller", None)
    capacity_snapshot: dict[str, Any] | None
    if cap_ctrl is not None and hasattr(cap_ctrl, "snapshot"):
        try:
            capacity_snapshot = cap_ctrl.snapshot()
        except Exception:
            _logger.warning("Capacity controller snapshot() failed", exc_info=True)
            capacity_snapshot = None
    else:
        capacity_snapshot = None

    # -- Active limits -------------------------------------------------------
    config: Any = getattr(app, "config", None)
    limits_obj: Any = None
    if config is not None:
        limits_obj = getattr(config, "limits", None)
    limits_snapshot = _snapshot_limits(limits_obj)

    # -- Health state (startup-derived, not live-refreshed) ------------------
    health_state: Any = getattr(app, "_health_state", None)
    if health_state is not None:
        if hasattr(health_state, "to_dict"):
            startup_health_snapshot = health_state.to_dict()
        elif isinstance(health_state, dict):
            startup_health_snapshot = health_state
        else:
            startup_health_snapshot = None
    else:
        startup_health_snapshot = None

    # -- Live health state (populated by refresh_live_health) -----------------
    live_health_obj: Any = getattr(app, "_live_health_state", None)
    live_health_snapshot: dict[str, Any] | None
    if live_health_obj is not None and hasattr(live_health_obj, "to_dict"):
        live_health_snapshot = live_health_obj.to_dict()
    else:
        live_health_snapshot = None

    has_live_health: bool = live_health_snapshot is not None

    # -- Runtime accounting counters -----------------------------------------
    accounting_obj: Any = getattr(app, "_runtime_accounting", None)
    accounting_snapshot: dict[str, int] | None
    if accounting_obj is not None and hasattr(accounting_obj, "snapshot"):
        accounting_snapshot = accounting_obj.snapshot()
    else:
        accounting_snapshot = None

    # -- Boot summary --------------------------------------------------------
    boot_summary_obj: Any = getattr(app, "_boot_summary", None)
    boot_summary_snapshot: dict[str, Any] | None
    if boot_summary_obj is not None and hasattr(boot_summary_obj, "to_dict"):
        boot_summary_snapshot = boot_summary_obj.to_dict()
    else:
        boot_summary_snapshot = None

    # -- Route eligibility (build-time, scope="build") ----------------------
    route_elig_obj: Any = getattr(app, "route_eligibility", None)
    route_eligibility_snapshot: dict[str, Any] | None
    if route_elig_obj is not None:
        route_eligibility_snapshot = {
            "configured": list(route_elig_obj.configured),
            "disabled": list(route_elig_obj.disabled),
            "degraded": [
                {
                    "failed_adapter_ids": list(d.failed_adapter_ids),
                    "route_id": d.route_id,
                }
                for d in route_elig_obj.degraded
            ],
            "live_refresh": False,
            "registered": list(route_elig_obj.registered),
            "scope": "build",
            "skipped": [
                {
                    "failed_adapter_ids": list(s.failed_adapter_ids),
                    "reason": s.reason,
                    "route_id": s.route_id,
                }
                for s in route_elig_obj.skipped
            ],
            "unavailable": [
                {
                    "missing_adapter_ids": list(u.missing_adapter_ids),
                    "reason": u.reason,
                    "route_id": u.route_id,
                }
                for u in route_elig_obj.unavailable
            ],
        }
    else:
        route_eligibility_snapshot = None

    # -- Route build readiness (per-route operational states, scope="build") -
    route_build_readiness_snapshot: dict[str, Any] | None
    if route_elig_obj is not None and hasattr(route_elig_obj, "route_states"):
        states = {
            rid: state.value
            for rid, state in sorted(route_elig_obj.route_states.items())
        }
        route_build_readiness_snapshot = {
            "live_refresh": False,
            "scope": "build",
            "states": states,
        }
    else:
        route_build_readiness_snapshot = None

    # -- Startup-derived route readiness (scope="startup") ------------------
    startup_readiness_obj: Any = getattr(app, "_startup_readiness", None)
    startup_readiness_snapshot: dict[str, Any] | None
    if startup_readiness_obj is not None:
        startup_readiness_snapshot = {
            "degraded": [
                {
                    "failed_adapter_ids": list(d.failed_adapter_ids),
                    "route_id": d.route_id,
                }
                for d in startup_readiness_obj.degraded
            ],
            "live_refresh": False,
            "readiness": {
                rid: state.value
                for rid, state in sorted(startup_readiness_obj.route_states.items())
            },
            "scope": "startup",
            "skipped": [
                {
                    "failed_adapter_ids": list(s.failed_adapter_ids),
                    "reason": s.reason,
                    "route_id": s.route_id,
                }
                for s in startup_readiness_obj.skipped
            ],
        }
    else:
        startup_readiness_snapshot = None

    # -- Runtime events (bounded, debug/unstable) ----------------------------
    event_buffer_obj: Any = getattr(app, "_event_buffer", None)
    runtime_events_snapshot: dict[str, Any] | None
    if event_buffer_obj is not None and hasattr(event_buffer_obj, "snapshot"):
        runtime_events_snapshot = event_buffer_obj.snapshot()
    else:
        runtime_events_snapshot = None

    # -- Retry worker state ---------------------------------------------------
    retry_state_obj: Any = getattr(app, "retry_state", None)
    retry_snapshot: dict[str, Any]
    if retry_state_obj is not None:
        retry_snapshot = {
            "dead_lettered": getattr(retry_state_obj, "dead_lettered", 0),
            "enabled": getattr(retry_state_obj, "enabled", False),
            "failed": getattr(retry_state_obj, "failed", 0),
            "last_run_at": getattr(retry_state_obj, "last_run_at", None),
            "live_refresh": False,
            "processed": getattr(retry_state_obj, "processed", 0),
            "running": getattr(retry_state_obj, "running", False),
            "scope": "process_local",
            "succeeded": getattr(retry_state_obj, "succeeded", 0),
        }
    else:
        retry_snapshot = {
            "dead_lettered": 0,
            "enabled": False,
            "failed": 0,
            "last_run_at": None,
            "live_refresh": False,
            "processed": 0,
            "running": False,
            "scope": "process_local",
            "succeeded": 0,
        }

    # -- Assemble final sectioned snapshot (sorted keys) ---------------------
    snap: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_at": snapshot_at,
        "accounting": {
            "counters": accounting_snapshot,
            "live_refresh": False,
            "scope": "process_local",
        },
        "adapters": adapters,
        "capacity": {
            "live_refresh": False,
            "scope": "process_local",
            "state": capacity_snapshot,
        },
        "diagnostics": {
            "live_refresh": False,
            "runtime_events": runtime_events_snapshot,
            "scope": "process_local",
        },
        "health": {
            "live_health": live_health_snapshot,
            "live_refresh": has_live_health,
            "scope": "live" if has_live_health else "startup",
        },
        "identity": {},
        "lifecycle": lifecycle,
        "limits": limits_snapshot,
        "persistence": {},
        "replay": {
            "available": replay_available,
            "counters": replay_counters,
        },
        "retry": retry_snapshot,
        "routes": {
            "build_readiness": route_build_readiness_snapshot,
            "eligibility": route_eligibility_snapshot,
            "startup_readiness": startup_readiness_snapshot,
            "stats": routes_stats_with_provenance,
        },
        "startup": {
            "boot_summary": boot_summary_snapshot,
            "build_failures": build_failures,
            "live_refresh": False,
            "scope": "startup",
            "startup_health": startup_health_snapshot,
        },
        "unstable": {},
    }

    return _sorted_dict(snap)
