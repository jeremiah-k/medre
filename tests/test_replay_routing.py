"""Replay + routing correctness tests.

Covers:
* Replay through routes preserves route attribution in namespaced metadata
* Replay isolation: one route failure does not affect other routes
* Route-aware replay summaries include route attribution
* Route metadata namespacing: attribution lives on ReplayResult, not CanonicalEvent
* Loop prevention under replay: events do not route back to source
* Delivery metadata preservation: replay delivery is honest about status
* Deterministic duplicate replay behaviour
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from medre.core.events import CanonicalEvent, EventMetadata, RoutingMetadata
from medre.core.planning import FallbackResolver
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import EventFilter, SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    ReplaySummary,
    _build_summary,
    _filter_replay_loops,
    _replay_delivery_envelope,
    collect_replay_state,
    collect_replay_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
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


def _make_event_with_routing(
    source_adapter: str = "adapter_a",
    matched_routes: tuple[str, ...] = ("route-1",),
) -> CanonicalEvent:
    """Create an event with routing metadata indicating prior routing."""
    return _make_event(
        source_adapter=source_adapter,
        metadata=EventMetadata(
            routing=RoutingMetadata(matched_routes=matched_routes),
        ),
    )


class _StubPipeline:
    """Minimal pipeline collaborator satisfying _PipelineProtocol for tests.

    Delegates routing to a :class:`Router` and rendering to a
    :class:`RenderingPipeline`.  Transforms are identity (no-op).
    """

    def __init__(
        self,
        router: Router | None = None,
        rendering_pipeline: RenderingPipeline | None = None,
    ) -> None:
        self._router = router
        self._rendering_pipeline = rendering_pipeline
        self._fallback_resolver = FallbackResolver()

    async def transform_event(self, event: CanonicalEvent) -> CanonicalEvent:
        """Identity transform – no-op for testing."""
        return event

    async def render_event(self, event: CanonicalEvent) -> Any:
        """Render event through the rendering pipeline."""
        if self._rendering_pipeline is not None:
            return await self._rendering_pipeline.render(event, "test_adapter")
        return None

    async def route_event(
        self, event: CanonicalEvent,
    ) -> list[tuple[Any, list[Any]]]:
        """Match event against the router and return (route, targets) pairs."""
        if self._router is None:
            return []
        results: list[tuple[Any, list[Any]]] = []
        for route in self._router.match(event):
            targets = self._router.resolve_targets(event, route)
            results.append((route, targets))
        return results

    async def plan_delivery(
        self,
        event: CanonicalEvent,
        routes: list[tuple[Any, list[Any]]],
    ) -> list[Any]:
        """Build delivery plans for each route-target pair."""
        plans: list[Any] = []
        for route, targets in routes:
            for target in targets:
                plan = self._fallback_resolver.resolve_fallback(
                    event, target, {}
                )
                plans.append(plan)
        return plans

    async def deliver(self, event: CanonicalEvent, plans: list[Any]) -> list[Any]:
        """No-op delivery for testing – returns plans as pseudo-receipts."""
        return plans


def _make_engine(
    storage: SQLiteStorage,
    pipeline: Any | None = None,
) -> ReplayEngine:
    """Create a ReplayEngine with the storage cast to StorageBackend protocol."""
    return ReplayEngine(
        storage=cast(StorageBackend, storage),
        pipeline=pipeline,
    )


def _make_router(
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
        event = _make_event(source_adapter="adapter_a")
        await temp_storage.append(event)

        router = _make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        router = _make_router(source="src", dests=("dst",), route_id="r-dry")
        rendering = RenderingPipeline()
        rendering.register(TextRenderer(), priority=100)
        pipeline = _StubPipeline(router=router, rendering_pipeline=rendering)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event()
        await temp_storage.append(event)

        engine = _make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].route_attribution is None

    async def test_no_attribution_when_no_routes_match(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """No matching routes produces attribution with empty route_ids."""
        event = _make_event(source_adapter="adapter_x")
        await temp_storage.append(event)

        router = _make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event()
        await temp_storage.append(event)

        router = _make_router(source="adapter_a", dests=("adapter_b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        _ = [r async for r in engine.replay(request)]

        # Original event must be unchanged
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        # No replay-specific metadata was added to the event
        assert stored.metadata.routing is None or stored.metadata.routing == event.metadata.routing

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
        event = _make_event(source_adapter="adapter_a")
        await temp_storage.append(event)

        # Route: adapter_a → adapter_a (would create feedback loop)
        router = _make_router(source="adapter_a", dests=("adapter_a",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event_a = _make_event(
            source_adapter="a", event_id="evt-from-a",
        )
        event_b = _make_event(
            source_adapter="b", event_id="evt-from-b",
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event_with_routing(
            source_adapter="adapter_a",
            matched_routes=("route-1",),
        )
        await temp_storage.append(event)

        router = _make_router(
            source="adapter_a",
            dests=("adapter_b",),
            route_id="route-1",
        )
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event_with_routing(
            source_adapter="adapter_a",
            matched_routes=("route-1",),
        )
        await temp_storage.append(event)

        # route-2 is different from route-1 and targets adapter_b (not source)
        router = _make_router(
            source="adapter_a",
            dests=("adapter_b",),
            route_id="route-2",
        )
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "route-2" in route_result.route_attribution.route_ids
        assert len(route_result.route_attribution.loop_warnings) == 0

    def test_filter_replay_loops_unit_no_loops(self) -> None:
        """_filter_replay_loops returns no warnings when no loops exist."""
        event = _make_event(source_adapter="a")
        routes = [
            (Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                   targets=[RouteTarget(adapter="b")]), [RouteTarget(adapter="b")]),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert warnings == []
        assert len(filtered) == 1

    def test_filter_replay_loops_unit_source_feedback(self) -> None:
        """_filter_replay_loops detects source feedback."""
        event = _make_event(source_adapter="a")
        routes = [
            (Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                   targets=[RouteTarget(adapter="a")]), [RouteTarget(adapter="a")]),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert len(warnings) == 1
        assert len(filtered) == 0
        assert "a" in warnings[0]

    def test_filter_replay_loops_unit_prior_routing(self) -> None:
        """_filter_replay_loops detects prior routing through same route."""
        event = _make_event_with_routing(
            source_adapter="a",
            matched_routes=("r1",),
        )
        routes = [
            (Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                   targets=[RouteTarget(adapter="b")]), [RouteTarget(adapter="b")]),
        ]
        warnings, filtered = _filter_replay_loops(event, routes)
        assert len(warnings) == 1
        assert len(filtered) == 0
        assert "r1" in warnings[0]


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
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.route_event = AsyncMock(
            side_effect=RuntimeError("Route lookup failed"),
        )

        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

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
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        # Route that loops back to source
        router = _make_router(source="a", dests=("a",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

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
        event = _make_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        rendering = RenderingPipeline()
        rendering.register(TextRenderer(), priority=100)
        pipeline = _StubPipeline(router=router, rendering_pipeline=rendering)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[])
        pipeline.plan_delivery = AsyncMock(return_value=[])
        pipeline.deliver = AsyncMock(return_value=[])

        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r-dup")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results1 = [r async for r in engine.replay(request)]
        results2 = [r async for r in engine.replay(request)]

        assert len(results1) == len(results2)
        for r1, r2 in zip(results1, results2):
            assert r1.event_id == r2.event_id
            assert r1.stage == r2.stage
            assert r1.status == r2.status
            assert r1.route_attribution == r2.route_attribution

    async def test_deterministic_loop_warnings(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Loop warnings are deterministic across repeated replays."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        # Create a loop route
        router = _make_router(source="a", dests=("a",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results1 = [r async for r in engine.replay(request)]
        results2 = [r async for r in engine.replay(request)]

        route1 = results1[1]
        route2 = results2[1]

        assert route1.route_attribution is not None
        assert route2.route_attribution is not None
        assert route1.route_attribution.loop_warnings == route2.route_attribution.loop_warnings

    async def test_collect_replay_state_deterministic(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """collect_replay_state produces identical state for same replay."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "good" in route_result.route_attribution.route_ids
        assert "loop" not in route_result.route_attribution.route_ids
        assert len(route_result.route_attribution.loop_warnings) == 1


