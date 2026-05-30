"""ReplayEngine mode semantics: STRICT, RE_RENDER, RE_ROUTE behavior.

Tests mode-specific stage execution, mutation safety, lineage/relations
preservation, and boundary conditions for each replay mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

from medre.core.engine.replay import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Router
from medre.core.storage import SQLiteStorage
from tests.helpers.replay import StubPipeline, make_engine

# ===================================================================
# STRICT mode
# ===================================================================


class TestStrictMode:
    """STRICT replay mode: read-only verification of stored events."""

    async def test_verifies_events(
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

    async def test_detects_unregistered_kind(
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

    async def test_preserves_lineage(
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

    async def test_does_not_mutate_events(
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


# ===================================================================
# RE_RENDER mode
# ===================================================================


class TestReRenderMode:
    """RE_RENDER replay mode: re-renders events with current renderers."""

    async def test_captures_output(
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
        from medre.core.storage import EventFilter

        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    async def test_uses_current_renderers(
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

    async def test_does_not_call_routing(
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

    async def test_relations_preserved(
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


# ===================================================================
# RE_ROUTE mode
# ===================================================================


class TestReRouteMode:
    """RE_ROUTE replay mode: plans delivery with current route rules."""

    async def test_plans_with_current_routes(
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
        from medre.core.storage import EventFilter

        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    async def test_uses_current_routes(
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

    async def test_does_not_deliver(
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

    async def test_relations_preserved(
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


# ===================================================================
# Cross-cutting: derived events, relations, empty results
# ===================================================================


class TestReplayEventsAndRelations:
    """Derived events, relation events, and empty-request handling."""

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
