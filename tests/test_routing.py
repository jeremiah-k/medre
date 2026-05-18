"""Tests for Router: route matching by adapter / event_kind / channel,
multiple routes, exclusive ownership conflicts, add/remove routes,
and RouteTarget resolution.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.metadata import RoutingMetadata
from medre.core.routing import (
    Route,
    RouteConflictError,
    RouteDestination,
    Router,
    RouteSource,
    RouteTarget,
)


def _make_event(
    source_adapter: str = "fake_transport",
    event_kind: str = "message.created",
    channel: str | None = "ch-0",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=channel,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hi"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Matching
# ===================================================================


class TestRouteMatching:
    """Router.match() filters by adapter, event_kind, and channel."""

    def test_match_by_adapter(self) -> None:
        route = Route(
            id="r1",
            source=RouteSource(adapter="fake_transport", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fake_presentation")],
        )
        router = Router(routes=[route])
        event = _make_event(source_adapter="fake_transport")
        assert route in router.match(event)

    def test_match_by_event_kind(self) -> None:
        route = Route(
            id="r2",
            source=RouteSource(
                adapter=None, event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(event_kind="message.created")
        assert route in router.match(event)

    def test_match_by_channel(self) -> None:
        route = Route(
            id="r3",
            source=RouteSource(adapter=None, event_kinds=(), channel="ch-0"),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(channel="ch-0")
        assert route in router.match(event)

    def test_no_match_when_adapter_differs(self) -> None:
        route = Route(
            id="r4",
            source=RouteSource(adapter="discord", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(source_adapter="meshtastic")
        assert router.match(event) == []

    def test_no_match_when_event_kind_differs(self) -> None:
        route = Route(
            id="r5",
            source=RouteSource(
                adapter=None, event_kinds=("telemetry.received",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(event_kind="message.created")
        assert router.match(event) == []

    def test_no_match_when_channel_differs(self) -> None:
        route = Route(
            id="r6",
            source=RouteSource(adapter=None, event_kinds=(), channel="ch-99"),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(channel="ch-0")
        assert router.match(event) == []

    def test_wildcard_source_matches_everything(self) -> None:
        """RouteSource with all None / empty tuple matches any event."""
        route = Route(
            id="wildcard",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        event = _make_event(
            source_adapter="anything", event_kind="any.kind", channel="any-ch"
        )
        assert router.match(event) == [route]

    def test_disabled_route_is_ignored(self) -> None:
        route = Route(
            id="disabled",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="target")],
            enabled=False,
        )
        router = Router(routes=[route])
        event = _make_event()
        assert router.match(event) == []


class TestMultipleRoutes:
    """Multiple routes can match the same event."""

    def test_multiple_routes_match_same_event(self) -> None:
        r1 = Route(
            id="r1",
            source=RouteSource(adapter="fake_transport", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="t1")],
        )
        r2 = Route(
            id="r2",
            source=RouteSource(
                adapter=None, event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="t2")],
        )
        router = Router(routes=[r1, r2])
        event = _make_event(
            source_adapter="fake_transport", event_kind="message.created"
        )
        matched = router.match(event)
        assert len(matched) == 2
        assert r1 in matched
        assert r2 in matched


# ===================================================================
# Conflict validation
# ===================================================================


class TestConflictValidation:
    """validate_no_conflicts enforces exclusive ownership."""

    def test_shared_routes_do_not_conflict(self) -> None:
        """Two shared routes with identical sources are fine."""
        r1 = Route(
            id="s1",
            source=RouteSource(adapter="a", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t1")],
            ownership="shared",
        )
        r2 = Route(
            id="s2",
            source=RouteSource(adapter="a", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t2")],
            ownership="shared",
        )
        router = Router(routes=[r1, r2])
        # Should not raise.
        router.validate_no_conflicts()

    def test_exclusive_routes_raise_on_overlap(self) -> None:
        """Two exclusive routes with overlapping sources raise RouteConflictError."""
        r1 = Route(
            id="e1",
            source=RouteSource(adapter="a", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t1")],
            ownership="exclusive",
        )
        r2 = Route(
            id="e2",
            source=RouteSource(adapter="a", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t2")],
            ownership="exclusive",
        )
        router = Router(routes=[r1, r2])
        with pytest.raises(RouteConflictError) as exc_info:
            router.validate_no_conflicts()
        err = exc_info.value
        assert err.route_a_id in ("e1", "e2")
        assert err.route_b_id in ("e1", "e2")

    def test_exclusive_routes_no_overlap_passes(self) -> None:
        """Exclusive routes with disjoint sources pass validation."""
        r1 = Route(
            id="e1",
            source=RouteSource(adapter="a", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t1")],
            ownership="exclusive",
        )
        r2 = Route(
            id="e2",
            source=RouteSource(adapter="b", event_kinds=("k1",), channel=None),
            targets=[RouteTarget(adapter="t2")],
            ownership="exclusive",
        )
        router = Router(routes=[r1, r2])
        router.validate_no_conflicts()  # No raise.


# ===================================================================
# Add / remove routes
# ===================================================================


class TestAddRemoveRoutes:
    """Router.add_route / remove_route."""

    def test_add_route(self) -> None:
        router = Router()
        route = Route(
            id="new",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="t")],
        )
        router.add_route(route)
        event = _make_event()
        assert router.match(event) == [route]

    def test_remove_route(self) -> None:
        route = Route(
            id="removeme",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="t")],
        )
        router = Router(routes=[route])
        router.remove_route("removeme")
        event = _make_event()
        assert router.match(event) == []

    def test_remove_nonexistent_raises_key_error(self) -> None:
        router = Router()
        with pytest.raises(KeyError):
            router.remove_route("nope")


# ===================================================================
# RouteTarget resolution
# ===================================================================


class TestResolveTargets:
    """Router.resolve_targets returns the route's target list."""

    def test_resolve_targets_returns_route_targets(self) -> None:
        targets = [
            RouteTarget(adapter="t1", channel="c1"),
            RouteTarget(adapter="t2", channel="c2"),
        ]
        route = Route(
            id="r1",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=targets,
        )
        router = Router(routes=[route])
        event = _make_event()
        resolved = router.resolve_targets(event, route)
        assert resolved == targets
        assert len(resolved) == 2


