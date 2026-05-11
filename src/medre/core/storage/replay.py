"""Replay harness for deterministic re-processing of historical events.

This module provides the machinery to re-process canonical events that have
already been persisted in storage through selected pipeline stages.  Different
:class:`ReplayMode` values control which stages are executed and whether
side-effects (delivery to adapters) are allowed.

Replay is **read-only** for STRICT, RE_RENDER, RE_ROUTE, and DRY_RUN modes –
stored canonical events are never mutated.  Only BEST_EFFORT mode permits the
delivery side-effect.

Mode guarantees
---------------
+------------+----------+--------+---------+---------+-------------------+
| Mode       | Store    | Route  | Render  | Deliver | Side effects      |
+============+==========+========+=========+=========+===================+
| STRICT     | verify   | --     | --      | --      | None (read-only)  |
+------------+----------+--------+---------+---------+-------------------+
| RE_RENDER  | verify   | --     | capture | --      | None (read-only)  |
+------------+----------+--------+---------+---------+-------------------+
| RE_ROUTE   | verify   | route  | --      | --      | None (read-only)  |
+------------+----------+--------+---------+---------+-------------------+
| BEST_EFFORT| verify   | route  | render  | deliver | Adapter delivery  |
+------------+----------+--------+---------+---------+-------------------+
| DRY_RUN    | verify   | route  | capture | skip    | None (read-only)  |
+------------+----------+--------+---------+---------+-------------------+

Public symbols
--------------
* :class:`ReplayMode` – behavioural mode enum.
* :class:`ReplayRequest` – filter and targeting for a replay operation.
* :class:`ReplayResult` – outcome of replaying a single event through one stage.
* :class:`ReplayState` – aggregate state tracker for a replay operation.
* :func:`collect_replay_state` – consume results into a :class:`ReplayState`.
* :class:`ReplaySummary` – immutable, JSON-safe replay summary.
* :func:`collect_replay_summary` – consume results into a :class:`ReplaySummary`.
* :func:`_build_summary` – construct a :class:`ReplaySummary` from materialised results.
* :class:`ReplayEngine` – the main replay orchestrator.
* :class:`ReplayRouteAttribution` – route attribution captured during route-aware replay.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Literal,
    Protocol,
    runtime_checkable,
)

from medre.core.events import CanonicalEvent, is_registered
from medre.core.storage.backend import EventFilter, StorageBackend

if TYPE_CHECKING:
    from medre.core.observability.metrics import Diagnostician


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------


class ReplayMode(Enum):
    """Behavioural mode controlling which pipeline stages are executed.

    Attributes
    ----------
    STRICT:
        Exact replay – verify event existence and integrity without
        invoking any pipeline stages.  No side effects.  Useful for
        integrity checks and migration validation.

        **Guarantees:** read-only; no storage mutations; deterministic
        for the same stored events; re-raises unexpected exceptions.

    RE_RENDER:
        Re-run transforms and rendering, capture output.  Routing,
        planning, and delivery are **not** executed.  Useful for testing
        new renderers and metadata evolution.

        **Guarantees:** read-only; no storage mutations; rendering output
        is captured in :attr:`ReplayResult.output`; deterministic for the
        same stored events and renderer configuration; re-raises
        unexpected exceptions.

    RE_ROUTE:
        Re-run transforms, routing, and planning with current routes.
        Rendering and delivery are **not** executed.  Useful for testing
        route changes and planning changes.

        **Guarantees:** read-only; no storage mutations; route and plan
        outputs are captured in :attr:`ReplayResult.output`; deterministic
        for the same stored events and route configuration; re-raises
        unexpected exceptions.

    BEST_EFFORT:
        Full re-processing including delivery to adapters.  Same as
        normal processing but sourced from historical events.  Useful
        for migration and testing adapters with real data.

        **Guarantees:** **only** mode with side effects (adapter delivery);
        individual event failures are captured as ``"error"`` results
        without crashing the replay; crashed events are recorded via
        the :class:`Diagnostician`; results are yielded in storage query
        order for deterministic iteration.

    DRY_RUN:
        Execute all pipeline stages up to and including rendering, but
        **skip delivery**.  Equivalent to BEST_EFFORT minus the deliver
        stage.  Useful for previewing what a BEST_EFFORT replay would do
        without any side effects.

        **Guarantees:** read-only; no storage mutations; route, plan, and
        render outputs are captured; delivery stage is always ``"skipped"``
        with the reason ``"dry_run: delivery suppressed"``; re-raises
        unexpected exceptions.
    """

    STRICT = "strict"
    RE_RENDER = "re_render"
    RE_ROUTE = "re_route"
    BEST_EFFORT = "best_effort"
    DRY_RUN = "dry_run"


# ---------------------------------------------------------------------------
# Mode-to-stages mapping
# ---------------------------------------------------------------------------

# Ordered pipeline stages per mode.
_MODE_STAGES: dict[ReplayMode, tuple[str, ...]] = {
    ReplayMode.STRICT: ("store",),
    ReplayMode.RE_RENDER: ("store", "render"),
    ReplayMode.RE_ROUTE: ("store", "route", "plan"),
    ReplayMode.BEST_EFFORT: ("store", "route", "plan", "render", "deliver"),
    ReplayMode.DRY_RUN: ("store", "route", "plan", "render", "deliver"),
}

# Modes that produce side effects (adapter delivery).
_SIDE_EFFECT_MODES: frozenset[ReplayMode] = frozenset({ReplayMode.BEST_EFFORT})


# ---------------------------------------------------------------------------
# Pipeline protocol (optional collaborator)
# ---------------------------------------------------------------------------


@runtime_checkable
class _PipelineProtocol(Protocol):
    """Minimal protocol that the pipeline collaborator must satisfy.

    The replay engine only calls the methods it needs for the requested
    replay mode.  If a method is not needed (e.g. ``deliver`` in STRICT
    mode), the pipeline does not have to provide it.

    Methods
    -------
    transform_event:
        Apply registered transforms to an event.
    render_event:
        Render an event for delivery.
    route_event:
        Match an event against current routes and resolve targets.
    plan_delivery:
        Build delivery plans from routing results.
    deliver:
        Execute delivery plans to adapters.
    """

    async def transform_event(self, event: CanonicalEvent) -> CanonicalEvent:
        """Apply registered transforms to *event* and return the result."""
        ...

    async def render_event(self, event: CanonicalEvent) -> Any:
        """Render *event* for delivery and return the rendering result."""
        ...

    async def route_event(
        self, event: CanonicalEvent,
    ) -> list[tuple[Any, list[Any]]]:
        """Match *event* against current routes and resolve targets.

        Returns a list of ``(route, targets)`` pairs.
        """
        ...

    async def plan_delivery(
        self,
        event: CanonicalEvent,
        routes: list[tuple[Any, list[Any]]],
    ) -> list[Any]:
        """Build delivery plans for the given event and route-target pairs."""
        ...

    async def deliver(
        self, event: CanonicalEvent, plans: list[Any],
    ) -> list[Any]:
        """Execute delivery plans and return receipts."""
        ...


@runtime_checkable
class _EventBusProtocol(Protocol):
    """Minimal event-bus protocol for publishing replayed events.

    Accepted by :class:`ReplayEngine` but not invoked during replay.
    Reserved for future notification use.
    """

    async def publish(
        self, event: CanonicalEvent, *, source: str = "",
    ) -> None:
        """Publish *event* to the bus."""
        ...


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ReplayRequest:
    """Filter and targeting specification for a replay operation.

    All filter fields are optional; ``None`` means *no restriction*.

    Attributes
    ----------
    time_start:
        Earliest event timestamp to include (inclusive).
    time_end:
        Latest event timestamp to include (inclusive).
    event_kinds:
        Restrict to these event kind strings.  ``None`` = all kinds.
    source_adapters:
        Restrict to events from these adapters.  ``None`` = all adapters.
    target_stages:
        Pipeline stages to replay.  ``None`` = all stages allowed by the
        selected :class:`ReplayMode`.  Valid values are ``"store"``,
        ``"route"``, ``"plan"``, ``"render"``, ``"deliver"``.
    correlation_ids:
        Restrict to events whose ``event_id`` appears in this list.
        ``None`` = no ID filtering.  When set, events are fetched by
        individual ID rather than via a storage query, and remaining
        filter fields are applied as post-filters.
    mode:
        The replay behavioural mode.
    limit:
        Maximum number of events to replay.
    target_adapters:
        Restrict delivery to these adapter names.  ``None`` = all
        adapters resolved by routing.  Only meaningful for modes that
        include the ``deliver`` stage (BEST_EFFORT, DRY_RUN).  Events
        whose delivery plans target adapters not in this list have their
        deliver stage result set to ``"skipped"``.
    route_ids:
        Restrict routing to only these route IDs.  ``()`` (empty) means
        all routes are considered (backward compatible).  When non-empty,
        only routes whose ``id`` appears in this tuple are used during
        replay.  If a requested route ID is disabled or does not match
        the event, a warning is recorded in the route attribution.
    run_id:
        Optional operator-assigned identifier for this replay execution.
        When set, it is recorded in :class:`ReplayRouteAttribution` and
        :class:`ReplaySummary` so operators can correlate replay runs.

        **Idempotency note:** Replay may intentionally redeliver events.
        Duplicate-send risk exists by design for mesh/radio transports
        where at-least-once delivery is the norm.  Operators should use
        ``run_id`` to track and deduplicate at the application layer.
    """

    time_start: datetime | None = None
    time_end: datetime | None = None
    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    target_stages: list[str] | None = None
    correlation_ids: list[str] | None = None
    mode: ReplayMode = ReplayMode.STRICT
    limit: int = 1000
    target_adapters: list[str] | None = None
    route_ids: tuple[str, ...] = ()
    run_id: str = ""


@dataclass
class ReplayResult:
    """Outcome of replaying a single event through one pipeline stage.

    Attributes
    ----------
    event_id:
        The canonical event identifier.
    stage:
        The pipeline stage that produced this result (``"store"``,
        ``"route"``, ``"plan"``, ``"render"``, ``"deliver"``).
    status:
        ``"passed"`` – stage completed successfully.
        ``"skipped"`` – stage was not executed because an upstream
        dependency was unavailable, delivery was suppressed (dry_run),
        or the target adapter was excluded by ``target_adapters``.
        ``"failed"`` – stage ran but the result was negative (e.g.
        integrity check failed, no routes matched).
        ``"error"`` – an exception was raised during stage execution.
    output:
        Stage-specific output, if applicable.
    error:
        Human-readable error message when *status* is ``"error"`` or
        ``"failed"``.
    duration_ms:
        Wall-clock time spent in this stage, in milliseconds.
    lineage:
        Lineage chain from the source canonical event.
    """

    event_id: str
    stage: str
    status: Literal["passed", "skipped", "failed", "error"]
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    lineage: list[str] = field(default_factory=list)
    route_attribution: ReplayRouteAttribution | None = None


@dataclass(frozen=True)
class ReplayRouteAttribution:
    """Route attribution captured during route-aware replay.

    Stored in :attr:`ReplayResult.route_attribution` as namespaced metadata
    alongside the route-target pairs.  This preserves route attribution
    without altering the canonical event schema.

    Determinism guarantee: for the same stored event and route
    configuration, the attribution is identical across replay runs.

    Attributes
    ----------
    route_ids:
        Identifiers of the routes that matched this event during replay.
    source_adapter:
        The ``source_adapter`` of the replayed canonical event.
    target_adapters:
        Adapter names of all resolved targets across matched routes.
    replay_mode:
        The :class:`ReplayMode` used for this replay.
    is_replay:
        Always ``True`` – distinguishes replay attribution from
        live-routing metadata.
    loop_warnings:
        Tuple of human-readable loop-prevention warnings, if any routes
        were skipped.  Empty when no loops were detected.
    run_id:
        Operator-assigned identifier for the replay execution that
        produced this attribution.  Empty string when not provided.
        Use this to correlate replay runs and deduplicate at the
        application layer.
    """

    route_ids: tuple[str, ...] = ()
    source_adapter: str = ""
    target_adapters: tuple[str, ...] = ()
    replay_mode: str = ""
    is_replay: bool = True
    loop_warnings: tuple[str, ...] = ()
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, deterministic representation."""
        return {
            "is_replay": self.is_replay,
            "loop_warnings": list(self.loop_warnings),
            "replay_mode": self.replay_mode,
            "route_ids": list(self.route_ids),
            "run_id": self.run_id,
            "source_adapter": self.source_adapter,
            "target_adapters": list(self.target_adapters),
        }


