"""Route-level delivery statistics.

This module provides per-route counters and aggregated snapshots for
observability:

* :class:`RouteCounters` – frozen dataclass with delivered/failed/skipped/
  loop_prevented counters for a single route.
* :class:`RouteStats` – mutable collector that records per-route counters
  and latest errors, with a deterministic :meth:`snapshot` for
  serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from medre.observability.sanitization import sanitize_error


# ---------------------------------------------------------------------------
# Per-route counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteCounters:
    """Immutable counters for a single route.

    Attributes
    ----------
    delivered:
        Number of successful deliveries.
    failed:
        Number of failed deliveries.
    skipped:
        Number of intentionally skipped deliveries.
    loop_prevented:
        Number of deliveries prevented by the self-loop guard.
    """

    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    loop_prevented: int = 0


# ---------------------------------------------------------------------------
# Aggregated stats
# ---------------------------------------------------------------------------


class RouteStats:
    """Collects per-route delivery counters and latest errors.

    Internally mutable; externally read-only via :meth:`snapshot`.

    Methods
    -------
    record_delivered(route_id):
        Increment the delivered counter for *route_id*.
    record_failed(route_id, error):
        Increment the failed counter and store the latest error.
    record_skipped(route_id):
        Increment the skipped counter.
    record_loop_prevented(route_id):
        Increment the loop_prevented counter.
    snapshot():
        Return a deterministic ordered dict of counters and errors.
    """

    def __init__(self) -> None:
        self._counters: dict[str, RouteCounters] = {}
        self._last_errors: dict[str, str] = {}

    # -- Recording ---------------------------------------------------------

    def record_delivered(self, route_id: str) -> None:
        """Record a successful delivery for *route_id*."""
        c = self._counters.get(route_id, RouteCounters())
        self._counters[route_id] = RouteCounters(
            delivered=c.delivered + 1,
            failed=c.failed,
            skipped=c.skipped,
            loop_prevented=c.loop_prevented,
        )

    def record_failed(self, route_id: str, error: str) -> None:
        """Record a failed delivery for *route_id*."""
        c = self._counters.get(route_id, RouteCounters())
        self._counters[route_id] = RouteCounters(
            delivered=c.delivered,
            failed=c.failed + 1,
            skipped=c.skipped,
            loop_prevented=c.loop_prevented,
        )
        self._last_errors[route_id] = sanitize_error(error)

    def record_skipped(self, route_id: str) -> None:
        """Record a skipped delivery for *route_id*."""
        c = self._counters.get(route_id, RouteCounters())
        self._counters[route_id] = RouteCounters(
            delivered=c.delivered,
            failed=c.failed,
            skipped=c.skipped + 1,
            loop_prevented=c.loop_prevented,
        )

    def record_loop_prevented(self, route_id: str) -> None:
        """Record a loop-prevented skip for *route_id*."""
        c = self._counters.get(route_id, RouteCounters())
        self._counters[route_id] = RouteCounters(
            delivered=c.delivered,
            failed=c.failed,
            skipped=c.skipped,
            loop_prevented=c.loop_prevented + 1,
        )

    # -- Snapshot ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a deterministic ordered snapshot of all counters.

        Returns
        -------
        dict
            Keys are route IDs sorted alphabetically.  Each value is a
            dict with ``delivered``, ``failed``, ``skipped``,
            ``loop_prevented``, and optional ``last_error``.
        """
        result: dict[str, dict] = {}
        for route_id in sorted(self._counters):
            c = self._counters[route_id]
            entry: dict[str, int | str] = {
                "delivered": c.delivered,
                "failed": c.failed,
                "skipped": c.skipped,
                "loop_prevented": c.loop_prevented,
            }
            if route_id in self._last_errors:
                entry["last_error"] = self._last_errors[route_id]
            result[route_id] = entry
        return result
