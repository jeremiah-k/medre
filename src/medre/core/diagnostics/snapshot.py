"""Deterministic snapshot builder for combined route and replay diagnostics.

Composes :class:`~medre.core.routing.stats.RouteStats` and
:class:`~medre.core.diagnostics.replay_metrics.ReplayMetrics` into a
single, JSON-safe, alphabetically-sorted snapshot suitable for dashboards
and audit logs.

Guarantees
----------
* Route keys sorted by ``route_id``.
* Adapter keys sorted by ``adapter_id``.
* No raw SDK objects, no secrets, no canonical payload content.
* ``last_error`` values are pre-sanitised by :class:`RouteStats`.

Public symbols
--------------
* :func:`build_diagnostics_snapshot` – compose route + replay metrics into
  a single deterministic dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from medre.core.diagnostics.replay_metrics import ReplayMetrics
    from medre.core.routing.stats import RouteStats


def build_diagnostics_snapshot(
    route_stats: RouteStats,
    replay_metrics: ReplayMetrics,
) -> dict:
    """Return a unified, deterministic diagnostics snapshot.

    Parameters
    ----------
    route_stats:
        Per-route delivery counters (from the routing subsystem).
    replay_metrics:
        Per-route replay counters (from the diagnostics subsystem).

    Returns
    -------
    dict
        Top-level keys:

        * ``"routes"`` – route-level delivery counters sorted by route_id.
          Each value is a dict with ``delivered``, ``failed``, ``skipped``,
          ``loop_prevented``, and optional ``last_error``.
        * ``"replay"`` – replay-specific counters with ``"global"`` totals
          and ``"by_route"`` breakdown sorted by route_id.
    """
    return {
        "routes": route_stats.snapshot(),
        "replay": replay_metrics.snapshot(),
    }