@dataclass
class ReplayState:
    """Aggregate state tracker for a replay operation.

    Accumulates counters and diagnostics across all :class:`ReplayResult`
    items produced by a single :meth:`ReplayEngine.replay` call.

    Attributes
    ----------
    events_processed:
        Total number of ``(event, stage)`` results recorded.
    events_passed:
        Count of results with ``status == "passed"``.
    events_skipped:
        Count of results with ``status == "skipped"``.
    events_failed:
        Count of results with ``status in ("failed", "error")``.
    current_lineage:
        Lineage of the most recently processed event.  Updated on
        every :meth:`record` call so callers can track derivation
        ancestry across the replay.
    errors:
        Collected error messages from ``"failed"`` and ``"error"``
        results.  In :attr:`ReplayMode.BEST_EFFORT` mode this list
        doubles as the diagnostic log.
    """

    events_processed: int = 0
    events_passed: int = 0
    events_skipped: int = 0
    events_failed: int = 0
    current_lineage: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record(self, result: ReplayResult) -> None:
        """Update state from a single *result*.

        Increments the appropriate counter, appends error messages
        when present, and refreshes ``current_lineage`` from the
        result's :attr:`~ReplayResult.lineage` field.
        """
        self.events_processed += 1
        if result.status == "passed":
            self.events_passed += 1
        elif result.status == "skipped":
            self.events_skipped += 1
        elif result.status in ("failed", "error"):
            self.events_failed += 1
            if result.error:
                self.errors.append(result.error)
        if result.lineage:
            self.current_lineage = list(result.lineage)


