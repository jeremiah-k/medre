"""Replay summary: immutable operator-facing snapshot and collection utilities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from medre.core.engine.replay.types import ReplayMode, ReplayResult

# Maximum number of error messages retained in a summary to prevent
# unbounded memory growth on large, failure-heavy replays.
_MAX_SUMMARY_ERRORS = 50

# String truncation length for individual error messages.
_MAX_ERROR_LENGTH = 512


@dataclass(frozen=True)
class ReplaySummary:
    """Immutable, JSON-safe snapshot of a completed replay operation.

    Designed for operator dashboards.  All fields are read-only after
    construction and :meth:`to_dict` produces a deterministic,
    ``json.dumps``-compatible mapping.  Replay is a best-effort
    operation; event delivery is not re-guaranteed by replay.

    Attributes
    ----------
    events_scanned:
        Total events scanned from storage (may differ from results when
        post-filters or limits exclude events).  ``0`` when not provided.
    events_replayed:
        Count of ``(event, stage)`` result tuples produced (same as
        ``ReplayState.events_processed``).
    skipped_count:
        Results with ``status == "skipped"``.
    failure_count:
        Results with ``status in ("failed", "error")``.
    route_resolution_count:
        Count of route-stage results that resolved targets (non-None
        route output with at least one target).  ``0`` when unavailable.
        This counts route-stage target resolution, *not*
        :class:`~medre.core.planning.relation_resolution.RelationResolver`
        or native-ref relation resolution.
    elapsed_ms:
        Wall-clock duration of the replay in milliseconds.  ``0.0``
        when not provided.
    by_status:
        Mapping of status string to count, e.g.
        ``{"passed": 5, "skipped": 1, "failed": 0, "error": 0}``.
        Keys are always the four canonical statuses with deterministic
        ordering.
    by_stage:
        Mapping of stage name to count, e.g. ``{"store": 3, "route": 2}``.
        Ordered by stage occurrence in the replay.
    errors:
        Collected error messages (truncated to
        :data:`_MAX_SUMMARY_ERRORS` entries, each capped at
        :data:`_MAX_ERROR_LENGTH` characters).  Empty list when no
        errors occurred.
    by_route:
        Per-route event counts.  Maps route_id to a dict with keys
        ``"events"`` (total events replayed through this route),
        ``"succeeded"`` (route-stage status ``"passed"``), and
        ``"failed"`` (route-stage status ``"failed"`` or ``"error"``).
        Empty when no route-stage results were produced.
    run_id:
        Operator-assigned identifier for this replay execution.
        Empty string when not provided.  Matches the ``run_id`` in
        :class:`ReplayRouteAttribution` for cross-referencing.
    mode:
        The :class:`ReplayMode` used, if known.  ``None`` when not
        provided.
    """

    events_scanned: int = 0
    events_replayed: int = 0
    skipped_count: int = 0
    failure_count: int = 0
    route_resolution_count: int = 0
    elapsed_ms: float = 0.0
    by_status: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    by_route: dict[str, dict[str, int]] = field(default_factory=dict)
    run_id: str = ""
    mode: ReplayMode | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe representation.

        The returned mapping is suitable for ``json.dumps(...,
        sort_keys=True)`` and has stable key ordering.
        """
        return {
            "by_route": {
                rid: dict(sorted(counts.items()))
                for rid, counts in sorted(self.by_route.items())
            },
            "by_stage": dict(sorted(self.by_stage.items())),
            "by_status": {
                "error": self.by_status.get("error", 0),
                "failed": self.by_status.get("failed", 0),
                "passed": self.by_status.get("passed", 0),
                "skipped": self.by_status.get("skipped", 0),
            },
            "elapsed_ms": self.elapsed_ms,
            "errors": list(self.errors),
            "events_replayed": self.events_replayed,
            "events_scanned": self.events_scanned,
            "failure_count": self.failure_count,
            "mode": self.mode.value if self.mode is not None else None,
            "route_resolution_count": self.route_resolution_count,
            "run_id": self.run_id,
            "skipped_count": self.skipped_count,
        }


