"""Replay harness for deterministic re-processing of historical events.

This module provides the machinery to re-process canonical events that have
already been persisted in storage through selected pipeline stages.  Different
:class:`ReplayMode` values control which stages are executed and whether
side-effects (delivery to adapters) are allowed.

Replay is **read-only** for STRICT, RE_RENDER, and RE_ROUTE modes – stored
canonical events are never mutated.  Only BEST_EFFORT mode permits the
delivery side-effect.

Public symbols
--------------
* :class:`ReplayMode` – behavioural mode enum.
* :class:`ReplayRequest` – filter and targeting for a replay operation.
* :class:`ReplayResult` – outcome of replaying a single event through one stage.
* :class:`ReplayState` – aggregate state tracker for a replay operation.
* :func:`collect_replay_state` – consume results into a :class:`ReplayState`.
* :class:`ReplayEngine` – the main replay orchestrator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Literal,
    Protocol,
    runtime_checkable,
)

from medre.core.events import CanonicalEvent, is_registered
from medre.core.storage.backend import EventFilter, StorageBackend


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
    RE_RENDER:
        Re-run transforms and rendering, capture output.  Routing,
        planning, and delivery are **not** executed.  Useful for testing
        new renderers and metadata evolution.
    RE_ROUTE:
        Re-run transforms, routing, and planning with current routes.
        Rendering and delivery are **not** executed.  Useful for testing
        route changes and planning changes.
    BEST_EFFORT:
        Full re-processing including delivery to adapters.  Same as
        normal processing but sourced from historical events.  Useful
        for migration and testing adapters with real data.
    """

    STRICT = "strict"
    RE_RENDER = "re_render"
    RE_ROUTE = "re_route"
    BEST_EFFORT = "best_effort"


# ---------------------------------------------------------------------------
# Mode-to-stages mapping
# ---------------------------------------------------------------------------

# Ordered pipeline stages per mode.
_MODE_STAGES: dict[ReplayMode, tuple[str, ...]] = {
    ReplayMode.STRICT: ("store",),
    ReplayMode.RE_RENDER: ("store", "render"),
    ReplayMode.RE_ROUTE: ("store", "route", "plan"),
    ReplayMode.BEST_EFFORT: ("store", "route", "plan", "render", "deliver"),
}


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
    """

    time_start: datetime | None = None
    time_end: datetime | None = None
    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    target_stages: list[str] | None = None
    correlation_ids: list[str] | None = None
    mode: ReplayMode = ReplayMode.STRICT
    limit: int = 1000


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
        dependency was unavailable.
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
    """

    event_id: str
    stage: str
    status: Literal["passed", "skipped", "failed", "error"]
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    lineage: list[str] = field(default_factory=list)


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
# Internal helpers
# ---------------------------------------------------------------------------


def _request_to_filter(request: ReplayRequest) -> EventFilter:
    """Convert a :class:`ReplayRequest` to an :class:`EventFilter`.

    The ``correlation_ids`` and ``target_stages`` fields have no
    equivalent in ``EventFilter`` and are handled separately by
    :meth:`ReplayEngine.replay`.
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
        ``RE_ROUTE``, and ``BEST_EFFORT`` modes.
    event_bus:
        Optional event bus for publishing replayed events.  Accepted
        but not currently invoked during replay; reserved for future
        notification use.
    """

    def __init__(
        self,
        storage: StorageBackend,
        pipeline: _PipelineProtocol | None = None,
        event_bus: _EventBusProtocol | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._event_bus = event_bus

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
                        event, stages, request.mode,
                    ):
                        yield result
        else:
            event_filter = _request_to_filter(request)
            async for event in self._storage.query(event_filter):  # type: ignore[union-attr]
                async for result in self._replay_event_safe(
                    event, stages, request.mode,
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
        mode: ReplayMode,
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event, wrapping with BEST_EFFORT crash-safety.

        Delegates to :meth:`_replay_event` inside a ``try`` block.  In
        :attr:`ReplayMode.BEST_EFFORT` mode any unexpected exception is
        caught and yielded as a single ``"error"`` result so the caller
        is never crashed by an individual event failure.  Other modes
        re-raise the exception.
        """
        try:
            async for result in self._replay_event(event, stages):
                yield result
        except Exception as exc:
            if mode is ReplayMode.BEST_EFFORT:
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
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event through *stages*, yielding results.

        Carries intermediate state (route results, delivery plans) forward
        between stages so that downstream stages can use upstream outputs.
        Each stage is always attempted; downstream stages gracefully
        handle missing upstream data.
        """
        route_result: list[tuple[Any, list[Any]]] | None = None
        plan_result: list[Any] | None = None

        for stage in stages:
            if stage == "store":
                result = await self._stage_store(event)
            elif stage == "route":
                result, route_result = await self._stage_route(event)
            elif stage == "plan":
                result, plan_result = await self._stage_plan(
                    event, route_result,
                )
            elif stage == "render":
                result = await self._stage_render(event)
            elif stage == "deliver":
                result = await self._stage_deliver(event, plan_result)
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
        self, event: CanonicalEvent,
    ) -> tuple[ReplayResult, list[tuple[Any, list[Any]]] | None]:
        """Route *event* against current routes.

        Returns the :class:`ReplayResult` and the route-target pairs for
        use by downstream stages.  If no routes match, the result status
        is ``"failed"`` and the route data is an empty list (not None)
        so downstream stages can distinguish "no routes" from "routing
        not attempted".
        """
        t0 = time.monotonic()
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
            if not routes:
                return (
                    ReplayResult(
                        event_id=event.event_id,
                        stage="route",
                        status="failed",
                        output=[],
                        duration_ms=_elapsed_ms(t0),
                    ),
                    routes,
                )
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="route",
                    status="passed",
                    output=routes,
                    duration_ms=_elapsed_ms(t0),
                ),
                routes,
            )
        except Exception as exc:
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

    async def _stage_render(self, event: CanonicalEvent) -> ReplayResult:
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
    ) -> ReplayResult:
        """Execute delivery plans for *event*.

        This is the **only** stage with side effects – it delivers to
        adapters.  Only executed in :attr:`ReplayMode.BEST_EFFORT` mode.
        """
        t0 = time.monotonic()
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
        try:
            receipts = await self._pipeline.deliver(event, plan_result)
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="passed",
                output=receipts,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )
