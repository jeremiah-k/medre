"""Replay + routing correctness tests (controls, counts, compatibility).

Covers:
* Explicit route_ids targeting in replay requests
* Replay summary per-route counts
* Replay run_id in attribution
* Disabled routes during replay
* Replay does not mutate canonical events
* Direct constructor scalar defaults
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.replay import (
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    _build_summary,
    collect_replay_summary,
)
from tests.helpers.replay_routing import (
    StubPipeline,
    make_engine,
    make_replay_event,
    make_router,
)

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
        r3 = Route(
            id="r3",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="d")],
        )
        router = Router(routes=[r1, r2, r3])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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

    async def test_route_ids_nonexistent_route_fails(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Requesting a nonexistent route_id results in no match."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        r1 = Route(
            id="r1",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
        )
        router = Router(routes=[r1])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",), route_id="r1")
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

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
        event = make_replay_event(source_adapter="x")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

        summary = await collect_replay_summary(
            engine.replay(
                ReplayRequest(
                    mode=ReplayMode.RE_ROUTE,
                    run_id="run-abc-123",
                )
            ),
            mode=ReplayMode.RE_ROUTE,
            run_id="run-abc-123",
        )
        assert summary.run_id == "run-abc-123"

    async def test_summary_by_route_in_to_dict(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Summary.to_dict() includes by_route."""
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",), route_id="r1")
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="x")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
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
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        r_disabled = Route(
            id="disabled-route",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
            enabled=False,
        )
        router = Router(routes=[r_disabled])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        r_disabled = Route(
            id="all-off",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],
            enabled=False,
        )
        router = Router(routes=[r_disabled])
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        # Snapshot original values
        orig_id = event.event_id
        orig_kind = event.event_kind
        orig_payload = dict(event.payload)
        orig_source = event.source_adapter

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
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
        event = make_replay_event(source_adapter="src")
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

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        _ = [r async for r in engine.replay(request)]

        # In-memory and stored event unchanged
        assert event.event_id == orig_id
        assert dict(event.payload) == orig_payload
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        assert dict(stored.payload) == orig_payload


# ===================================================================
# 14. Direct constructor scalar defaults
# ===================================================================


class TestBackwardCompat:
    """Replay without route_ids/run_id works as before."""

    async def test_replay_without_route_ids_uses_all_routes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplayRequest without route_ids uses all available routes."""
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
        event = make_replay_event(source_adapter="a")
        await temp_storage.append(event)

        router = make_router(source="a", dests=("b",))
        pipeline = StubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)

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
