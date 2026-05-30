"""Replay engine: orchestrates deterministic re-processing of historical events."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from medre.core.engine.replay.delivery import (
    _filter_plans_by_adapter,
    _filter_plans_by_capability,
    _replay_delivery_envelope,
)
from medre.core.engine.replay.helpers import (
    _elapsed_ms,
    _event_matches_filters,
    _request_to_filter,
    _resolve_stages,
    _verify_immutability,
)
from medre.core.engine.replay.protocols import _EventBusProtocol, _PipelineProtocol
from medre.core.engine.replay.routing import (
    _clean_routing_metadata,
    _filter_replay_loops,
)
from medre.core.engine.replay.types import (
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
)
from medre.core.events import CanonicalEvent, is_registered
from medre.core.storage.backend import StorageBackend

if TYPE_CHECKING:
    from medre.core.observability.metrics import Diagnostician
    from medre.core.supervision.accounting import RuntimeAccounting
    from medre.core.supervision.capacity import CapacityController


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Replay engine
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
        capacity_controller: CapacityController | None = None,
        accounting: RuntimeAccounting | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._event_bus = event_bus
        self._diagnostician = diagnostician
        self._capacity_controller: CapacityController | None = capacity_controller
        self._accounting: RuntimeAccounting | None = accounting
        self._cancel_event: asyncio.Event = asyncio.Event()

    def set_capacity_controller(self, cc: CapacityController) -> None:
        """Wire a :class:`~medre.core.supervision.capacity.CapacityController`.

        When set, :meth:`_stage_deliver` acquires a replay slot
        before delivery in BEST_EFFORT mode and releases it on
        completion.
        """
        self._capacity_controller = cc

    # -- Cancellation -------------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation of any in-flight replay.

        Idempotent: calling more than once is harmless.  After
        cancellation the engine's ``replay()`` async generator stops
        iterating new events and skips remaining stages for the
        current event, producing ``ReplayResult(status="skipped",
        error="replay_cancelled")`` for each abandoned stage.

        The cancellation signal persists until :meth:`reset_cancellation`
        is called (or a new :class:`ReplayEngine` is constructed).
        """
        self._cancel_event.set()
        _logger.info("Replay cancellation requested")

    @property
    def is_cancelled(self) -> bool:
        """Return ``True`` if cancellation has been requested."""
        return self._cancel_event.is_set()

    def reset_cancellation(self) -> None:
        """Clear the cancellation signal so a new replay can proceed.

        Useful when a :class:`ReplayEngine` instance is reused across
        multiple replay operations (e.g. in long-lived runtimes).
        """
        self._cancel_event.clear()

    # -- Public API ---------------------------------------------------------

    async def replay(
        self,
        request: ReplayRequest,
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
                if self._cancel_event.is_set():
                    _logger.info(
                        "Replay cancelled --- stopping correlation-id iteration",
                    )
                    return
                if event is None:
                    async for result in self._replay_missing(event_id, stages):
                        yield result
                else:
                    async for result in self._replay_event_safe(
                        event,
                        stages,
                        request,
                    ):
                        yield result
        else:
            event_filter = _request_to_filter(request)
            async for event in self._storage.query(event_filter):  # type: ignore[union-attr]
                if self._cancel_event.is_set():
                    _logger.info("Replay cancelled --- stopping event iteration")
                    return
                async for result in self._replay_event_safe(
                    event,
                    stages,
                    request,
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
        self,
        request: ReplayRequest,
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
            # All stages completed for this event -> processed.
            if self._accounting is not None:
                self._accounting.record_replay_processed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if mode is ReplayMode.BEST_EFFORT:
                if self._diagnostician is not None:
                    self._diagnostician.record_adapter_failure(
                        event.event_id,
                        "replay",
                        f"Unexpected error in BEST_EFFORT mode: {exc}",
                    )
                if self._accounting is not None:
                    self._accounting.record_replay_rejected()
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
                event_id,
                "Event not found in storage",
            )
        if self._accounting is not None:
            self._accounting.record_replay_rejected()
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
        route_result: list[tuple[Any, Any]] | None = None
        plan_result: list[Any] | None = None
        enriched_event: CanonicalEvent | None = None

        # Immutability guard: checkpoint event identity before processing.
        _verify_immutability(event, event.event_id)

        for stage in stages:
            # Check cancellation between stages --- skip remaining stages
            # for this event if cancellation was requested mid-event.
            if self._cancel_event.is_set():
                yield ReplayResult(
                    event_id=event.event_id,
                    stage=stage,
                    status="skipped",
                    error="replay_cancelled",
                    lineage=list(event.lineage),
                )
                continue
            if stage == "store":
                result = await self._stage_store(event)
            elif stage == "route":
                result, route_result, enriched_event = await self._stage_route(
                    event,
                    request=request,
                )
            elif stage == "plan":
                result, plan_result = await self._stage_plan(
                    enriched_event or event,
                    route_result,
                )
            elif stage == "render":
                result = await self._stage_render(event, mode)
            elif stage == "deliver":
                result = await self._stage_deliver(
                    enriched_event or event,
                    plan_result,
                    request,
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
                        event.event_id,
                        "Event not found in storage",
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
        self,
        event: CanonicalEvent,
        *,
        request: ReplayRequest,
    ) -> tuple[ReplayResult, list[tuple[Any, Any]] | None, CanonicalEvent | None]:
        """Route *event* against current routes.

        Returns the :class:`ReplayResult`, the route--plan pairs for
        use by downstream stages, and the enriched event (or ``None``
        if routing failed before enrichment).  If no routes match, the
        result status is ``"failed"`` and the route data is an empty
        list (not None) so downstream stages can distinguish "no routes"
        from "routing not attempted".

        The pipeline's ``route_event`` returns
        ``(enriched_event, list[tuple[Route, DeliveryPlan]])``.  The
        enriched event carries :class:`RoutingMetadata` with
        ``matched_routes`` and ``route_trace`` and is returned so
        that downstream stages (plan, deliver) operate on the
        pipeline-enriched event rather than the original.  After
        filtering by ``route_ids``, the enriched event's metadata is
        cleaned to contain only the retained routes.

        Route-aware replay adds :class:`ReplayRouteAttribution` to the
        result and filters out routes that would create replay loops.
        A replay loop is detected when a route would deliver back to the
        event's ``source_adapter`` or when the event's routing metadata
        (matched_routes or route_trace) indicates it was already routed
        through the same route.

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
                None,
            )
        try:
            # Save routing metadata *before* route_event enriches the event.
            # _filter_replay_loops must check original routing to avoid
            # false positives --- route_event populates matched_routes and
            # route_trace with the *current* pass, which should not be
            # treated as "previously matched".
            original_routing = event.metadata.routing

            result = await self._pipeline.route_event(event)
            # Unwrap real pipeline return: (CanonicalEvent, list[tuple[Route, DeliveryPlan]])
            # Use the enriched event (may have route_trace metadata).
            if isinstance(result, tuple) and len(result) == 2:
                event, routes = result
            else:
                routes = result  # type: ignore[assignment]

            # Filter by explicit route_ids when provided.
            if requested_route_ids:
                allowed = set(requested_route_ids)
                routes = [
                    (r, p) for r, p in routes if getattr(r, "id", None) in allowed
                ]
                # Clean enriched event metadata so filtered-out routes
                # don't leak into matched_routes / route_trace.
                event = _clean_routing_metadata(event, allowed)
                # Warn about requested route IDs not found among matched
                # routes.  This covers disabled routes (the router won't
                # return them) and routes that don't match the event's
                # source filter.
                found_ids = {getattr(r, "id", None) for r, _ in routes}
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
                        event.event_id,
                        "No routes matched",
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
                    event,
                )

            # Route-aware loop prevention: filter routes that would
            # deliver back to the event's source adapter or match routes
            # the event was already routed through.  Pass the original
            # (pre-enrichment) routing metadata so that the current
            # routing pass is not mistaken for a previous one.
            loop_warnings, filtered_routes = _filter_replay_loops(
                event,
                routes,
                previous_routing=original_routing,
            )

            # Clean enriched event metadata to reflect only the routes
            # that survived loop prevention filtering.
            if filtered_routes and len(filtered_routes) < len(routes):
                surviving_ids = {getattr(r, "id", None) for r, _ in filtered_routes}
                event = _clean_routing_metadata(event, surviving_ids)

            # Build route attribution for this replay.
            route_ids = tuple(r.id for r, _ in filtered_routes if hasattr(r, "id"))
            target_adapters: list[str] = []
            for _, plan_or_target in filtered_routes:
                plan = plan_or_target
                # Real pipeline returns DeliveryPlan objects with .target.adapter.
                # Stub pipelines may return raw target objects or lists of them.
                target_obj = getattr(plan, "target", plan)
                if isinstance(target_obj, (list, tuple)):
                    subtargets = target_obj
                else:
                    subtargets = [target_obj]
                for sub in subtargets:
                    adapter = getattr(sub, "adapter", None)
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
                    event,
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
                event,
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_planner_failure(
                    event.event_id,
                    str(exc),
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
                None,
            )

    async def _stage_plan(
        self,
        event: CanonicalEvent,
        route_result: list[tuple[Any, Any]] | None,
    ) -> tuple[ReplayResult, list[Any] | None]:
        """Build delivery plans for *event* based on routing results.

        Returns the :class:`ReplayResult` and the delivery plans for use
        by downstream stages.

        When *route_result* already contains ``DeliveryPlan`` objects
        (i.e. from the real PipelineRunner), the route--plan pairs are
        preserved as ``list[tuple[Route, DeliveryPlan]]`` so that
        :meth:`_stage_deliver` can call ``deliver_to_targets``.
        For stub pipelines where the second element is not a
        ``DeliveryPlan``, the ``plan_delivery`` fallback path is used
        and bare plans are returned so that :meth:`_stage_deliver`
        can process them.
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

        # Empty route_result means routes were filtered out (e.g. loop
        # prevention) --- nothing to plan.
        if not route_result:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="skipped",
                    error="No routes matched after filtering",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )

        # If route_result items already contain DeliveryPlan objects
        # (real pipeline returns list[tuple[Route, DeliveryPlan]]),
        # preserve the route--plan pairs.  For stub pipelines where the
        # second element is not a DeliveryPlan, we fall through to the
        # plan_delivery path below.
        plans: list[Any] = []
        all_delivery_plans = True
        for route, plan_or_target in route_result:
            if hasattr(plan_or_target, "target") and hasattr(plan_or_target, "plan_id"):
                # Preserve route--plan pairs so that _stage_deliver can
                # call deliver_to_targets with the correct shape.
                plans.append((route, plan_or_target))
            else:
                all_delivery_plans = False
                break

        if all_delivery_plans and plans:
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

        # Fall back to pipeline's plan_delivery for stubs.
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
        if not hasattr(self._pipeline, "plan_delivery"):
            raise RuntimeError(
                "Pipeline has no deliver_to_targets and no plan_delivery; "
                "cannot build delivery plans for event_id=" + event.event_id
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
                    event.event_id,
                    str(exc),
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
            if hasattr(self._pipeline, "transform_event"):
                transformed = await self._pipeline.transform_event(event)
            else:
                _logger.debug(
                    "Pipeline has no transform_event; skipping transform "
                    "for event_id=%s",
                    event.event_id,
                )
                transformed = event
            if hasattr(self._pipeline, "render_event"):
                rendered = await self._pipeline.render_event(transformed)
            else:
                _logger.debug(
                    "Pipeline has no render_event; skipping render " "for event_id=%s",
                    event.event_id,
                )
                rendered = transformed
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

        This is the **only** stage with side effects -- it delivers to
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
                plan_result,
                request.target_adapters,
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

        # Capability-aware skip: for BEST_EFFORT mode, check if the
        # event kind is supported by the target adapter.  Skip delivery
        # with a descriptive error when the adapter lacks the required
        # capability.  Non-BEST_EFFORT modes are not affected.
        if mode is ReplayMode.BEST_EFFORT:
            # Extract adapters dict from the pipeline collaborator.
            _adapters: dict[str, Any] | None = None
            if self._pipeline is not None:
                _cfg = getattr(self._pipeline, "_config", None)
                if _cfg is not None:
                    _adapters = getattr(_cfg, "adapters", None)
            _before_filter = len(plan_result)
            plan_result = _filter_plans_by_capability(
                event,
                plan_result,
                _adapters,
            )
            _suppressed = _before_filter - len(plan_result)
            if _suppressed > 0 and self._accounting is not None:
                for _ in range(_suppressed):
                    self._accounting.record_capability_suppressed()
            if not plan_result:
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="skipped",
                    error=(
                        f"capability_suppressed: {event.event_kind} "
                        f"not supported by target adapter(s)"
                    ),
                    duration_ms=_elapsed_ms(t0),
                )

        # Capacity guard: acquire replay slot for BEST_EFFORT delivery.
        _capacity_acquired = False
        if self._capacity_controller is not None and mode is ReplayMode.BEST_EFFORT:
            acquired = await self._capacity_controller.acquire_replay()
            if not acquired:
                if self._accounting is not None:
                    self._accounting.record_capacity_rejection()
                replay_error = (
                    "replay_rejected_shutdown"
                    if not self._capacity_controller.accepting_work
                    else "replay_capacity_exceeded"
                )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="error",
                    error=replay_error,
                    duration_ms=_elapsed_ms(t0),
                )
            _capacity_acquired = True

        try:
            # Detect real pipeline by data format: if plan_result contains
            # (Route, DeliveryPlan) tuples, use deliver_to_targets.  This
            # avoids false-positives from AsyncMock which auto-creates every
            # attribute (making hasattr unreliable).
            _has_route_plan_pairs = (
                bool(plan_result)
                and isinstance(plan_result[0], tuple)
                and len(plan_result[0]) == 2
                and hasattr(plan_result[0][1], "target")
                and hasattr(plan_result[0][1], "plan_id")
            )
            if _has_route_plan_pairs:
                # Real pipeline: plan_result is list[tuple[Route, DeliveryPlan]].
                outcomes = await self._pipeline.deliver_to_targets(
                    event,
                    plan_result,
                    source="replay",
                    replay_run_id=request.run_id or None,
                )
                replay_output = _replay_delivery_envelope(outcomes)
            else:
                # Stub pipeline: plan_result is list[Any] (bare plans).
                receipts = await self._pipeline.deliver(event, plan_result)
                replay_output = _replay_delivery_envelope(receipts)
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="passed",
                output=replay_output,
                duration_ms=_elapsed_ms(t0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_adapter_failure(
                    event.event_id,
                    "replay",
                    str(exc),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )
        finally:
            if _capacity_acquired and self._capacity_controller is not None:
                await self._capacity_controller.release_replay()
