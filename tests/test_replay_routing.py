"""Replay + routing correctness tests (core).

Covers:
* Replay through routes preserves route attribution in namespaced metadata
* Route metadata namespacing: attribution lives on ReplayResult, not CanonicalEvent
* Loop prevention under replay: events do not route back to source
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from medre.core.engine.replay import (
    ReplayMode,
    ReplayRequest,
    ReplayRouteAttribution,
    _filter_replay_loops,
)
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay_routing import (
    StubPipeline,
    make_engine,
    make_event_with_routing,
    make_replay_event,
    make_router,
)

# ===================================================================
# 1. Route attribution in replay results
# ===================================================================


class TestReplayRouteAttribution:
    """Route attribution is captured in ReplayResult during route-aware replay."""

    async def test_route_attribution_in_re_route_result(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RE_ROUTE mode captures route attribution on the route result."""
        event = make_replay_event(source_adapter="adapter_a")
        await temp_storage.append(event)

        router = make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]

        # store + route + plan
        assert len(results) == 3
        route_result = results[1]
        assert route_result.stage == "route"
        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        attr = route_result.route_attribution
        assert attr.is_replay is True
        assert attr.source_adapter == "adapter_a"
        assert "adapter_b" in attr.target_adapters
        assert "route-1" in attr.route_ids
        assert attr.replay_mode == "re_route"

    async def test_route_attribution_in_dry_run_result(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """DRY_RUN mode captures route attribution on the route result."""
        event = make_replay_event(source_adapter="src")
        await temp_storage.append(event)

        router = make_router(source="src", dests=("dst",), route_id="r-dry")
        rendering = RenderingPipeline()
        rendering.register(TextRenderer(), priority=100)
        pipeline = StubPipeline(router=router, rendering_pipeline=rendering)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        results = [r async for r in engine.replay(request)]

        route_result = results[1]
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.replay_mode == "dry_run"

    async def test_route_attribution_in_best_effort_result(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT mode captures route attribution on the route result."""
        event = make_replay_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]

        route_result = results[1]
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.replay_mode == "best_effort"

    async def test_no_attribution_for_strict_mode(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """STRICT mode does not produce route results, so no attribution."""
        event = make_replay_event()
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].route_attribution is None

    async def test_no_attribution_when_no_routes_match(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """No matching routes produces attribution with empty route_ids."""
        event = make_replay_event(source_adapter="adapter_x")
        await temp_storage.append(event)

        router = make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]

        route_result = results[1]
        assert route_result.status == "failed"
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.route_ids == ()
        assert route_result.route_attribution.target_adapters == ()
        assert route_result.route_attribution.source_adapter == "adapter_x"


# ===================================================================
# 2. Route metadata namespacing
# ===================================================================


class TestRouteMetadataNamespacing:
    """Route attribution lives in ReplayResult metadata, not on CanonicalEvent."""

    async def test_attribution_not_on_canonical_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route attribution is not stamped onto the canonical event."""
        event = make_replay_event()
        await temp_storage.append(event)

        router = make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        _ = [r async for r in engine.replay(request)]

        # Original event must be unchanged
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        # No replay-specific metadata was added to the event
        assert (
            stored.metadata.routing is None
            or stored.metadata.routing == event.metadata.routing
        )

    def test_attribution_to_dict_is_deterministic(self) -> None:
        """ReplayRouteAttribution.to_dict() produces deterministic output."""
        attr = ReplayRouteAttribution(
            route_ids=("r1", "r2"),
            source_adapter="a",
            target_adapters=("b", "c"),
            replay_mode="re_route",
            loop_warnings=("warning 1",),
        )
        d = attr.to_dict()
        assert d == {
            "is_replay": True,
            "loop_warnings": ["warning 1"],
            "replay_mode": "re_route",
            "route_ids": ["r1", "r2"],
            "run_id": "",
            "source_adapter": "a",
            "target_adapters": ["b", "c"],
        }

    def test_attribution_is_frozen(self) -> None:
        """ReplayRouteAttribution is immutable."""
        attr = ReplayRouteAttribution()
        with pytest.raises(AttributeError):
            attr.is_replay = False  # type: ignore[misc]

    def test_attribution_defaults(self) -> None:
        """ReplayRouteAttribution has sensible defaults."""
        attr = ReplayRouteAttribution()
        assert attr.route_ids == ()
        assert attr.source_adapter == ""
        assert attr.target_adapters == ()
        assert attr.replay_mode == ""
        assert attr.is_replay is True
        assert attr.loop_warnings == ()


# ===================================================================
# 3. Loop prevention under replay
# ===================================================================


class TestReplayLoopPrevention:
    """Replay loop prevention: events do not route back to source."""

    async def test_loop_prevention_source_feedback(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event from adapter_a is not routed back to adapter_a."""
        event = make_replay_event(source_adapter="adapter_a")
        await temp_storage.append(event)

        # Route: adapter_a → adapter_a (would create feedback loop)
        router = make_router(source="adapter_a", dests=("adapter_a",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]

        route_result = results[1]
        assert route_result.stage == "route"
        # Route should be filtered out due to loop
        assert route_result.status == "failed"
        assert route_result.route_attribution is not None
        assert len(route_result.route_attribution.loop_warnings) > 0
        assert "adapter_a" in route_result.route_attribution.loop_warnings[0]

    async def test_loop_prevention_with_bidirectional_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Bidirectional routes: each event only routes away from its source."""
        event_a = make_replay_event(
            source_adapter="a",
            event_id="evt-from-a",
        )
        event_b = make_replay_event(
            source_adapter="b",
            event_id="evt-from-b",
        )
        await temp_storage.append(event_a)
        await temp_storage.append(event_b)

        # Routes: a→b and b→a
        route_fwd = Route(
            id="fwd",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        route_rev = Route(
            id="rev",
            source=RouteSource(adapter="b", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="a")],
        )
        router = Router(routes=[route_fwd, route_rev])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # 2 events × 3 stages = 6 results
        assert len(results) == 6

        # First event (from a): should only match fwd route (a→b)
        route_a = results[1]
        assert route_a.status == "passed"
        assert route_a.route_attribution is not None
        assert "fwd" in route_a.route_attribution.route_ids
        # rev route (b→a) filtered because it would deliver back to source 'a'
        assert "rev" not in route_a.route_attribution.route_ids

        # Second event (from b): should only match rev route (b→a)
        route_b = results[4]
        assert route_b.status == "passed"
        assert route_b.route_attribution is not None
        assert "rev" in route_b.route_attribution.route_ids
        # fwd route (a→b) filtered because it would deliver back to source 'b'
        assert "fwd" not in route_b.route_attribution.route_ids

    async def test_loop_prevention_lineage_metadata(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event with routing metadata showing prior route match is filtered."""
        # Event already has routing metadata indicating it was routed through
        # route-1 in a previous pass.
        event = make_event_with_routing(
            source_adapter="adapter_a",
            matched_routes=("route-1",),
        )
        await temp_storage.append(event)

        router = make_router(
            source="adapter_a",
            dests=("adapter_b",),
            route_id="route-1",
        )
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        # Route should be filtered due to prior routing through same route
        assert route_result.status == "failed"
        assert route_result.route_attribution is not None
        assert len(route_result.route_attribution.loop_warnings) > 0
        assert "route-1" in route_result.route_attribution.loop_warnings[0]

    async def test_no_false_positive_loop_on_different_route(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event routed through route-1 is NOT filtered from route-2."""
        event = make_event_with_routing(
            source_adapter="adapter_a",
            matched_routes=("route-1",),
        )
        await temp_storage.append(event)

        # route-2 is different from route-1 and targets adapter_b (not source)
        router = make_router(
            source="adapter_a",
            dests=("adapter_b",),
            route_id="route-2",
        )
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "route-2" in route_result.route_attribution.route_ids
        assert len(route_result.route_attribution.loop_warnings) == 0

    def test_filter_replay_loops_unit_no_loops(self) -> None:
        """_filter_replay_loops returns no warnings when no loops exist."""
        event = make_replay_event(source_adapter="a")
        routes = [
            (
                Route(
                    id="r1",
                    source=RouteSource(adapter="a", event_kinds=(), channel=None),
                    targets=[RouteTarget(adapter="b")],
                ),
                [RouteTarget(adapter="b")],
            ),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert warnings == []
        assert len(filtered) == 1

    def test_filter_replay_loops_unit_source_feedback(self) -> None:
        """_filter_replay_loops detects source feedback."""
        event = make_replay_event(source_adapter="a")
        routes = [
            (
                Route(
                    id="r1",
                    source=RouteSource(adapter="a", event_kinds=(), channel=None),
                    targets=[RouteTarget(adapter="a")],
                ),
                [RouteTarget(adapter="a")],
            ),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert len(warnings) == 1
        assert len(filtered) == 0
        assert "a" in warnings[0]

    def test_filter_replay_loops_unit_prior_routing(self) -> None:
        """_filter_replay_loops detects prior routing through same route."""
        event = make_event_with_routing(
            source_adapter="a",
            matched_routes=("r1",),
        )
        routes = [
            (
                Route(
                    id="r1",
                    source=RouteSource(adapter="a", event_kinds=(), channel=None),
                    targets=[RouteTarget(adapter="b")],
                ),
                [RouteTarget(adapter="b")],
            ),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert len(warnings) == 1
        assert len(filtered) == 0
        assert "r1" in warnings[0]