# ===================================================================
# RouteDestination
# ===================================================================


class TestRouteDestination:
    """RouteDestination identity-based addressing."""

    def test_construction(self) -> None:
        dest = RouteDestination(
            kind="lxmf_destination",
            destination_hash="abc123",
            destination_name="Node-42",
            metadata={"hop_count": 3},
        )
        assert dest.kind == "lxmf_destination"
        assert dest.destination_hash == "abc123"
        assert dest.destination_name == "Node-42"
        assert dest.metadata["hop_count"] == 3

    def test_route_target_with_destination(self) -> None:
        dest = RouteDestination(
            kind="channel", destination_hash=None, destination_name=None
        )
        target = RouteTarget(adapter="matrix", destination=dest)
        assert target.destination is not None
        assert target.destination.kind == "channel"


# ===================================================================
# RoutingMetadata route_trace
# ===================================================================


class TestRoutingMetadataRouteTrace:
    """RoutingMetadata.route_trace default and setting."""

    def test_default_empty_tuple(self) -> None:
        """Default route_trace is an empty tuple."""
        rm = RoutingMetadata()
        assert rm.route_trace == ()

    def test_route_trace_with_values(self) -> None:
        """route_trace can be set at construction."""
        rm = RoutingMetadata(route_trace=("r1", "r2"))
        assert rm.route_trace == ("r1", "r2")

    def test_route_trace_preserved_in_event(self) -> None:
        """route_trace survives round-trip through EventMetadata."""
        routing = RoutingMetadata(
            matched_routes=("r1",),
            route_trace=("r1", "r2"),
        )
        meta = EventMetadata(routing=routing)
        event = _make_event()
        event2 = CanonicalEvent(
            event_id=event.event_id,
            event_kind=event.event_kind,
            schema_version=event.schema_version,
            timestamp=event.timestamp,
            source_adapter=event.source_adapter,
            source_transport_id=event.source_transport_id,
            source_channel_id=event.source_channel_id,
            parent_event_id=event.parent_event_id,
            lineage=event.lineage,
            relations=event.relations,
            payload=event.payload,
            metadata=meta,
        )
        assert event2.metadata.routing is not None
        assert event2.metadata.routing.route_trace == ("r1", "r2")

    def test_route_trace_frozen(self) -> None:
        """route_trace is immutable on frozen struct."""
        rm = RoutingMetadata(route_trace=("r1",))
        with pytest.raises((TypeError, AttributeError)):
            rm.route_trace = ("r2",)  # type: ignore[misc]