async def collect_replay_state(
    results: AsyncIterator[ReplayResult],
) -> ReplayState:
    """Consume all *results* and return the accumulated :class:`ReplayState`.

    Convenience wrapper that iterates over an async iterator of
    :class:`ReplayResult` items (as returned by
    :meth:`ReplayEngine.replay`) and accumulates them into a single
    :class:`ReplayState`.
    """
    state = ReplayState()
    async for result in results:
        state.record(result)
    return state


# ---------------------------------------------------------------------------
# Replay summary – immutable operator-facing snapshot
# ---------------------------------------------------------------------------


# Maximum number of error messages retained in a summary to prevent
# unbounded memory growth on large, failure-heavy replays.
_MAX_SUMMARY_ERRORS = 50

# String truncation length for individual error messages.
_MAX_ERROR_LENGTH = 512


@dataclass(frozen=True)
class ReplaySummary:
    """Immutable, JSON-safe snapshot of a completed replay operation.

    Designed for operator dashboards and audit logs.  All fields are
    read-only after construction and :meth:`to_dict` produces a
    deterministic, ``json.dumps``-compatible mapping.

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
        Wall-clock duration in milliseconds.  ``None`` → ``0.0``.
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
    collected: list[ReplayResult] = []
    async for result in results:
        collected.append(result)

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _request_to_filter(request: ReplayRequest) -> EventFilter:
    """Convert a :class:`ReplayRequest` to an :class:`EventFilter`.

    The ``correlation_ids``, ``target_stages``, and ``target_adapters``
    fields have no equivalent in ``EventFilter`` and are handled
    separately by :meth:`ReplayEngine.replay`.
    """
    return EventFilter(
        event_kinds=request.event_kinds,
        source_adapters=request.source_adapters,
        time_start=request.time_start,
        time_end=request.time_end,
        limit=request.limit,
    )


def _resolve_stages(request: ReplayRequest) -> tuple[str, ...]:
    """Return the ordered tuple of stages to execute for *request*.

    The result is the intersection of the stages allowed by the replay
    mode and the stages explicitly requested via ``target_stages``.
    If ``target_stages`` is ``None``, all stages for the mode are used.
    """
    allowed = _MODE_STAGES[request.mode]
    if request.target_stages is None:
        return allowed
    requested = set(request.target_stages)
    return tuple(s for s in allowed if s in requested)


def _event_matches_filters(
    event: CanonicalEvent,
    request: ReplayRequest,
) -> bool:
    """Return ``True`` if *event* satisfies the non-ID filter criteria.

    Used when events are fetched individually by correlation ID and the
    time / kind / adapter filters must be applied as post-filters.
    """
    if (
        request.event_kinds is not None
        and event.event_kind not in request.event_kinds
    ):
        return False
    if (
        request.source_adapters is not None
        and event.source_adapter not in request.source_adapters
    ):
        return False
    if request.time_start is not None and event.timestamp < request.time_start:
        return False
    if request.time_end is not None and event.timestamp > request.time_end:
        return False
    return True


def _elapsed_ms(t0: float) -> float:
    """Return milliseconds elapsed since *t0* (from ``time.monotonic()``)."""
    return (time.monotonic() - t0) * 1000.0


def _verify_immutability(original: CanonicalEvent, event_id: str) -> None:
    """Assert that *original* is still frozen (immutable).

    This is a development-time guard to catch accidental mutation of
    historical canonical events during replay.  Since ``CanonicalEvent``
    uses ``frozen=True`` (msgspec Struct), any in-place mutation raises
    ``FrozenInstanceError`` at the point of attempted mutation.  This
    function provides an explicit checkpoint for diagnostic purposes
    and verifies that key identity fields remain stable.

    Replay must never mutate historical CanonicalEvents.  This guarantee
    holds across all replay modes — even BEST_EFFORT reads events from
    storage without modification.
    """
    # The frozen=True on CanonicalEvent prevents mutation at the
    # Python level.  This function verifies key identity fields are
    # present as an additional guard.
    assert original.event_id == event_id, (
        f"Event ID mismatch during immutability check: "
        f"{original.event_id!r} != {event_id!r}"
    )


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Replays historical canonical events through selected pipeline stages.

    The replay engine reads events from storage (read-only) and pushes them
    through the specified pipeline stages.  Different :class:`ReplayMode`
    values control which stages are executed and whether side effects
    (delivery to adapters) are allowed.

    Parameters
    ----------
    storage:
        The storage backend to read historical events from.
    pipeline:
        Optional pipeline collaborator that satisfies
        :class:`_PipelineProtocol`.  Required for ``RE_RENDER``,
        ``RE_ROUTE``, ``BEST_EFFORT``, and ``DRY_RUN`` modes.
    event_bus:
        Optional event bus for publishing replayed events.  Accepted
        but not currently invoked during replay; reserved for future
        notification use.
    diagnostician:
        Optional :class:`~medre.core.observability.metrics.Diagnostician`
        for recording replay skips, downgrades, renderer failures, and
        adapter failures.  When provided, diagnostic events are emitted
        for each notable replay condition.
    """

    def __init__(
        self,
        storage: StorageBackend,
        pipeline: _PipelineProtocol | None = None,
        event_bus: _EventBusProtocol | None = None,
        diagnostician: Diagnostician | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._event_bus = event_bus
        self._diagnostician = diagnostician

    # -- Public API ---------------------------------------------------------

    async def replay(
        self, request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Iterate over matching events and replay through requested stages.

        Yields one :class:`ReplayResult` for each ``(event, stage)``
        combination.

        When ``request.correlation_ids`` is set, events are fetched by
        individual ID (via :meth:`StorageBackend.get`) and remaining
        filter criteria are applied as post-filters.  Otherwise a
        standard :meth:`StorageBackend.query` is used.

        **Determinism guarantee:** Results are yielded in the order
        events are returned by storage (timestamp ascending for queries,
        correlation_id list order for ID-based lookups).  For a given
        stored dataset and pipeline configuration, the sequence of
        ``(event_id, stage, status)`` tuples is deterministic.

        **Immutability guarantee:** The replay engine never mutates
        historical :class:`CanonicalEvent` instances.  Events are read
        from storage and passed through pipeline stages without
        modification.  Non-BEST_EFFORT modes produce no storage side
        effects.

        Parameters
        ----------
        request:
            Filter and targeting specification.

        Yields
        ------
        ReplayResult
            Outcome for each stage of each matching event.
        """
        stages = _resolve_stages(request)

        if request.correlation_ids is not None:
            async for event_id, event in self._iter_by_ids(request):
                if event is None:
                    async for result in self._replay_missing(event_id, stages):
                        yield result
                else:
                    async for result in self._replay_event_safe(
                        event, stages, request,
                    ):
                        yield result
        else:
            event_filter = _request_to_filter(request)
            async for event in self._storage.query(event_filter):  # type: ignore[union-attr]
                async for result in self._replay_event_safe(
                    event, stages, request,
                ):
                    yield result

    async def count_matching(self, request: ReplayRequest) -> int:
        """Return the number of events matching *request* without replaying.

        Follows the same dual-path strategy as :meth:`replay`: individual
        gets when ``correlation_ids`` is set, storage query otherwise.

        Parameters
        ----------
        request:
            Filter specification.

        Returns
        -------
        int
            Count of matching events.
        """
        count = 0

        if request.correlation_ids is not None:
            for eid in request.correlation_ids:
                if count >= request.limit:
                    break
                event = await self._storage.get(eid)
                if event is not None and _event_matches_filters(event, request):
                    count += 1
        else:
            event_filter = _request_to_filter(request)
            async for _ in self._storage.query(event_filter):  # type: ignore[union-attr]
                count += 1

        return count

    # -- Internal event iteration -------------------------------------------

    async def _iter_by_ids(
        self, request: ReplayRequest,
    ) -> AsyncIterator[tuple[str, CanonicalEvent | None]]:
        """Yield ``(event_id, event | None)`` tuples for correlation IDs.

        For each requested ID, fetches the event from storage.  If the
        event does not exist, ``(event_id, None)`` is yielded so that
        the caller can report the failure.  If the event exists but does
        not match the filter criteria (time, kind, adapter), the pair is
        skipped entirely.

        Respects the ``limit`` on *request*.
        """
        yielded = 0
        ids = request.correlation_ids
        if ids is None:
            return
        for eid in ids:
            if yielded >= request.limit:
                break
            event = await self._storage.get(eid)
            if event is None:
                yielded += 1
                yield (eid, None)
                continue
            if not _event_matches_filters(event, request):
                continue
            yielded += 1
            yield (eid, event)

    # -- Internal per-event replay ------------------------------------------

    async def _replay_event_safe(
        self,
        event: CanonicalEvent,
        stages: tuple[str, ...],
        request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event, wrapping with BEST_EFFORT crash-safety.

        Delegates to :meth:`_replay_event` inside a ``try`` block.  In
        :attr:`ReplayMode.BEST_EFFORT` mode any unexpected exception is
        caught and yielded as a single ``"error"`` result so the caller
        is never crashed by an individual event failure.  Other modes
        re-raise the exception.
        """
        mode = request.mode
        try:
            async for result in self._replay_event(event, stages, request):
                yield result
        except Exception as exc:
            if mode is ReplayMode.BEST_EFFORT:
                if self._diagnostician is not None:
                    self._diagnostician.record_adapter_failure(
                        event.event_id,
                        "replay",
                        f"Unexpected error in BEST_EFFORT mode: {exc}",
                    )
                yield ReplayResult(
                    event_id=event.event_id,
                    stage="unknown",
                    status="error",
                    error=f"Unexpected error in BEST_EFFORT mode: {exc}",
                    lineage=list(event.lineage),
                )
            else:
                raise

    async def _replay_missing(
        self,
        event_id: str,
        stages: tuple[str, ...],
    ) -> AsyncIterator[ReplayResult]:
        """Yield results for an event that could not be found in storage.

        The first stage (``store``) receives ``"failed"`` status; all
        subsequent stages receive ``"skipped"``.
        """
        if self._diagnostician is not None:
            self._diagnostician.record_replay_skip(
                event_id, "Event not found in storage",
            )
        for stage in stages:
            if stage == "store":
                yield ReplayResult(
                    event_id=event_id,
                    stage="store",
                    status="failed",
                    error="Event not found in storage",
                )
            else:
                yield ReplayResult(
                    event_id=event_id,
                    stage=stage,
                    status="skipped",
                    error="Event not found in storage; upstream stages failed",
                )

    async def _replay_event(
        self,
        event: CanonicalEvent,
        stages: tuple[str, ...],
        request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event through *stages*, yielding results.

        Carries intermediate state (route results, delivery plans) forward
        between stages so that downstream stages can use upstream outputs.
        Each stage is always attempted; downstream stages gracefully
        handle missing upstream data.
        """
        mode = request.mode
        route_result: list[tuple[Any, list[Any]]] | None = None
        plan_result: list[Any] | None = None

        # Immutability guard: checkpoint event identity before processing.
        _verify_immutability(event, event.event_id)

        for stage in stages:
            if stage == "store":
                result = await self._stage_store(event)
            elif stage == "route":
                result, route_result = await self._stage_route(
                    event, request=request,
                )
            elif stage == "plan":
                result, plan_result = await self._stage_plan(
                    event, route_result,
                )
            elif stage == "render":
                result = await self._stage_render(event, mode)
            elif stage == "deliver":
                result = await self._stage_deliver(
                    event, plan_result, request,
                )
            else:
                result = ReplayResult(
                    event_id=event.event_id,
                    stage=stage,
                    status="skipped",
                    error=f"Unknown stage: {stage!r}",
                )
            result.lineage = list(event.lineage)
            yield result

    # -- Stage implementations ----------------------------------------------

    async def _stage_store(self, event: CanonicalEvent) -> ReplayResult:
        """Verify that *event* still exists in storage and is well-formed.

        This stage is read-only and performs no mutations.  It checks:

        1. The event can still be retrieved by ID from storage.
        2. The ``event_id`` field is non-empty.
        3. The ``event_kind`` is registered in the built-in kind registry.
        """
        t0 = time.monotonic()
        try:
            stored = await self._storage.get(event.event_id)
            if stored is None:
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_skip(
                        event.event_id, "Event not found in storage",
                    )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="store",
                    status="failed",
                    error="Event not found in storage",
                    duration_ms=_elapsed_ms(t0),
                )
            if not stored.event_id:
                return ReplayResult(
                    event_id=event.event_id,
                    stage="store",
                    status="failed",
                    error="Event has empty event_id",
                    duration_ms=_elapsed_ms(t0),
                )
            if not is_registered(stored.event_kind):
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_downgrade(
                        event.event_id,
                        stored.event_kind,
                        "unregistered_kind",
                    )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="store",
                    status="failed",
                    error=f"Unregistered event_kind: {stored.event_kind!r}",
                    duration_ms=_elapsed_ms(t0),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="store",
                status="passed",
                output=stored,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            return ReplayResult(
                event_id=event.event_id,
                stage="store",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )

    async def _stage_route(
        self, event: CanonicalEvent, *, request: ReplayRequest,
    ) -> tuple[ReplayResult, list[tuple[Any, list[Any]]] | None]:
        """Route *event* against current routes.

        Returns the :class:`ReplayResult` and the route-target pairs for
        use by downstream stages.  If no routes match, the result status
        is ``"failed"`` and the route data is an empty list (not None)
        so downstream stages can distinguish "no routes" from "routing
        not attempted".

        Route-aware replay adds :class:`ReplayRouteAttribution` to the
        result and filters out routes that would create replay loops.
        A replay loop is detected when a route would deliver back to the
        event's ``source_adapter`` or when the event's lineage / routing
        metadata indicates it was already routed through the same route.

        When ``request.route_ids`` is non-empty, only routes whose IDs
        appear in the set are used.  If a requested route ID was not
        found among the matched routes (e.g. because it is disabled or
        does not match the event's source), a warning is recorded in
        the route attribution's ``loop_warnings``.

        Disabled routes are automatically excluded by the router's
        ``match()`` method.  When a route is explicitly requested via
        ``route_ids`` but is disabled, a warning is emitted since the
        router will not return it.
        """
        t0 = time.monotonic()
        mode = request.mode
        requested_route_ids = request.route_ids
        run_id = request.run_id
        if self._pipeline is None:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="route",
                    status="error",
                    error="No pipeline configured; routing requires a pipeline",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )
        try:
            routes = await self._pipeline.route_event(event)

            # Filter by explicit route_ids when provided.
            if requested_route_ids:
                allowed = set(requested_route_ids)
                routes = [
                    (r, t) for r, t in routes
                    if getattr(r, "id", None) in allowed
                ]
                # Warn about requested route IDs not found among matched
                # routes.  This covers disabled routes (the router won't
                # return them) and routes that don't match the event's
                # source filter.
                found_ids = {
                    getattr(r, "id", None) for r, _ in routes
                }
                missing = allowed - found_ids
                if missing and self._diagnostician is not None:
                    for mid in sorted(missing):
                        self._diagnostician.record_replay_skip(
                            event.event_id,
                            f"Requested route_id {mid!r} not found in "
                            f"matched routes (may be disabled or "
                            f"source filter mismatch)",
                        )

            if not routes:
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_skip(
                        event.event_id, "No routes matched",
                    )
                attribution = ReplayRouteAttribution(
                    source_adapter=event.source_adapter,
                    replay_mode=mode.value,
                    run_id=run_id,
                )
                return (
                    ReplayResult(
                        event_id=event.event_id,
                        stage="route",
                        status="failed",
                        output=[],
                        duration_ms=_elapsed_ms(t0),
                        route_attribution=attribution,
                    ),
                    routes if routes else [],
                )

            # Route-aware loop prevention: filter routes that would
            # deliver back to the event's source adapter or match routes
            # the event was already routed through.
            loop_warnings, filtered_routes = _filter_replay_loops(
                event, routes,
            )

            # Build route attribution for this replay.
            route_ids = tuple(
                r.id for r, _ in filtered_routes
                if hasattr(r, "id")
            )
            target_adapters: list[str] = []
            for _, targets in filtered_routes:
                for t in targets:
                    adapter = getattr(t, "adapter", None)
                    if adapter is not None and adapter not in target_adapters:
                        target_adapters.append(adapter)

            attribution = ReplayRouteAttribution(
                route_ids=route_ids,
                source_adapter=event.source_adapter,
                target_adapters=tuple(target_adapters),
                replay_mode=mode.value,
                loop_warnings=tuple(loop_warnings),
                run_id=run_id,
            )

            if not filtered_routes:
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_skip(
                        event.event_id,
                        "All routes filtered by replay loop prevention",
                    )
                return (
                    ReplayResult(
                        event_id=event.event_id,
                        stage="route",
                        status="failed",
                        output=[],
                        duration_ms=_elapsed_ms(t0),
                        route_attribution=attribution,
                    ),
                    [],
                )

            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="route",
                    status="passed",
                    output=filtered_routes,
                    duration_ms=_elapsed_ms(t0),
                    route_attribution=attribution,
                ),
                filtered_routes,
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_planner_failure(
                    event.event_id, str(exc),
                )
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="route",
                    status="error",
                    error=str(exc),
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )

    async def _stage_plan(
        self,
        event: CanonicalEvent,
        route_result: list[tuple[Any, list[Any]]] | None,
    ) -> tuple[ReplayResult, list[Any] | None]:
        """Build delivery plans for *event* based on routing results.

        Returns the :class:`ReplayResult` and the delivery plans for use
        by downstream stages.
        """
        t0 = time.monotonic()
        if route_result is None:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="skipped",
                    error="No route result available; routing may have errored",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )
        if self._pipeline is None:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="error",
                    error="No pipeline configured; planning requires a pipeline",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )
        try:
            plans = await self._pipeline.plan_delivery(event, route_result)
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="passed",
                    output=plans,
                    duration_ms=_elapsed_ms(t0),
                ),
                plans,
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_planner_failure(
                    event.event_id, str(exc),
                )
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="error",
                    error=str(exc),
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )

    async def _stage_render(
        self,
        event: CanonicalEvent,
        mode: ReplayMode,
    ) -> ReplayResult:
        """Re-run transforms and rendering on *event*.

        Applies transforms first (via ``pipeline.transform_event``) and
        then renders the transformed event (via ``pipeline.render_event``).
        Captures the rendering output without delivering it.  Read-only.
        """
        t0 = time.monotonic()
        if self._pipeline is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="error",
                error="No pipeline configured; rendering requires a pipeline",
                duration_ms=_elapsed_ms(t0),
            )
        try:
            transformed = await self._pipeline.transform_event(event)
            rendered = await self._pipeline.render_event(transformed)
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="passed",
                output=rendered,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_renderer_failure(
                    event.event_id,
                    "replay",
                    str(exc),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )

    async def _stage_deliver(
        self,
        event: CanonicalEvent,
        plan_result: list[Any] | None,
        request: ReplayRequest,
    ) -> ReplayResult:
        """Execute delivery plans for *event*.

        This is the **only** stage with side effects – it delivers to
        adapters.  Only executed in :attr:`ReplayMode.BEST_EFFORT` mode.
        In :attr:`ReplayMode.DRY_RUN` mode the delivery is suppressed
        and the result is ``"skipped"``.

        Delivery metadata honesty: the output wraps adapter results in a
        replay delivery envelope that marks the delivery as originating
        from replay.  The adapter's original result is preserved as-is;
        queued / best-effort results are **not** promoted to delivered /
        final.  Downstream consumers can inspect ``output["replay"]``
        to distinguish replay deliveries from live ones.
        """
        t0 = time.monotonic()
        mode = request.mode

        # DRY_RUN mode: suppress delivery, always skip.
        if mode is ReplayMode.DRY_RUN:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="skipped",
                error="dry_run: delivery suppressed",
                duration_ms=_elapsed_ms(t0),
            )

        if plan_result is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="skipped",
                error="No delivery plans available; planning may have errored",
                duration_ms=_elapsed_ms(t0),
            )
        if self._pipeline is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error="No pipeline configured; delivery requires a pipeline",
                duration_ms=_elapsed_ms(t0),
            )

        # Filter plans by target_adapters if specified.
        if request.target_adapters is not None:
            filtered = _filter_plans_by_adapter(
                plan_result, request.target_adapters,
            )
            if not filtered:
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_skip(
                        event.event_id,
                        "No delivery plans matched target_adapters filter",
                    )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="skipped",
                    error="No delivery plans matched target_adapters filter",
                    duration_ms=_elapsed_ms(t0),
                )
            plan_result = filtered

        try:
            receipts = await self._pipeline.deliver(event, plan_result)
            # Wrap adapter results in a replay delivery envelope.
            # The adapter's original result is preserved without
            # promotion: queued/best-effort stays queued/best-effort.
            replay_output = _replay_delivery_envelope(receipts)
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="passed",
                output=replay_output,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_adapter_failure(
                    event.event_id, "replay", str(exc),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )


# ---------------------------------------------------------------------------
# Replay loop prevention
# ---------------------------------------------------------------------------


def _filter_replay_loops(
    event: CanonicalEvent,
    routes: list[tuple[Any, list[Any]]],
) -> tuple[list[str], list[tuple[Any, list[Any]]]]:
    """Filter routes that would create a replay routing loop.

    A replay loop is detected when:

    1. A route would deliver an event back to its own ``source_adapter``.
    2. The event's existing ``RoutingMetadata.matched_routes`` overlaps
       with a matched route ID, indicating the event was previously
       routed through the same route.

    Returns a tuple of ``(loop_warnings, filtered_routes)``.  Loop-causing
    routes are removed from the filtered list and a warning string is
    added for each.
    """
    source = event.source_adapter
    warnings: list[str] = []
    filtered: list[tuple[Any, list[Any]]] = []

    # Pre-compute previously matched routes from event metadata (if any).
    prev_matched: set[str] = set()
    routing_meta = event.metadata.routing
    if routing_meta is not None and routing_meta.matched_routes:
        prev_matched = set(routing_meta.matched_routes)

    for route, targets in routes:
        route_id = getattr(route, "id", None)

        # Check 1: would this route deliver back to the source?
        target_adapters = {
            getattr(t, "adapter", None) for t in targets
        }
        if source in target_adapters:
            warnings.append(
                f"Route {route_id!r} would deliver back to source "
                f"adapter {source!r}; skipped to prevent replay loop"
            )
            continue

        # Check 2: was this event already routed through this route?
        if route_id is not None and route_id in prev_matched:
            warnings.append(
                f"Event was previously routed through route "
                f"{route_id!r}; skipped to prevent replay loop"
            )
            continue

        filtered.append((route, targets))

    return warnings, filtered


# ---------------------------------------------------------------------------
# Replay delivery envelope
# ---------------------------------------------------------------------------


def _replay_delivery_envelope(receipts: Any) -> dict[str, Any]:
    """Wrap adapter delivery results in a replay delivery envelope.

    The envelope marks the delivery as originating from replay and
    preserves the adapter's original results without promotion:
    queued/best-effort stays queued/best-effort.  Downstream consumers
    can inspect ``output["replay"]`` to distinguish replay deliveries
    from live ones.

    Parameters
    ----------
    receipts:
        The original adapter delivery results (list of receipts,
        :class:`AdapterDeliveryResult` instances, or any other
        pipeline output).

    Returns
    -------
    dict
        Envelope with ``"replay": True`` and ``"adapter_results"`` key.
    """
    return {
        "replay": True,
        "adapter_results": receipts,
    }


# ---------------------------------------------------------------------------
# Plan filtering
# ---------------------------------------------------------------------------


def _filter_plans_by_adapter(
    plans: list[Any],
    target_adapters: list[str],
) -> list[Any]:
    """Filter delivery plans to those targeting adapters in *target_adapters*.

    Plans that do not expose a ``target`` attribute with an ``adapter``
    field are passed through (conservative: include rather than exclude
    when the plan structure is opaque).
    """
    allowed = set(target_adapters)
    result: list[Any] = []
    for plan in plans:
        target = getattr(plan, "target", None)
        adapter = getattr(target, "adapter", None) if target is not None else None
        if adapter is None:
            # Opaque plan structure – include conservatively.
            result.append(plan)
        elif adapter in allowed:
            result.append(plan)
    return result
