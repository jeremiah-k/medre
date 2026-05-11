"""Diagnostics subsystem for route and replay metrics visibility.

Provides structured, deterministic snapshots of route-level and replay-level
counters for observability.  All snapshots are JSON-safe, sorted, and free
of secrets or raw SDK objects.

Public symbols
--------------
* :class:`~medre.core.diagnostics.replay_metrics.ReplayMetrics`
  – mutable collector for replay-specific counters with per-route breakdown.
* :class:`~medre.core.diagnostics.replay_metrics.ReplayRouteCounters`
  – frozen per-route replay counter dataclass.
* :func:`~medre.core.diagnostics.snapshot.build_diagnostics_snapshot`
  – compose RouteStats + ReplayMetrics into a single deterministic dict.
"""

from medre.core.diagnostics.replay_metrics import (
    ReplayMetrics,
    ReplayRouteCounters,
)
from medre.core.diagnostics.snapshot import build_diagnostics_snapshot

__all__ = [
    "ReplayMetrics",
    "ReplayRouteCounters",
    "build_diagnostics_snapshot",
]
