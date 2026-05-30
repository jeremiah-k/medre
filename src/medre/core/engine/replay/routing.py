"""Replay routing: metadata cleanup and loop-prevention filtering."""

from __future__ import annotations

from collections import Counter
from typing import AbstractSet, Any

import msgspec

from medre.core.events import CanonicalEvent

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
    # Also check route_trace for historical traversal.  A route ID that
    # appears only once in the trace was added by the current routing pass
    # --- do NOT filter it.  Only filter when the same route ID appears
    # multiple times (indicating a prior routing pass).
    if routing_meta is not None and routing_meta.route_trace:
        trace = routing_meta.route_trace
        # Build a multiset: only routes appearing >1 time are "previously matched".
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
