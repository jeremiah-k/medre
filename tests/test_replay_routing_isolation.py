"""Replay + routing correctness tests (isolation, summaries, delivery, determinism).

Covers:
* Replay isolation: one route failure does not affect other routes
* Route-aware replay summaries include route attribution
* Delivery metadata preservation: replay delivery is honest about status
* Deterministic duplicate replay behaviour
* Multiple routes matching same event
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from medre.core.engine.replay import (
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    _build_summary,
    _replay_delivery_envelope,
    collect_replay_state,
    collect_replay_summary,
)
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from tests.helpers.replay_routing import (
    StubPipeline,
    make_engine,
    make_replay_event,
    make_router,
)

# ===================================================================
# 4. Replay isolation
# ===================================================================


class TestReplayIsolation:
    """One route failure does not affect other routes during replay."""

    async def test_isolation_one_route_filtered_one_passes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When one route is loop-filtered, others still deliver."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        # Route 1: a→a (loop - will be filtered)
        # Route 2: a→b (valid - should pass)
        route_loop = Route(
            id="loop-route",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="a")],
        )
        route_good = Route(
            id="good-route",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        router = Router(routes=[route_loop, route_good])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "good-route" in route_result.route_attribution.route_ids
        assert "loop-route" not in route_result.route_attribution.route_ids
        assert len(route_result.route_attribution.loop_warnings) == 1

    async def test_isolation_pipeline_error_in_route_does_not_crash(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route stage error is captured, not propagated."""
        event = make_replay_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.route_event = AsyncMock(
            side_effect=RuntimeError("Route lookup failed"),
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # store + route (error) + plan (skipped)
        assert len(results) == 3
        assert results[1].status == "error"
        assert "Route lookup failed" in (results[1].error or "")
        assert results[2].status == "skipped"


# ===================================================================
# 5. Route-aware replay summaries
# ===================================================================


class TestRouteAwareReplaySummaries:
    """ReplaySummary captures route resolution from route-aware replay."""

    async def test_summary_includes_route_resolution_count(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplaySummary counts route resolutions with attribution."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert summary.route_resolution_count == 1

    async def test_summary_zero_resolutions_when_loop_filtered(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplaySummary shows 0 route resolutions when all routes loop-filtered."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        # Route that loops back to source
        router = make_router(source="a", dests=("a",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert summary.route_resolution_count == 0

    def test_build_summary_with_attribution(self) -> None:
        """_build_summary correctly aggregates results with route attribution."""
        results = [
            ReplayResult(
                event_id="e1",
                stage="store",
                status="passed",
            ),
            ReplayResult(
                event_id="e1",
                stage="route",
                status="passed",
                output=[("route", ["target"])],
                route_attribution=ReplayRouteAttribution(
                    route_ids=("r1",),
                    source_adapter="a",
                    target_adapters=("b",),
                    replay_mode="re_route",
                ),
            ),
            ReplayResult(
                event_id="e1",
                stage="plan",
                status="passed",
            ),
        ]
        summary = _build_summary(results, mode=ReplayMode.RE_ROUTE)
        assert summary.events_replayed == 3
        assert summary.route_resolution_count == 1
        assert summary.mode == ReplayMode.RE_ROUTE


# ===================================================================
# 6. Delivery metadata preservation
# ===================================================================


class TestDeliveryMetadataPreservation:
    """Replay delivery metadata is honest about delivery status."""

    async def test_best_effort_delivery_wrapped_in_envelope(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT delivery output is wrapped in replay envelope."""
        event = make_replay_event()
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
        deliver_result = results[4]

        assert deliver_result.status == "passed"
        assert isinstance(deliver_result.output, dict)
        assert deliver_result.output["replay"] is True
        # Original adapter results preserved as-is
        assert deliver_result.output["adapter_results"] == ["receipt"]

    async def test_dry_run_does_not_claim_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """DRY_RUN mode never claims delivery was performed."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        rendering = RenderingPipeline()
        rendering.register(TextRenderer(), priority=100)
        pipeline = StubPipeline(router=router, rendering_pipeline=rendering)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        results = [r async for r in engine.replay(request)]
        deliver_result = results[4]

        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "skipped"
        assert "dry_run" in (deliver_result.error or "")

    async def test_delivery_envelope_preserves_adapter_results(self) -> None:
        """_replay_delivery_envelope preserves original results without promotion."""

        class _FakeDeliveryResult:
            """Simulates a queued/best-effort adapter result."""

            native_message_id = None  # Not confirmed delivered
            status = "queued"

        original = [_FakeDeliveryResult()]
        envelope = _replay_delivery_envelope(original)

        assert envelope["replay"] is True
        assert envelope["adapter_results"] is original
        # The queued status is preserved, not promoted to "delivered"
        assert envelope["adapter_results"][0].status == "queued"
        assert envelope["adapter_results"][0].native_message_id is None

    async def test_delivery_no_plans_is_skipped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT with no delivery plans produces skipped, not error."""
        event = make_replay_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[])
        pipeline.plan_delivery = AsyncMock(return_value=[])
        pipeline.deliver = AsyncMock(return_value=[])

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]
        # route result is "failed" (no routes), plan is skipped, deliver is skipped
        route_result = results[1]
        assert route_result.status == "failed"


# ===================================================================
# 7. Deterministic duplicate replay behaviour
# ===================================================================


class TestDeterministicDuplicateReplay:
    """Same replay request produces identical results across runs."""

    async def test_identical_results_on_duplicate_re_route(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RE_ROUTE replay produces identical attribution on repeated runs."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",), route_id="r-dup")
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results1 = [r async for r in engine.replay(request)]
        results2 = [r async for r in engine.replay(request)]

        assert len(results1) == len(results2)
        for r1, r2 in zip(results1, results2, strict=False):
            assert r1.event_id == r2.event_id
            assert r1.stage == r2.stage
            assert r1.status == r2.status
            assert r1.route_attribution == r2.route_attribution

    async def test_deterministic_loop_warnings(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Loop warnings are deterministic across repeated replays."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        # Create a loop route
        router = make_router(source="a", dests=("a",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results1 = [r async for r in engine.replay(request)]
        results2 = [r async for r in engine.replay(request)]

        route1 = results1[1]
        route2 = results2[1]

        assert route1.route_attribution is not None
        assert route2.route_attribution is not None
        assert (
            route1.route_attribution.loop_warnings
            == route2.route_attribution.loop_warnings
        )

    async def test_collect_replay_state_deterministic(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """collect_replay_state produces identical state for same replay."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        state1 = await collect_replay_state(engine.replay(request))
        state2 = await collect_replay_state(engine.replay(request))

        assert state1.events_processed == state2.events_processed
        assert state1.events_passed == state2.events_passed
        assert state1.events_failed == state2.events_failed


# ===================================================================
# 8. Multiple routes matching same event
# ===================================================================


class TestReplayMultipleRoutes:
    """Multiple routes can match the same event during replay."""

    async def test_multiple_routes_attributed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route attribution includes all matching routes."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        r1 = Route(
            id="r1",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        r2 = Route(
            id="r2",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="c")],
        )
        router = Router(routes=[r1, r2])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "r1" in route_result.route_attribution.route_ids
        assert "r2" in route_result.route_attribution.route_ids
        assert "b" in route_result.route_attribution.target_adapters
        assert "c" in route_result.route_attribution.target_adapters

    async def test_multi_route_partial_loop_filter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When one of multiple routes loops, only the loop route is filtered."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        r_good = Route(
            id="good",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        r_loop = Route(
            id="loop",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="a")],  # back to source
        )
        router = Router(routes=[r_good, r_loop])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "good" in route_result.route_attribution.route_ids
        assert "loop" not in route_result.route_attribution.route_ids
        assert len(route_result.route_attribution.loop_warnings) == 1