def _build_summary(
    results: list[ReplayResult],
    *,
    events_scanned: int = 0,
    elapsed_ms: float = 0.0,
    mode: ReplayMode | None = None,
    run_id: str = "",
) -> ReplaySummary:
    """Build an immutable :class:`ReplaySummary` from a list of results.

    This is the internal construction helper used by
    :func:`collect_replay_summary`.  It is exposed publicly for
    callers who already have materialised results.

    Parameters
    ----------
    results:
        Materialised replay results.
    events_scanned:
        Total events scanned (including filtered/limited).  ``0`` if
        unknown.
    elapsed_ms:
        Wall-clock duration in milliseconds.  ``0.0`` if unknown.
    mode:
        The replay mode used.  ``None`` if unknown.
    run_id:
        Operator-assigned identifier for this replay.  ``""`` if
        unknown.
    """
    by_status: dict[str, int] = {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
    by_stage: dict[str, int] = {}
    errors: list[str] = []
    route_resolution_count = 0
    by_route: dict[str, dict[str, int]] = {}

    for result in results:
        # Status counts
        if result.status in by_status:
            by_status[result.status] += 1

        # Stage counts
        by_stage[result.stage] = by_stage.get(result.stage, 0) + 1

        # Error collection (truncated)
        if result.error:
            truncated = result.error[:_MAX_ERROR_LENGTH]
            if len(errors) < _MAX_SUMMARY_ERRORS:
                errors.append(truncated)

        # Route-resolution: route-stage results with non-None,
        # non-empty output indicate target resolution.
        if result.stage == "route" and result.status == "passed":
            if result.output is not None:
                # route output is list[tuple[Any, list[Any]]]
                if isinstance(result.output, list) and len(result.output) > 0:
                    route_resolution_count += 1

        # Per-route counts from route_attribution.
        if result.stage == "route" and result.route_attribution is not None:
            attr = result.route_attribution
            is_success = result.status == "passed"
            is_failure = result.status in ("failed", "error")
            for rid in attr.route_ids:
                if rid not in by_route:
                    by_route[rid] = {"events": 0, "succeeded": 0, "failed": 0}
                by_route[rid]["events"] += 1
                if is_success:
                    by_route[rid]["succeeded"] += 1
                elif is_failure:
                    by_route[rid]["failed"] += 1

    events_replayed = len(results)
    skipped_count = by_status["skipped"]
    failure_count = by_status["failed"] + by_status["error"]

    return ReplaySummary(
        events_scanned=events_scanned,
        events_replayed=events_replayed,
        skipped_count=skipped_count,
        failure_count=failure_count,
        route_resolution_count=route_resolution_count,
        elapsed_ms=elapsed_ms,
        by_status=by_status,
        by_stage=by_stage,
        errors=tuple(errors),
        by_route=by_route,
        run_id=run_id,
        mode=mode,
    )


async def collect_replay_summary(
    results: AsyncIterator[ReplayResult],
    *,
    events_scanned: int | None = None,
    elapsed_ms: float | None = None,
    mode: ReplayMode | None = None,
    run_id: str = "",
) -> ReplaySummary:
    """Consume *results* and return an immutable :class:`ReplaySummary`.

    Materialises the async iterator, computes status/stage breakdowns,
    and returns a frozen summary suitable for ``json.dumps``.

    Parameters
    ----------
    results:
        Async iterator of :class:`ReplayResult` items.
    events_scanned:
        Override for the events-scanned count.  When ``None``, defaults
        to the number of distinct ``event_id`` values in *results*.
    elapsed_ms:
        Wall-clock duration in milliseconds.  ``None`` -> ``0.0``.
    mode:
        The :class:`ReplayMode` used.  ``None`` if unknown.
    run_id:
        Operator-assigned identifier for this replay.  ``""`` if
        unknown.

    Returns
    -------
    ReplaySummary
        Immutable summary of the replay operation.
    """
    collected = [result async for result in results]

    if events_scanned is None:
        # Derive from distinct event_ids in results
        event_ids: set[str] = set()
        for r in collected:
            event_ids.add(r.event_id)
        events_scanned_val = len(event_ids)
    else:
        events_scanned_val = events_scanned

    return _build_summary(
        collected,
        events_scanned=events_scanned_val,
        elapsed_ms=elapsed_ms if elapsed_ms is not None else 0.0,
        mode=mode,
        run_id=run_id,
    )
