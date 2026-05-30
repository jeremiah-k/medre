"""Shared helpers for replay routing tests.

Provides event builders, router factory, and re-exports of StubPipeline /
make_engine from :mod:`tests.helpers.replay` used across the split routing
test modules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events import CanonicalEvent, EventMetadata, RoutingMetadata
from medre.core.routing import Route, Router, RouteSource, RouteTarget

# Re-export from the existing replay helper to avoid duplication.
from tests.helpers.replay import StubPipeline, make_engine  # noqa: F401

# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def make_replay_event(
    source_adapter: str = "adapter_a",
    event_kind: str = "message.created",
    source_channel_id: str | None = "ch-0",
    *,
    event_id: str = "evt-1",
    metadata: EventMetadata | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for routing tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=metadata or EventMetadata(),
    )


def make_event_with_routing(
    source_adapter: str = "adapter_a",
    matched_routes: tuple[str, ...] = ("route-1",),
    route_trace: tuple[str, ...] = (),
) -> CanonicalEvent:
    """Create an event with routing metadata indicating prior routing."""
    return make_replay_event(
        source_adapter=source_adapter,
        metadata=EventMetadata(
            routing=RoutingMetadata(
                matched_routes=matched_routes,
                route_trace=route_trace,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def make_router(
    source: str = "adapter_a",
    dests: tuple[str, ...] = ("adapter_b",),
    route_id: str = "route-1",
    event_kinds: tuple[str, ...] = (),
) -> Router:
    """Create a Router with a single route."""
    route = Route(
        id=route_id,
        source=RouteSource(adapter=source, event_kinds=event_kinds, channel=None),
        targets=[RouteTarget(adapter=d) for d in dests],
    )
    return Router(routes=[route])
