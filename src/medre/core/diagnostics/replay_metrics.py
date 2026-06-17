"""Replay-aware metrics collector for diagnostics visibility.

Tracks global replay counters and per-route replay breakdowns.
All counter keys are plain strings (no SDK objects).  Snapshots
are deterministic: route and adapter keys are sorted alphabetically.

Public symbols
--------------
* :class:`ReplayRouteCounters` – frozen per-route replay counter dataclass.
* :class:`ReplayMetrics` – mutable collector for replay-specific counters.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Per-route replay counters (frozen, immutable snapshots)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayRouteCounters:
    """Immutable replay counters for a single route.

    Attributes
    ----------
    events_processed:
        Events replayed through this route.
    deliveries_attempted:
        Delivery attempts for this route during replay.
    deliveries_succeeded:
        Successful deliveries for this route during replay.
    deliveries_failed:
        Failed deliveries for this route during replay.
    skipped_by_filter:
        Events skipped because the replay filter excluded this route.
    skipped_by_loop:
        Events skipped by loop prevention during replay.
    """

    events_processed: int = 0
    deliveries_attempted: int = 0
    deliveries_succeeded: int = 0
    deliveries_failed: int = 0
    skipped_by_filter: int = 0
    skipped_by_loop: int = 0


# ---------------------------------------------------------------------------
# Mutable replay metrics collector
# ---------------------------------------------------------------------------


class ReplayMetrics:
    """Collects global and per-route replay execution counters.

    Thread-safe for concurrent increment operations under the CPython GIL.
    Use :meth:`snapshot` to obtain a deterministic, JSON-safe summary.

    Methods
    -------
    record_events_processed(route_id):
        Increment the processed counter for *route_id*.
    record_delivery_attempted(route_id):
        Increment the delivery-attempted counter for *route_id*.
    record_delivery_succeeded(route_id):
        Increment the delivery-succeeded counter for *route_id*.
    record_delivery_failed(route_id):
        Increment the delivery-failed counter for *route_id*.
    record_skipped_by_filter(route_id):
        Increment the skipped-by-filter counter for *route_id*.
    record_skipped_by_loop(route_id):
        Increment the skipped-by-loop counter for *route_id*.
    snapshot():
        Return a deterministic ordered dict of all replay counters.
    """

    def __init__(self) -> None:
        self._route_counters: dict[str, ReplayRouteCounters] = {}
        # Backlog, rejection, cancellation counters.
        self._backlog_estimate: int = 0
        self._rejection_count: int = 0
        self._cancellation_count: int = 0
        self._last_cancelled_at: float | None = None

    # -- Helpers ---------------------------------------------------------------

    def _get(self, route_id: str) -> ReplayRouteCounters:
        return self._route_counters.get(route_id, ReplayRouteCounters())

    def _put(self, route_id: str, c: ReplayRouteCounters) -> None:
        self._route_counters[route_id] = c

    # -- Recording methods -----------------------------------------------------

    def record_events_processed(self, route_id: str) -> None:
        """Record a replayed event processed through *route_id*."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed + 1,
                deliveries_attempted=c.deliveries_attempted,
                deliveries_succeeded=c.deliveries_succeeded,
                deliveries_failed=c.deliveries_failed,
                skipped_by_filter=c.skipped_by_filter,
                skipped_by_loop=c.skipped_by_loop,
            ),
        )

    def record_delivery_attempted(self, route_id: str) -> None:
        """Record a delivery attempt for *route_id* during replay."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed,
                deliveries_attempted=c.deliveries_attempted + 1,
                deliveries_succeeded=c.deliveries_succeeded,
                deliveries_failed=c.deliveries_failed,
                skipped_by_filter=c.skipped_by_filter,
                skipped_by_loop=c.skipped_by_loop,
            ),
        )

    def record_delivery_succeeded(self, route_id: str) -> None:
        """Record a successful delivery for *route_id* during replay."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed,
                deliveries_attempted=c.deliveries_attempted,
                deliveries_succeeded=c.deliveries_succeeded + 1,
                deliveries_failed=c.deliveries_failed,
                skipped_by_filter=c.skipped_by_filter,
                skipped_by_loop=c.skipped_by_loop,
            ),
        )

    def record_delivery_failed(self, route_id: str) -> None:
        """Record a failed delivery for *route_id* during replay."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed,
                deliveries_attempted=c.deliveries_attempted,
                deliveries_succeeded=c.deliveries_succeeded,
                deliveries_failed=c.deliveries_failed + 1,
                skipped_by_filter=c.skipped_by_filter,
                skipped_by_loop=c.skipped_by_loop,
            ),
        )

    def record_skipped_by_filter(self, route_id: str) -> None:
        """Record a replay event skipped by filter for *route_id*."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed,
                deliveries_attempted=c.deliveries_attempted,
                deliveries_succeeded=c.deliveries_succeeded,
                deliveries_failed=c.deliveries_failed,
                skipped_by_filter=c.skipped_by_filter + 1,
                skipped_by_loop=c.skipped_by_loop,
            ),
        )

    def record_skipped_by_loop(self, route_id: str) -> None:
        """Record a replay event skipped by loop prevention for *route_id*."""
        c = self._get(route_id)
        self._put(
            route_id,
            ReplayRouteCounters(
                events_processed=c.events_processed,
                deliveries_attempted=c.deliveries_attempted,
                deliveries_succeeded=c.deliveries_succeeded,
                deliveries_failed=c.deliveries_failed,
                skipped_by_filter=c.skipped_by_filter,
                skipped_by_loop=c.skipped_by_loop + 1,
            ),
        )

    # -- Backlog / rejection / cancellation counters ----------------

    def set_backlog_estimate(self, count: int) -> None:
        """Update the estimated number of pending replay events."""
        self._backlog_estimate = max(0, count)

    def record_rejection(self) -> None:
        """Record a capacity rejection during replay."""
        self._rejection_count += 1

    def record_cancellation(self) -> None:
        """Record a replay cancellation, capturing the current timestamp."""
        self._cancellation_count += 1
        self._last_cancelled_at = _time.monotonic()

    # -- Snapshot --------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a deterministic ordered snapshot of all replay counters.

        Returns
        -------
        dict
            Keys:

            * ``"global"`` – aggregated totals across all routes.
              Includes ``backlog_estimate``, ``rejection_count``,
              ``last_cancelled_at`` in addition to delivery counters.
            * ``"by_route"`` – per-route breakdown sorted by route_id.
              Each entry includes ``events_processed``,
              ``deliveries_attempted``, ``deliveries_succeeded``,
              ``deliveries_failed``, ``skipped_by_filter``,
              ``skipped_by_loop``.
        """
        # Aggregate global totals
        total_events = 0
        total_attempted = 0
        total_succeeded = 0
        total_failed = 0
        total_filter = 0
        total_loop = 0

        by_route: dict[str, dict[str, int]] = {}
        for route_id in sorted(self._route_counters):
            c = self._route_counters[route_id]
            total_events += c.events_processed
            total_attempted += c.deliveries_attempted
            total_succeeded += c.deliveries_succeeded
            total_failed += c.deliveries_failed
            total_filter += c.skipped_by_filter
            total_loop += c.skipped_by_loop

            by_route[route_id] = {
                "events_processed": c.events_processed,
                "deliveries_attempted": c.deliveries_attempted,
                "deliveries_succeeded": c.deliveries_succeeded,
                "deliveries_failed": c.deliveries_failed,
                "skipped_by_filter": c.skipped_by_filter,
                "skipped_by_loop": c.skipped_by_loop,
            }

        return {
            "global": {
                "replay_events_processed": total_events,
                "replay_deliveries_attempted": total_attempted,
                "replay_deliveries_succeeded": total_succeeded,
                "replay_deliveries_failed": total_failed,
                "replay_skipped_by_filter": total_filter,
                "replay_skipped_by_loop": total_loop,
                "backlog_estimate": self._backlog_estimate,
                "rejection_count": self._rejection_count,
                "cancellation_count": self._cancellation_count,
                "last_cancelled_at": self._last_cancelled_at,
            },
            "by_route": by_route,
        }
