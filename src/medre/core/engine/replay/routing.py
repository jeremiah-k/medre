"""Replay routing: metadata cleanup and loop-prevention filtering."""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import AbstractSet, Any

import msgspec

from medre.core.engine.replay.helpers import _elapsed_ms
from medre.core.engine.replay.types import (
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
)
from medre.core.events import CanonicalEvent

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing metadata cleanup
# ---------------------------------------------------------------------------


def _clean_routing_metadata(
    event: CanonicalEvent,
    allowed_route_ids: AbstractSet[str | None],
) -> CanonicalEvent:
    """Remove filtered-out route IDs from the enriched event's RoutingMetadata.

    After ``route_event`` populates ``matched_routes`` and ``route_trace``
    with ALL matched routes, this function narrows them to only the routes
    that survive replay filtering (explicit ``route_ids`` or loop
    prevention).  Because :class:`RoutingMetadata` is frozen, a new
    struct is built via ``msgspec.structs.replace`` and a new
    :class:`CanonicalEvent` is returned.
    """
    routing_meta = event.metadata.routing
    if routing_meta is None:
        return event

    cleaned_matched = tuple(
        rid for rid in routing_meta.matched_routes if rid in allowed_route_ids
    )
    cleaned_trace = tuple(
        rid for rid in routing_meta.route_trace if rid in allowed_route_ids
    )

    if (
        cleaned_matched == routing_meta.matched_routes
        and cleaned_trace == routing_meta.route_trace
    ):
        return event

    new_routing = msgspec.structs.replace(
        routing_meta,
        matched_routes=cleaned_matched,
        route_trace=cleaned_trace,
    )
    new_metadata = msgspec.structs.replace(event.metadata, routing=new_routing)
    return msgspec.structs.replace(event, metadata=new_metadata)


_UNSET = object()


# ---------------------------------------------------------------------------
# Replay loop prevention
# ---------------------------------------------------------------------------


def _filter_replay_loops(
    event: CanonicalEvent,
    routes: list[tuple[Any, Any]],
    *,
    previous_routing: Any = _UNSET,
) -> tuple[list[str], list[tuple[Any, Any]]]:
    """Filter routes that would create a replay routing loop.

    A replay loop is detected when:

    1. A route would deliver an event back to its own ``source_adapter``.
    2. The event's existing ``RoutingMetadata.matched_routes`` overlaps
       with a matched route ID, indicating the event was previously
       routed through the same route.
    3. The event's ``RoutingMetadata.route_trace`` contains a matched
       route ID, indicating a historical traversal through the same route.

    Parameters
    ----------
    event:
        The event being replayed (enriched by route_event).
    routes:
        Matched route--plan pairs from the current routing pass.
    previous_routing:
        The routing metadata from *before* route_event enriched the
        event.  When provided (including ``None`` for a fresh event),
        loop detection uses this value.  When left as the default
        sentinel, falls back to ``event.metadata.routing`` for backward
        compatibility with callers that do not track pre-enrichment state.

    Returns a tuple of ``(loop_warnings, filtered_routes)``.  Loop-causing
    routes are removed from the filtered list and a warning string is
    added for each.
    """
    source = event.source_adapter
    warnings: list[str] = []
    filtered: list[tuple[Any, Any]] = []

    # Pre-compute previously matched routes.  When previous_routing is
    # explicitly provided (called from _stage_route), use it --- even if
    # it is None (fresh event, no prior routing).  When left as the
    # default sentinel, fall back to the event's current routing
    # metadata as a fallback when no explicit routing context is provided.
    prev_matched: set[str] = set()
    if previous_routing is _UNSET:
        routing_meta = event.metadata.routing
    else:
        routing_meta = previous_routing
    if routing_meta is not None and routing_meta.matched_routes:
        prev_matched = set(routing_meta.matched_routes)
    # Also check route_trace for historical traversal.  The count > 1
    # check is intentional: when ``previous_routing`` is the default
    # sentinel (``_UNSET``), this function falls back to the event's
    # *post-enrichment* routing metadata, which includes routes added
    # by the current routing pass.  A route appearing exactly once may
    # be from the current pass, so filtering it would be a false
    # positive.  Only routes appearing more than once (indicating
    # accumulation across prior passes) are treated as "previously
    # matched".
    #
    # When ``previous_routing`` is explicitly provided (the normal path
    # from ``_stage_route``), the matched_routes check above already
    # catches all prior-pass routes.  The route_trace check here is a
    # supplementary safety net for the sentinel-fallback path.
    if routing_meta is not None and routing_meta.route_trace:
        trace = routing_meta.route_trace
        trace_counts = Counter(trace)
        prev_matched |= {rid for rid, cnt in trace_counts.items() if cnt > 1}

    for route, plan_or_targets in routes:
        route_id = getattr(route, "id", None)

        # Resolve target adapters: real pipeline returns DeliveryPlan with
        # .target.adapter; stubs may return a list of target objects.
        target_adapters: set[str | None] = set()
        if isinstance(plan_or_targets, (list, tuple)):
            for t in plan_or_targets:
                target_adapters.add(getattr(t, "adapter", None))
        else:
            # Single DeliveryPlan or target object.
            target = getattr(plan_or_targets, "target", plan_or_targets)
            target_adapters.add(getattr(target, "adapter", None))

        # Check 1: would this route deliver back to the source?
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

        filtered.append((route, plan_or_targets))

    return warnings, filtered


# ---------------------------------------------------------------------------
# Replay routing mixin
# ---------------------------------------------------------------------------


class _ReplayRoutingMixin:
    """Mixin providing replay routing logic for the replay engine.

    Uses ``self._pipeline`` and ``self._diagnostician`` supplied by the
    host class via MRO.
    """

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
                    [],
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
