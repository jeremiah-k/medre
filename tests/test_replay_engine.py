"""ReplayEngine stage execution, mode behavior, request/result construction.

Tests the five replay modes (STRICT, RE_RENDER, RE_ROUTE, BEST_EFFORT,
DRY_RUN), stage execution, count_matching, empty-result handling,
deterministic ordering, schema-version compatibility, and diagnostician
wiring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from medre.core.engine.replay import (
    ReplayMode,
    ReplayRequest,
    collect_replay_state,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import EventFilter, SQLiteStorage
from tests.helpers.replay import (
    StubPipeline,
    make_engine,
    make_second_event,
)


class TestReplayEngine:
    """Test replay engine with storage and pipeline."""

    async def test_strict_mode_verifies_events(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT mode reads events and validates they exist."""
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]

        assert len(results) == 1
        result = results[0]
        assert result.event_id == sample_event.event_id
        assert result.stage == "store"
        assert result.status == "passed"

    async def test_render_mode_captures_output(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """RE_RENDER mode renders events without delivering."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]

        # RE_RENDER runs store + render stages
        assert len(results) == 2

        # Stage 1: store verification
        assert results[0].stage == "store"
        assert results[0].status == "passed"

        # Stage 2: render
        assert results[1].stage == "render"
        assert results[1].status == "passed"
        assert results[1].output is not None
        # TextRenderer extracts payload["text"] for message.created
        assert "hello world" in results[1].output.payload.get("text", "")

        # Verify no storage mutation: event count unchanged
        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    async def test_re_route_mode_plans_with_current_routes(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_ROUTE mode plans delivery but doesn't render or deliver."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(router=router_with_routes)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_ROUTE,
        )

        results = [r async for r in engine.replay(request)]

        # RE_ROUTE runs store + route + plan stages
        assert len(results) == 3

        assert results[0].stage == "store"
        assert results[0].status == "passed"

        assert results[1].stage == "route"
        assert results[1].status == "passed"
        # Output contains matched route-target pairs
        assert results[1].output is not None
        assert len(results[1].output) == 1  # one route matched

        assert results[2].stage == "plan"
        assert results[2].status == "passed"
        # Output contains delivery plans
        assert results[2].output is not None
        assert len(results[2].output) == 1  # one target -> one plan

        # Verify no storage mutation
        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    async def test_empty_request_returns_no_results(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """No matching events returns empty iterator."""
        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert results == []

    async def test_count_matching(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching returns correct count without replaying."""
        await temp_storage.append(sample_event)

        second = make_second_event(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        count = await engine.count_matching(request)
        assert count == 2

        # Also verify a filtered count
        filtered_request = ReplayRequest(
            event_kinds=["message.created"],
            source_adapters=["nonexistent"],
            mode=ReplayMode.STRICT,
        )
        filtered_count = await engine.count_matching(filtered_request)
        assert filtered_count == 0

    async def test_strict_mode_detects_unregistered_kind(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """STRICT mode reports failure for events with unregistered kinds."""
        event = CanonicalEvent(
            event_id="bad-kind-001",
            event_kind="unknown.event_type",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["unknown.event_type"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "Unregistered" in (results[0].error or "")

    async def test_count_matching_with_correlation_ids(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching with correlation_ids fetches by individual ID."""
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            correlation_ids=[sample_event.event_id, "nonexistent-id"],
            mode=ReplayMode.STRICT,
        )

        count = await engine.count_matching(request)
        assert count == 1  # Only sample_event exists

    # ------------------------------------------------------------------
    # New tests: formalized replay semantics
    # ------------------------------------------------------------------

    async def test_strict_mode_preserves_lineage(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT mode preserves event lineage in all results."""
        event = CanonicalEvent(
            event_id="lin-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id="parent-evt",
            lineage=("ancestor-1", "ancestor-2"),
            relations=(),
            payload={"text": "derived"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].lineage == ["ancestor-1", "ancestor-2"]

        # Verify ReplayState collects lineage correctly
        state = await collect_replay_state(engine.replay(request))
        assert state.events_processed == 1
        assert state.events_passed == 1
        assert state.current_lineage == ["ancestor-1", "ancestor-2"]

    async def test_re_render_mode_uses_current_renderers(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """RE_RENDER mode invokes the current rendering pipeline."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(rendering_pipeline=rendering_pipeline)

        # Wrap render_event to track calls
        render_calls: list[CanonicalEvent] = []
        original_render = pipeline.render_event

        async def tracking_render(event: CanonicalEvent) -> Any:
            render_calls.append(event)
            return await original_render(event)

        pipeline.render_event = tracking_render

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]

        assert len(render_calls) == 1
        assert render_calls[0].event_id == sample_event.event_id

        assert results[1].stage == "render"
        assert results[1].status == "passed"
        assert results[1].output is not None

    async def test_re_route_mode_uses_current_routes(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_ROUTE mode invokes the router with current route rules."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(router=router_with_routes)

        # Wrap route_event to track calls
        route_calls: list[CanonicalEvent] = []
        original_route = pipeline.route_event

        async def tracking_route(
            event: CanonicalEvent,
        ) -> list[tuple[Any, list[Any]]]:
            route_calls.append(event)
            return await original_route(event)

        pipeline.route_event = tracking_route

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_ROUTE,
        )

        results = [r async for r in engine.replay(request)]

        assert len(route_calls) == 1
        assert route_calls[0].event_id == sample_event.event_id

        # store + route + plan
        assert len(results) == 3
        assert results[1].stage == "route"
        assert results[1].status == "passed"

    async def test_strict_does_not_mutate_events(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT mode never mutates stored canonical events."""
        await temp_storage.append(sample_event)

        # Snapshot original values
        orig_id = sample_event.event_id
        orig_kind = sample_event.event_kind
        orig_payload = dict(sample_event.payload)
        orig_lineage = list(sample_event.lineage)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        _ = [r async for r in engine.replay(request)]

        # Original event object unchanged
        assert sample_event.event_id == orig_id
        assert sample_event.event_kind == orig_kind
        assert dict(sample_event.payload) == orig_payload
        assert list(sample_event.lineage) == orig_lineage

        # Stored event also unchanged
        stored = await temp_storage.get(sample_event.event_id)
        assert stored is not None
        assert stored.event_id == orig_id
        assert stored.event_kind == orig_kind
        assert dict(stored.payload) == orig_payload
        assert list(stored.lineage) == orig_lineage

    async def test_re_render_does_not_call_routing(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_RENDER mode never invokes routing, planning, or delivery."""
        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]

        # Only store + render stages executed
        assert [r.stage for r in results] == ["store", "render"]

        # Routing, planning, and delivery never invoked
        pipeline.route_event.assert_not_called()
        pipeline.plan_delivery.assert_not_called()
        pipeline.deliver.assert_not_called()

    async def test_re_route_does_not_deliver(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_ROUTE mode plans delivery but never executes it."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(router=router_with_routes)

        deliver_called = False
        original_deliver = pipeline.deliver

        async def tracking_deliver(
            event: CanonicalEvent,
            plans: list[Any],
        ) -> list[Any]:
            nonlocal deliver_called
            deliver_called = True
            return await original_deliver(event, plans)

        pipeline.deliver = tracking_deliver

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_ROUTE,
        )

        results = [r async for r in engine.replay(request)]

        # Only store + route + plan stages
        assert [r.stage for r in results] == ["store", "route", "plan"]
        assert not deliver_called

    async def test_replay_of_derived_events(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Derived events with lineage replay correctly."""
        derived = CanonicalEvent(
            event_id="derived-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=sample_event.event_id,
            lineage=(sample_event.event_id,),
            relations=(),
            payload={"text": "derived from parent"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(sample_event)
        await temp_storage.append(derived)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            correlation_ids=["derived-001"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].event_id == "derived-001"
        assert results[0].status == "passed"
        assert results[0].lineage == [sample_event.event_id]

    async def test_replay_of_relation_events(
        self,
        temp_storage: SQLiteStorage,
        sample_event_with_relations: CanonicalEvent,
    ) -> None:
        """Events carrying relations replay without errors."""
        await temp_storage.append(sample_event_with_relations)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].event_id == sample_event_with_relations.event_id
        assert results[0].status == "passed"

        # Verify the stored event still has its relations intact
        stored = await temp_storage.get(sample_event_with_relations.event_id)
        assert stored is not None
        assert len(stored.relations) == 1
        assert stored.relations[0].relation_type == "reply"

    async def test_count_matching_with_filters(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching applies time, kind, and adapter filters."""
        evt_old = CanonicalEvent(
            event_id="old-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=sample_event.timestamp - timedelta(hours=2),
            source_adapter="adapter_alpha",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "old event"},
            metadata=EventMetadata(),
        )
        evt_presence = CanonicalEvent(
            event_id="presence-001",
            event_kind="presence.changed",
            schema_version=1,
            timestamp=sample_event.timestamp + timedelta(hours=1),
            source_adapter="adapter_beta",
            source_transport_id="node-456",
            source_channel_id="ch-1",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"status": "online"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(evt_old)
        await temp_storage.append(evt_presence)
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)

        # All events
        count_all = await engine.count_matching(
            ReplayRequest(mode=ReplayMode.STRICT),
        )
        assert count_all == 3

        # Filter by event kind
        count_kind = await engine.count_matching(
            ReplayRequest(
                event_kinds=["message.created"],
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_kind == 2  # evt_old + sample_event

        # Filter by source adapter
        count_adapter = await engine.count_matching(
            ReplayRequest(
                source_adapters=["adapter_beta"],
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_adapter == 1

        # Filter by time window around sample_event
        count_time = await engine.count_matching(
            ReplayRequest(
                time_start=sample_event.timestamp - timedelta(minutes=1),
                time_end=sample_event.timestamp + timedelta(minutes=1),
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_time == 1  # Only sample_event in the window

    # ------------------------------------------------------------------
    # Deterministic ordering
    # ------------------------------------------------------------------

    async def test_results_are_ordered_deterministically(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Multiple events produce results in consistent order."""
        second = make_second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2

        # Run again -- order must be identical
        results2 = [r async for r in engine.replay(request)]
        assert [r.event_id for r in results] == [r.event_id for r in results2]

    # ------------------------------------------------------------------
    # Relations across modes
    # ------------------------------------------------------------------

    async def test_relations_preserved_across_re_render(
        self,
        temp_storage: SQLiteStorage,
        sample_event_with_relations: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """RE_RENDER mode preserves relations on the stored event."""
        await temp_storage.append(sample_event_with_relations)

        pipeline = StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2

        # The stored event in store stage output should have relations
        stored = results[0].output
        assert stored is not None
        assert len(stored.relations) == 1
        assert stored.relations[0].relation_type == "reply"

        # Verify stored event is unchanged after replay
        stored_after = await temp_storage.get(sample_event_with_relations.event_id)
        assert stored_after is not None
        assert len(stored_after.relations) == 1
        assert stored_after.relations[0].relation_type == "reply"

    async def test_relations_preserved_across_re_route(
        self,
        temp_storage: SQLiteStorage,
        sample_event_with_relations: CanonicalEvent,
        router_with_routes: Router,
    ) -> None:
        """RE_ROUTE mode preserves relations on the stored event."""
        await temp_storage.append(sample_event_with_relations)

        pipeline = StubPipeline(router=router_with_routes)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_ROUTE,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 3

        # Verify stored event unchanged
        stored = await temp_storage.get(sample_event_with_relations.event_id)
        assert stored is not None
        assert len(stored.relations) == 1

    # ------------------------------------------------------------------
    # Renderer missing / downgrade diagnostics
    # ------------------------------------------------------------------

    async def test_render_failure_records_diagnostics(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Render failure emits diagnostic via Diagnostician."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(
            side_effect=RuntimeError("No renderer for adapter"),
        )

        diag = Diagnostician()
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2
        assert results[1].stage == "render"
        assert results[1].status == "error"
        assert "No renderer" in (results[1].error or "")

        # Diagnostician captured the renderer failure
        snap = diag.snapshot()
        assert len(snap["renderer_failures"]) > 0

    # ------------------------------------------------------------------
    # Schema-version compatibility
    # ------------------------------------------------------------------

    async def test_schema_version_compatibility(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Events with current schema_version pass STRICT replay."""
        from medre.core.events.schema import CURRENT_SCHEMA_VERSION

        event = CanonicalEvent(
            event_id="schema-v1",
            event_kind="message.created",
            schema_version=CURRENT_SCHEMA_VERSION,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "schema test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "passed"

    async def test_future_schema_version_accepted(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Events with schema_version > CURRENT pass STRICT replay.

        The schema system accepts future versions at storage time.
        Replay should not reject them either.
        """
        from medre.core.events.schema import CURRENT_SCHEMA_VERSION

        event = CanonicalEvent(
            event_id="schema-future",
            event_kind="message.created",
            schema_version=CURRENT_SCHEMA_VERSION + 1,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "future schema"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "passed"

    # ------------------------------------------------------------------
    # Diagnostician wiring
    # ------------------------------------------------------------------

    async def test_diagnostician_records_missing_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Replaying a missing event records a replay_skip diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        diag = Diagnostician()
        engine = make_engine(temp_storage)
        engine._diagnostician = diag

        request = ReplayRequest(
            correlation_ids=["nonexistent-001"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        snap = diag.snapshot()
        assert "Event not found in storage" in snap["replay_skips"]

    async def test_diagnostician_records_unregistered_kind(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Unregistered event_kind records a replay_downgrade diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        event = CanonicalEvent(
            event_id="bad-kind-002",
            event_kind="unknown.event_type",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        diag = Diagnostician()
        engine = make_engine(temp_storage)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["unknown.event_type"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        snap = diag.snapshot()
        assert len(snap["replay_downgrades"]) > 0

    async def test_diagnostician_records_no_routes_matched(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """No matching routes records a replay_skip diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        # Empty router -- no routes will match
        empty_router = Router(routes=[])
        pipeline = StubPipeline(router=empty_router)

        diag = Diagnostician()
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # store + route (failed)
        assert len(results) == 3
        assert results[1].status == "failed"

        snap = diag.snapshot()
        assert "No routes matched" in snap["replay_skips"]

    # ------------------------------------------------------------------
    # Stage resolution
    # ------------------------------------------------------------------

    def test_resolve_stages_all_modes(self) -> None:
        """_resolve_stages returns correct stages for each mode."""
        from medre.core.engine.replay import _resolve_stages

        strict = _resolve_stages(ReplayRequest(mode=ReplayMode.STRICT))
        assert strict == ("store",)

        re_render = _resolve_stages(ReplayRequest(mode=ReplayMode.RE_RENDER))
        assert re_render == ("store", "render")

        re_route = _resolve_stages(ReplayRequest(mode=ReplayMode.RE_ROUTE))
        assert re_route == ("store", "route", "plan")

        best = _resolve_stages(ReplayRequest(mode=ReplayMode.BEST_EFFORT))
        assert best == ("store", "route", "plan", "render", "deliver")

        dry = _resolve_stages(ReplayRequest(mode=ReplayMode.DRY_RUN))
        assert dry == ("store", "route", "plan", "render", "deliver")

    def test_resolve_stages_with_target_stages(self) -> None:
        """target_stages intersects with mode-allowed stages."""
        from medre.core.engine.replay import _resolve_stages

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            target_stages=["store", "render"],
        )
        stages = _resolve_stages(request)
        assert stages == ("store", "render")  # ordered by mode definition

    def test_resolve_stages_target_stages_subset(self) -> None:
        """target_stages only returns stages allowed by the mode."""
        from medre.core.engine.replay import _resolve_stages

        # STRICT only allows "store"; requesting "render" is a no-op
        request = ReplayRequest(
            mode=ReplayMode.STRICT,
            target_stages=["render"],
        )
        stages = _resolve_stages(request)
        assert stages == ()

    # ------------------------------------------------------------------
    # collect_replay_state aggregation
    # ------------------------------------------------------------------

    async def test_collect_replay_state_aggregates(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """collect_replay_state correctly aggregates multi-event results."""
        second = make_second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        state = await collect_replay_state(engine.replay(request))

        assert state.events_processed == 2
        assert state.events_passed == 2
        assert state.events_failed == 0
        assert state.events_skipped == 0


# ===================================================================
# _filter_plans_by_adapter
# ===================================================================


def _make_delivery_plan(
    adapter: str | None = "matrix-bridge",
) -> Any:
    """Build a minimal DeliveryPlan-like object for filter tests."""
    from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
    from medre.core.routing.models import RouteTarget

    target = RouteTarget(adapter=adapter, channel="ch-out")
    return DeliveryPlan(
        plan_id="plan-001",
        event_id="evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )


class TestFilterPlansByAdapter:
    """Tests for _filter_plans_by_adapter matching logic."""

    def test_matching_adapter_included(self) -> None:
        """Plan with matching target adapter is included."""
        from medre.core.engine.replay import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([plan], ["matrix-bridge"])
        assert len(result) == 1

    def test_non_matching_adapter_excluded(self) -> None:
        """Plan with non-matching adapter is excluded."""
        from medre.core.engine.replay import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([plan], ["other-adapter"])
        assert len(result) == 0

    def test_none_adapter_included_conservatively(self) -> None:
        """Plan with adapter=None is included (conservative)."""
        from medre.core.engine.replay import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter=None)
        result = _filter_plans_by_adapter([plan], ["matrix-bridge"])
        assert len(result) == 1

    def test_tuple_plan_matching_adapter(self) -> None:
        """Tuple (route, DeliveryPlan) with matching adapter is included."""
        from medre.core.engine.replay import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([("route-stub", plan)], ["matrix-bridge"])
        assert len(result) == 1

    def test_tuple_plan_non_matching_excluded(self) -> None:
        """Tuple (route, DeliveryPlan) with non-matching adapter excluded."""
        from medre.core.engine.replay import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([("route-stub", plan)], ["other-adapter"])
        assert len(result) == 0


# ===================================================================
# _filter_plans_by_capability
# ===================================================================


class TestFilterPlansByCapability:
    """Tests for _filter_plans_by_capability early-return paths."""

    def _make_event(self) -> CanonicalEvent:
        return CanonicalEvent(
            event_id="cap-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="t-0",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
        )

    def test_returns_plans_when_pipeline_is_none(self) -> None:
        """When adapters is None, all plans pass through."""
        from medre.core.engine.replay import _filter_plans_by_capability

        plans = [_make_delivery_plan()]
        result = _filter_plans_by_capability(self._make_event(), plans, adapters=None)
        assert result == plans

    def test_returns_plans_when_pipeline_lacks_method(self) -> None:
        """Empty adapters dict passes everything conservatively."""
        from medre.core.engine.replay import _filter_plans_by_capability

        plans = [_make_delivery_plan()]
        result = _filter_plans_by_capability(self._make_event(), plans, adapters={})
        assert result == plans

    def test_supported_event_kind_passes(self) -> None:
        """Plan with adapter that supports the event kind is included."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import _filter_plans_by_capability

        caps = AdapterCapabilities(text=True)

        class _CapAdapter:
            _capabilities = caps

        adapters = {"adapter-1": _CapAdapter()}
        plan = _make_delivery_plan(adapter="adapter-1")
        result = _filter_plans_by_capability(
            self._make_event(), [plan], adapters=adapters
        )
        assert len(result) == 1

    def test_unsupported_event_kind_filtered(self) -> None:
        """Plan with adapter that doesn't support event kind is excluded."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import _filter_plans_by_capability

        caps = AdapterCapabilities(text=False)

        class _CapAdapter:
            _capabilities = caps

        adapters = {"adapter-1": _CapAdapter()}
        plan = _make_delivery_plan(adapter="adapter-1")
        result = _filter_plans_by_capability(
            self._make_event(), [plan], adapters=adapters
        )
        assert len(result) == 0

    def test_missing_adapter_included_conservatively(self) -> None:
        """Plan targeting adapter NOT in adapters dict is included (conservative)."""
        from medre.core.engine.replay import _filter_plans_by_capability

        # Plan targets "adapter-unknown" which is absent from adapters dict.
        plan = _make_delivery_plan(adapter="adapter-unknown")
        result = _filter_plans_by_capability(
            self._make_event(),
            [plan],
            adapters={},
        )
        assert result == [plan]


# ===================================================================
# _stage_deliver capability filtering
# ===================================================================


class TestStageDeliverCapabilityFilter:
    """Tests for _stage_deliver BEST_EFFORT capability-aware filtering."""

    async def test_best_effort_filters_by_capability(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT filters unsupported event kinds."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import ReplayRequest

        # Create a route that matches sample_event (fake_transport → target-adapter)
        route = Route(
            id="cap-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="target-adapter")],
        )
        router = Router(routes=[route])

        caps = AdapterCapabilities(text=False)

        class _CapAdapter:
            _capabilities = caps

        class _Config:
            adapters = {"target-adapter": _CapAdapter()}

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        # Find the deliver-stage result
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"
        assert "capability_suppressed" in (deliver_results[0].error or "")

    async def test_dry_run_skips_capability_filter(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """DRY_RUN mode doesn't filter by capability."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import ReplayRequest

        route = Route(
            id="cap-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="target-adapter")],
        )
        router = Router(routes=[route])

        caps = AdapterCapabilities(text=False)

        class _CapAdapter:
            _capabilities = caps

        class _Config:
            adapters = {"target-adapter": _CapAdapter()}

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.DRY_RUN)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"
        assert "dry_run" in (deliver_results[0].error or "")

    async def test_accounting_recorded_when_all_plans_filtered(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """When all plans filtered by capability, accounting is called."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import ReplayRequest
        from medre.core.supervision.accounting import RuntimeAccounting

        route = Route(
            id="cap-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="target-adapter")],
        )
        router = Router(routes=[route])

        caps = AdapterCapabilities(text=False)

        class _CapAdapter:
            _capabilities = caps

        class _Config:
            adapters = {"target-adapter": _CapAdapter()}

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        accounting = RuntimeAccounting()
        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=accounting)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"

        snap = accounting.snapshot()
        assert snap["capability_suppressed"] >= 1

    async def test_partial_suppression_accounting(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Partial suppression: only SOME plans filtered, accounting counts correctly."""
        from medre.core.contracts.adapter import AdapterCapabilities
        from medre.core.engine.replay import ReplayRequest
        from medre.core.events import CanonicalEvent, EventMetadata
        from medre.core.supervision.accounting import RuntimeAccounting

        # Build a message.file event — capability check uses caps.attachments.
        file_event = CanonicalEvent(
            event_id="file-001",
            event_kind="message.file",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "see attached", "url": "https://example.com/f.pdf"},
            metadata=EventMetadata(),
        )

        # Route with TWO targets: one supports attachments, one does not.
        route = Route(
            id="dual-target-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.file",),
                channel="ch-0",
            ),
            targets=[
                RouteTarget(adapter="adapter-with-attachments", channel="ch-ok"),
                RouteTarget(adapter="adapter-no-attachments", channel="ch-skip"),
            ],
        )
        router = Router(routes=[route])

        class _AdapterWithAttachments:
            _capabilities = AdapterCapabilities(attachments=True)

        class _AdapterNoAttachments:
            _capabilities = AdapterCapabilities(attachments=False)

        class _Config:
            adapters = {
                "adapter-with-attachments": _AdapterWithAttachments(),
                "adapter-no-attachments": _AdapterNoAttachments(),
            }

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        accounting = RuntimeAccounting()
        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=accounting)
        await temp_storage.append(file_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        # Accounting snapshot: exactly 1 plan suppressed (not 2).
        snap = accounting.snapshot()
        assert snap["capability_suppressed"] == 1

        # The supported target should have delivered successfully.
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "passed"

        # The unsupported target is filtered out — only 1 plan survives
        # in the replay envelope output.
        output = deliver_results[0].output
        assert output["replay"] is True
        adapter_results = output["adapter_results"]
        assert len(adapter_results) == 1
