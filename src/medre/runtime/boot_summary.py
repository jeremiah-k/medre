"""Deterministic runtime boot summary for startup diagnostics.

Provides :class:`BootSummary` — a frozen dataclass recording the outcome of
runtime startup in a JSON-safe, deterministic snapshot.  Built during
:class:`~medre.runtime.app.MedreApp.start()` and stored on the app for
downstream snapshot / CLI consumption.

Public symbols
--------------
* :class:`BootSummary` — frozen startup diagnostics snapshot.
* :func:`build_boot_summary` — constructs a BootSummary from runtime state.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

__all__ = ["BootSummary", "build_boot_summary"]


@dataclass(frozen=True)
class BootSummary:
    """Immutable record of runtime startup outcome.

    All fields are plain types (no SDK objects, no secrets).  The
    ``to_dict()`` method produces a deterministic, JSON-safe dictionary
    with alphabetically sorted keys.

    Attributes
    ----------
    startup_timestamp:
        ISO-8601 UTC string when startup completed, or ``None``.
    startup_outcome:
        One of ``"success"``, ``"partial"``, ``"total_failure"``.
    runtime_health:
        One of ``"healthy"``, ``"degraded"``, ``"failed"``.
    adapters_started:
        Number of adapters that started successfully.
    adapters_failed:
        Number of adapters that failed during startup.
    adapters_total:
        Total number of adapters that were attempted (enabled + built).
    adapters_disabled:
        Number of configured but disabled adapters (not attempted).
    build_failure_count:
        Number of adapters that failed during construction (before startup).
    failed_adapter_ids:
        Sorted tuple of adapter IDs that failed to start.
    started_adapter_ids:
        Sorted tuple of adapter IDs that started successfully.
    route_count:
        Number of registered routes at startup time.
    storage_backend:
        Storage backend name (e.g. ``"sqlite"``, ``"memory"``), or ``"none"``.
    replay_available:
        Whether the replay engine was wired.
    persisted_events_count:
        Number of events in storage at startup, or ``None`` if unavailable.
    """

    startup_timestamp: str | None
    startup_outcome: str
    runtime_health: str
    adapters_started: int
    adapters_failed: int
    adapters_total: int
    adapters_disabled: int
    build_failure_count: int
    failed_adapter_ids: tuple[str, ...]
    started_adapter_ids: tuple[str, ...]
    route_count: int
    storage_backend: str
    replay_available: bool
    persisted_events_count: int | None

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe dict of the boot summary.

        Keys are sorted alphabetically at every level.
        """
        d = dataclasses.asdict(self)
        # Ensure tuple becomes list for JSON
        return _sorted_dict(d)


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
        elif isinstance(val, (list, tuple)):
            result[key] = [
                _sorted_dict(v) if isinstance(v, dict) else v for v in val
            ]
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_boot_summary(
    *,
    startup_timestamp: str | None,
    startup_outcome: str,
    runtime_health: str,
    adapters_started: int,
    adapters_failed: int,
    adapters_total: int,
    adapters_disabled: int,
    build_failure_count: int,
    failed_adapter_ids: list[str] | tuple[str, ...],
    started_adapter_ids: list[str] | tuple[str, ...],
    route_count: int,
    storage_backend: str,
    replay_available: bool,
    persisted_events_count: int | None,
) -> BootSummary:
    """Construct a :class:`BootSummary` with deterministic field ordering.

    All list inputs are sorted and converted to tuples for immutability
    and deterministic output.
    """
    return BootSummary(
        startup_timestamp=startup_timestamp,
        startup_outcome=startup_outcome,
        runtime_health=runtime_health,
        adapters_started=adapters_started,
        adapters_failed=adapters_failed,
        adapters_total=adapters_total,
        adapters_disabled=adapters_disabled,
        build_failure_count=build_failure_count,
        failed_adapter_ids=tuple(sorted(failed_adapter_ids)),
        started_adapter_ids=tuple(sorted(started_adapter_ids)),
        route_count=route_count,
        storage_backend=storage_backend,
        replay_available=replay_available,
        persisted_events_count=persisted_events_count,
    )
