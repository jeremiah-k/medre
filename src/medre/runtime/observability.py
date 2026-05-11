"""Runtime observability collector for route and replay metrics.

Provides a single :class:`DiagnosticsCollector` that composes
:class:`~medre.core.routing.stats.RouteStats` with
:class:`~medre.core.diagnostics.replay_metrics.ReplayMetrics` behind a
unified interface.  Use :meth:`DiagnosticsCollector.snapshot` to obtain a
deterministic, JSON-safe diagnostics snapshot.

This module is the **only writer** for both subsystems at runtime.

Public symbols
--------------
* :class:`DiagnosticsCollector` – unified route + replay metrics collector.
"""

from __future__ import annotations

from medre.core.diagnostics.replay_metrics import ReplayMetrics
from medre.core.diagnostics.snapshot import build_diagnostics_snapshot
from medre.core.routing.stats import RouteStats

__all__ = ["DiagnosticsCollector"]


class DiagnosticsCollector:
    """Unified collector for route and replay execution metrics.

    Owns a :class:`~medre.core.routing.stats.RouteStats` and a
    :class:`~medre.core.diagnostics.replay_metrics.ReplayMetrics` instance.
    All recording methods delegate to the appropriate subsystem; the
    :meth:`snapshot` method produces a single deterministic dict.

    Example
    -------
    >>> collector = DiagnosticsCollector()
    >>> collector.record_route_delivered("bridge-a")
    >>> collector.record_replay_delivery_succeeded("bridge-a")
    >>> snap = collector.snapshot()
    >>> snap["routes"]["bridge-a"]["delivered"]
    1
    >>> snap["replay"]["global"]["replay_deliveries_succeeded"]
    1
    """

    def __init__(self) -> None:
        self._route_stats = RouteStats()
        self._replay_metrics = ReplayMetrics()

    # -- Route-level recording (delegates to RouteStats) ----------------------

    def record_route_delivered(self, route_id: str) -> None:
        """Record a successful delivery for *route_id*."""
        self._route_stats.record_delivered(route_id)

    def record_route_failed(self, route_id: str, error: str) -> None:
        """Record a failed delivery for *route_id*.

        The *error* string is sanitised before storage (tokens, keys, and
        raw SDK object reprs are redacted).
        """
        self._route_stats.record_failed(route_id, error)

    def record_route_skipped(self, route_id: str) -> None:
        """Record a skipped delivery for *route_id*."""
        self._route_stats.record_skipped(route_id)

    def record_route_loop_prevented(self, route_id: str) -> None:
        """Record a loop-prevented skip for *route_id*."""
        self._route_stats.record_loop_prevented(route_id)

    # -- Replay-level recording (delegates to ReplayMetrics) ------------------

    def record_replay_events_processed(self, route_id: str) -> None:
        """Record a replayed event processed through *route_id*."""
        self._replay_metrics.record_events_processed(route_id)

    def record_replay_delivery_attempted(self, route_id: str) -> None:
        """Record a delivery attempt for *route_id* during replay."""
        self._replay_metrics.record_delivery_attempted(route_id)

    def record_replay_delivery_succeeded(self, route_id: str) -> None:
        """Record a successful delivery for *route_id* during replay."""
        self._replay_metrics.record_delivery_succeeded(route_id)

    def record_replay_delivery_failed(self, route_id: str) -> None:
        """Record a failed delivery for *route_id* during replay."""
        self._replay_metrics.record_delivery_failed(route_id)

    def record_replay_skipped_by_filter(self, route_id: str) -> None:
        """Record a replay event skipped by filter for *route_id*."""
        self._replay_metrics.record_skipped_by_filter(route_id)

    def record_replay_skipped_by_loop(self, route_id: str) -> None:
        """Record a replay event skipped by loop prevention for *route_id*."""
        self._replay_metrics.record_skipped_by_loop(route_id)

    # -- Snapshot -------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a unified, deterministic diagnostics snapshot.

        See :func:`~medre.core.diagnostics.snapshot.build_diagnostics_snapshot`
        for the full structure specification.
        """
        return build_diagnostics_snapshot(
            self._route_stats, self._replay_metrics,
        )
