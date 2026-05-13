"""Deterministic runtime snapshot builder for the MEDRE framework.

Provides :func:`build_runtime_snapshot` which reads the current state of a
:class:`~medre.runtime.app.MedreApp` and returns a plain-dict, JSON-safe,
deterministic snapshot.  No SDK objects, no secrets, no async I/O.

Snapshot schema (``schema_version`` 1)
--------------------------------------
Top-level keys are alphabetically sorted for stable serialisation::

    {
      "accounting": {...} | null,
      "adapters": {adapter_id: {...}},
      "boot_summary": {...} | null,
      "build_failures": [...],
      "capacity": {...} | null,
      "delivery_counters": {route_id: {...}} | null,
      "limits": {...},
      "live_health": null,
      "replay": {"available": bool, "counters": {...} | null},
      "routes": {route_id: {...}},
      "runtime_state": str,
      "schema_version": 1,
      "snapshot_at": str,
      "startup_health": {...} | null,
      "startup_timestamp": str | null,
      "uptime_seconds": float | null,
    }

Key guarantees:

* Deterministic key ordering (sorted dicts).
* No SDK objects — only plain ``dict`` / ``list`` / ``str`` / ``int`` /
  ``float`` / ``bool`` / ``None``.
* No secrets — adapter configs are never introspected.
* Bounded size — adapter and route collections are capped.
* Graceful degradation — absent optional subsystems report ``null``
  rather than raising.

Health freshness:

* Adapter-level ``"health"`` values come from the adapter's
  ``_last_health`` attribute (static, set during build/startup), not
  from live ``health_check()`` calls.  Values are startup-derived
  unless explicitly refreshed by an external caller.
* The ``"startup_health"`` field reflects the runtime health state
  initialized at startup; it is not automatically refreshed by
  post-start health polling.
* The ``"live_health"`` field is always ``null`` because active
  post-start health polling is not implemented.  Operators must not
  mistake ``startup_health`` for current/live health.

Future extensions
-----------------
* ``"live_health"`` is reserved for a :class:`RuntimeHealth` aggregate
  that may be populated by a future active health-polling integration.
  Until that integration exists, the field is always ``null``.
* ``"startup_timestamp"`` and ``"uptime_seconds"`` read
  ``app._startup_monotonic`` (float, monotonic epoch) and
  ``app._startup_wall`` (ISO-8601 string).  If those attributes do not
  exist yet (the runtime sets them during ``start()``), both report ``null``.
* The function signature accepts injectable ``now_fn`` and
  ``monotonic_fn`` callables so the CLI and diagnostics can supply a
  frozen clock.

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

if TYPE_CHECKING:
    from medre.runtime.app import MedreApp

__all__ = ["build_runtime_snapshot", "SCHEMA_VERSION"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Current snapshot schema version.  Bumped when the top-level shape changes."""

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
    """Extract JSON-safe adapter metadata from a :class:`BaseAdapter`.

    Reads only static attributes; does **not** call
    :meth:`~medre.adapters.base.BaseAdapter.health_check` (which is async).
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


def _sanitize_error(error: str) -> str:
    """Sanitize an error string for safe inclusion in snapshots.

    Strips likely token/secret patterns and SDK object repr strings,
    then truncates to :data:`_MAX_ERROR_DETAIL_LEN`.
    """
    import re as _re

    _TOKEN = _re.compile(
        r'(syt_[A-Za-z0-9]+)'
        r'|(MDAx[A-Za-z0-9+/=]{20,})'
        r'|(?!(.)\3{39,})[A-Za-z0-9+/=]{40,}'
        r'|(sk-[A-Za-z0-9]{20,})'
        r'|(api[_-]?key[=:]\s*\S+)'
        r'|(access_token[=:]\s*\S+)'
        r'|(password[=:]\s*\S+)'
        r'|(secret[=:]\s*\S+)'
    )
    _SDK = _re.compile(r'<[\w.]+ object at 0x[0-9a-fA-F]+>')

    sanitized = _TOKEN.sub('[REDACTED]', error)
    sanitized = _SDK.sub('[OBJECT_REPR]', sanitized)
    if len(sanitized) > _MAX_ERROR_DETAIL_LEN:
        sanitized = sanitized[: _MAX_ERROR_DETAIL_LEN - 3] + "..."
    return sanitized


def _snapshot_build_failures(failures: list[Any]) -> list[dict[str, Any]]:
    """Extract JSON-safe build-failure records."""
    entries: list[dict[str, Any]] = []
    for bf in failures[:_MAX_BUILD_FAILURES]:
        adapter_id = getattr(bf, "adapter_id", "unknown")
        error = getattr(bf, "error", "unknown error")
        error_str = _sanitize_error(str(error))
        entries.append({
            "adapter_id": adapter_id,
            "error": error_str,
        })
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
     responsibility of the caller or a future integration.

    **Health freshness:** The ``"startup_health"`` field reflects the
    latest runtime-owned health snapshot, which is currently
    initialized at startup and not automatically refreshed.  Adapter-level
    health values come from the adapter's ``_last_health`` attribute
    (static, set during build) rather than from live ``health_check()``
    calls.  The ``"live_health"`` field is always ``null`` because
    active post-start health polling is not implemented.  Callers and
    operators must not assume ``startup_health`` represents real-time
    adapter health.
    """
    _now = now_fn or _now_utc
    _mono = monotonic_fn or _monotonic_now

    snapshot_at = _utc_iso(_now())

    # -- Runtime state -------------------------------------------------------
    state_attr = getattr(app, "state", None)
    if state_attr is not None and hasattr(state_attr, "value"):
        runtime_state: str = state_attr.value
    elif state_attr is not None:
        runtime_state = str(state_attr)
    else:
        runtime_state = "unknown"

    # -- Startup timestamp & uptime ------------------------------------------
    # The runtime may add _startup_wall (ISO string) and _startup_monotonic
    # (float, monotonic seconds) to MedreApp during start().  Tolerate
    # absence gracefully.
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
    delivery_counters: dict[str, Any] | None
    if route_stats is not None and hasattr(route_stats, "snapshot"):
        raw_routes = route_stats.snapshot()
        # Apply bound and sort.
        routes_snapshot = {}
        for rid in sorted(raw_routes.keys())[:_MAX_ROUTES]:
            routes_snapshot[rid] = raw_routes[rid]
        delivery_counters = dict(routes_snapshot)
    else:
        routes_snapshot = {}
        delivery_counters = None

    # -- Replay availability & counters --------------------------------------
    replay_engine: Any = getattr(app, "_replay_engine", None)
    replay_available = replay_engine is not None

    # Try to get replay counters from an observability collector if wired.
    # The DiagnosticsCollector (runtime/observability.py) is injected by
    # the builder; check for _diagnostics_collector or diagnostician.
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
    # The _health_state attribute is set during startup classification.
    # It reflects the health assessment made at boot time and is not
    # automatically refreshed by post-start health polling (not implemented).
    # Callers must not assume this represents real-time adapter health.
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

    # -- Assemble final snapshot (sorted keys) -------------------------------
    snap: dict[str, Any] = {
        "accounting": accounting_snapshot,
        "adapters": adapters,
        "boot_summary": boot_summary_snapshot,
        "build_failures": build_failures,
        "capacity": capacity_snapshot,
        "delivery_counters": delivery_counters,
        "limits": limits_snapshot,
        "live_health": None,
        "replay": {
            "available": replay_available,
            "counters": replay_counters,
        },
        "routes": routes_snapshot,
        "runtime_state": runtime_state,
        "schema_version": SCHEMA_VERSION,
        "snapshot_at": snapshot_at,
        "startup_health": startup_health_snapshot,
        "startup_timestamp": startup_wall,
        "uptime_seconds": uptime_seconds,
    }

    return _sorted_dict(snap)