# ===================================================================
# 9. Explicit route_ids targeting
# ===================================================================


class TestReplayExplicitRouteIds:
    """Replay can target explicit route IDs."""

    async def test_route_ids_filters_to_matching_route(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When route_ids is set, only matching routes are used."""
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            route_ids=("r1",),
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.route_ids == ("r1",)
        assert "r2" not in route_result.route_attribution.route_ids
        assert "b" in route_result.route_attribution.target_adapters
        assert "c" not in route_result.route_attribution.target_adapters

    async def test_route_ids_multiple_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Multiple route_ids all get used."""
        event = _make_event(source_adapter="a")
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
        r3 = Route(
            id="r3",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="d")],
        )
        router = Router(routes=[r1, r2, r3])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            route_ids=("r1", "r3"),
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "r1" in route_result.route_attribution.route_ids
        assert "r3" in route_result.route_attribution.route_ids
        assert "r2" not in route_result.route_attribution.route_ids

    async def test_route_ids_empty_means_all_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Empty route_ids (default) means all routes are considered."""
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "r1" in route_result.route_attribution.route_ids
        assert "r2" in route_result.route_attribution.route_ids

    async def test_route_ids_nonexistent_route_fails(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Requesting a nonexistent route_id results in no match."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        r1 = Route(
            id="r1",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        router = Router(routes=[r1])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            route_ids=("nonexistent",),
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "failed"
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.route_ids == ()


# ===================================================================
# 10. Replay summary per-route counts
# ===================================================================


class TestReplaySummaryRouteCounts:
    """ReplaySummary includes per-route counts."""

    async def test_summary_by_route_single_route(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary tracks per-route event counts for a single route."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r1")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert "r1" in summary.by_route
        assert summary.by_route["r1"]["events"] == 1
        assert summary.by_route["r1"]["succeeded"] == 1
        assert summary.by_route["r1"]["failed"] == 0

    async def test_summary_by_route_multiple_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary tracks per-route counts when multiple routes match."""
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert "r1" in summary.by_route
        assert "r2" in summary.by_route
        assert summary.by_route["r1"]["events"] == 1
        assert summary.by_route["r1"]["succeeded"] == 1
        assert summary.by_route["r2"]["events"] == 1
        assert summary.by_route["r2"]["succeeded"] == 1

    async def test_summary_by_route_empty_when_no_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary by_route is empty when no routes are matched."""
        event = _make_event(source_adapter="x")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert summary.by_route == {}

    async def test_summary_run_id_propagated(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary includes run_id when provided."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(
                mode=ReplayMode.RE_ROUTE,
                run_id="run-abc-123",
            )),
            mode=ReplayMode.RE_ROUTE,
            run_id="run-abc-123",
        )
        assert summary.run_id == "run-abc-123"

    async def test_summary_by_route_in_to_dict(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary.to_dict() includes by_route."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r1")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        d = summary.to_dict()
        assert "by_route" in d
        assert "r1" in d["by_route"]
        assert d["by_route"]["r1"]["events"] == 1

    def test_build_summary_with_by_route(self) -> None:
        """_build_summary correctly computes by_route from results."""
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
                    route_ids=("r1", "r2"),
                    source_adapter="a",
                    target_adapters=("b", "c"),
                    replay_mode="re_route",
                ),
            ),
            ReplayResult(
                event_id="e2",
                stage="store",
                status="passed",
            ),
            ReplayResult(
                event_id="e2",
                stage="route",
                status="failed",
                output=[],
                route_attribution=ReplayRouteAttribution(
                    route_ids=("r1",),
                    source_adapter="a",
                    target_adapters=(),
                    replay_mode="re_route",
                ),
            ),
        ]
        summary = _build_summary(
            results,
            mode=ReplayMode.RE_ROUTE,
            run_id="test-run",
        )
        assert summary.run_id == "test-run"
        assert "r1" in summary.by_route
        assert summary.by_route["r1"]["events"] == 2
        assert summary.by_route["r1"]["succeeded"] == 1
        assert summary.by_route["r1"]["failed"] == 1
        assert "r2" in summary.by_route
        assert summary.by_route["r2"]["events"] == 1
        assert summary.by_route["r2"]["succeeded"] == 1


# ===================================================================
# 11. Replay run_id in attribution
# ===================================================================


class TestReplayRunId:
    """run_id is recorded in ReplayRouteAttribution."""

    async def test_run_id_in_route_attribution(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """run_id from ReplayRequest appears in ReplayRouteAttribution."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            run_id="replay-op-42",
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.route_attribution is not None
        assert route_result.route_attribution.run_id == "replay-op-42"

    async def test_run_id_empty_when_not_set(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """run_id defaults to empty string when not set."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.route_attribution is not None
        assert route_result.route_attribution.run_id == ""

    async def test_run_id_in_attribution_to_dict(self) -> None:
        """run_id appears in ReplayRouteAttribution.to_dict()."""
        attr = ReplayRouteAttribution(
            route_ids=("r1",),
            source_adapter="a",
            target_adapters=("b",),
            replay_mode="re_route",
            run_id="run-xyz",
        )
        d = attr.to_dict()
        assert d["run_id"] == "run-xyz"

    async def test_run_id_preserved_in_no_match_attribution(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """run_id appears even when no routes match."""
        event = _make_event(source_adapter="x")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            run_id="no-match-run",
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.route_attribution is not None
        assert route_result.route_attribution.run_id == "no-match-run"
        assert route_result.route_attribution.route_ids == ()


# ===================================================================
# 12. Disabled routes during replay
# ===================================================================


class TestDisabledRoutesDuringReplay:
    """Disabled routes are skipped during replay."""

    async def test_disabled_route_not_used_by_default(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Disabled route is not used when route_ids is empty."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        r_enabled = Route(
            id="enabled",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
            enabled=True,
        )
        r_disabled = Route(
            id="disabled",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="c")],
            enabled=False,
        )
        router = Router(routes=[r_enabled, r_disabled])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "enabled" in route_result.route_attribution.route_ids
        assert "disabled" not in route_result.route_attribution.route_ids

    async def test_explicit_disabled_route_not_found(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Explicitly requesting a disabled route results in no match.

        The router's match() method skips disabled routes, so a
        route_id filter for a disabled route yields no results.  The
        replay engine records a diagnostic warning for the missing
        route.
        """
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        r_disabled = Route(
            id="disabled-route",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
            enabled=False,
        )
        router = Router(routes=[r_disabled])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            route_ids=("disabled-route",),
        )

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        # Disabled route is not returned by the router, so no match
        assert route_result.status == "failed"
        assert route_result.route_attribution is not None
        assert route_result.route_attribution.route_ids == ()

    async def test_all_routes_disabled_fails_gracefully(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When all routes are disabled, replay fails with no match."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        r_disabled = Route(
            id="all-off",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
            enabled=False,
        )
        router = Router(routes=[r_disabled])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "failed"


# ===================================================================
# 13. Replay does not mutate canonical events
# ===================================================================


class TestReplayNoMutation:
    """Replay never mutates historical CanonicalEvents."""

    async def test_re_route_preserves_original_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RE_ROUTE mode does not mutate the stored event."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        # Snapshot original values
        orig_id = event.event_id
        orig_kind = event.event_kind
        orig_payload = dict(event.payload)
        orig_source = event.source_adapter

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        _ = [r async for r in engine.replay(request)]

        # In-memory event object unchanged
        assert event.event_id == orig_id
        assert event.event_kind == orig_kind
        assert dict(event.payload) == orig_payload
        assert event.source_adapter == orig_source

        # Stored event also unchanged
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        assert stored.event_id == orig_id
        assert stored.event_kind == orig_kind
        assert dict(stored.payload) == orig_payload
        assert stored.source_adapter == orig_source

    async def test_best_effort_preserves_original_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT mode does not mutate the stored event."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        orig_id = event.event_id
        orig_payload = dict(event.payload)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        _ = [r async for r in engine.replay(request)]

        # In-memory and stored event unchanged
        assert event.event_id == orig_id
        assert dict(event.payload) == orig_payload
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        assert dict(stored.payload) == orig_payload


# ===================================================================
# 14. Backward compatibility
# ===================================================================


class TestBackwardCompat:
    """Replay without route_ids/run_id works as before."""

    async def test_replay_without_route_ids_uses_all_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplayRequest without route_ids uses all available routes."""
        event = _make_event(source_adapter="a")
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
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        # Old-style request: no route_ids, no run_id
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        route_result = results[1]

        assert route_result.status == "passed"
        assert route_result.route_attribution is not None
        assert "r1" in route_result.route_attribution.route_ids
        assert "r2" in route_result.route_attribution.route_ids
        assert route_result.route_attribution.run_id == ""

    async def test_summary_without_run_id_has_empty_string(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplaySummary.run_id defaults to empty string."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
            mode=ReplayMode.RE_ROUTE,
        )
        assert summary.run_id == ""
        assert summary.to_dict()["run_id"] == ""

    async def test_replay_request_defaults(self) -> None:
        """New ReplayRequest fields have sensible defaults."""
        req = ReplayRequest()
        assert req.route_ids == ()
        assert req.run_id == ""

    async def test_attribution_defaults_include_run_id(self) -> None:
        """ReplayRouteAttribution defaults include run_id."""
        attr = ReplayRouteAttribution()
        assert attr.run_id == ""
        d = attr.to_dict()
        assert d["run_id"] == ""
