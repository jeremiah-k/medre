"""Replay engine tests: ReplayEngine with SQLiteStorage and pipeline components.

Tests the four replay modes (STRICT, RE_RENDER, RE_ROUTE, BEST_EFFORT),
count_matching, empty-result handling, and the guarantee that non-BEST_EFFORT
modes do not mutate storage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.planning import FallbackResolver
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import EventFilter, SQLiteStorage
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayState,
    collect_replay_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rendering_pipeline() -> RenderingPipeline:
    """RenderingPipeline with TextRenderer registered."""
    pipeline = RenderingPipeline()
    pipeline.register(TextRenderer(), priority=100)
    return pipeline


# ---------------------------------------------------------------------------
# Stub pipeline for replay modes that require a _PipelineProtocol
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _second_event(sample_event: CanonicalEvent) -> CanonicalEvent:
    """Create a second event distinct from *sample_event*."""
    return CanonicalEvent(
        event_id="test-002",
        event_kind="message.created",
        schema_version=1,
        timestamp=sample_event.timestamp,
        source_adapter="fake_transport",
        source_transport_id="node-123",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=[],
        relations=[],
        payload={"text": "second event"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Tests
# ===================================================================


class TestReplayEngine:
    """Test replay engine with storage and pipeline."""

    async def test_strict_mode_verifies_events(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT mode reads events and validates they exist."""
        await temp_storage.append(sample_event)

        engine = ReplayEngine(storage=temp_storage)
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

        pipeline = _StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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

        pipeline = _StubPipeline(router=router_with_routes)
        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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
        assert len(results[2].output) == 1  # one target → one plan

        # Verify no storage mutation
        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    async def test_empty_request_returns_no_results(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """No matching events returns empty iterator."""
        engine = ReplayEngine(storage=temp_storage)
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

        second = _second_event(sample_event)
        await temp_storage.append(second)

        engine = ReplayEngine(storage=temp_storage)
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
            lineage=[],
            relations=[],
            payload={"text": "test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = ReplayEngine(storage=temp_storage)
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

        engine = ReplayEngine(storage=temp_storage)
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
            lineage=["ancestor-1", "ancestor-2"],
            relations=[],
            payload={"text": "derived"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = ReplayEngine(storage=temp_storage)
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

        pipeline = _StubPipeline(rendering_pipeline=rendering_pipeline)

        # Wrap render_event to track calls
        render_calls: list[CanonicalEvent] = []
        original_render = pipeline.render_event

        async def tracking_render(event: CanonicalEvent) -> Any:
            render_calls.append(event)
            return await original_render(event)

        pipeline.render_event = tracking_render

        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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

        pipeline = _StubPipeline(router=router_with_routes)

        # Wrap route_event to track calls
        route_calls: list[CanonicalEvent] = []
        original_route = pipeline.route_event

        async def tracking_route(
            event: CanonicalEvent,
        ) -> list[tuple[Any, list[Any]]]:
            route_calls.append(event)
            return await original_route(event)

        pipeline.route_event = tracking_route

        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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

    async def test_best_effort_tolerates_missing_adapter(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT mode returns error results instead of crashing."""
        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Adapter 'missing' not found"),
        )

        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
        )

        # Must not raise — error is captured in results
        results = [r async for r in engine.replay(request)]

        assert len(results) == 5  # store, route, plan, render, deliver

        # The deliver stage should have error status
        deliver_result = results[4]
        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "error"
        assert "Adapter 'missing' not found" in (deliver_result.error or "")

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

        engine = ReplayEngine(storage=temp_storage)
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

        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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

        pipeline = _StubPipeline(router=router_with_routes)

        deliver_called = False
        original_deliver = pipeline.deliver

        async def tracking_deliver(
            event: CanonicalEvent, plans: list[Any],
        ) -> list[Any]:
            nonlocal deliver_called
            deliver_called = True
            return await original_deliver(event, plans)

        pipeline.deliver = tracking_deliver

        engine = ReplayEngine(storage=temp_storage, pipeline=pipeline)
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
            lineage=[sample_event.event_id],
            relations=[],
            payload={"text": "derived from parent"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(sample_event)
        await temp_storage.append(derived)

        engine = ReplayEngine(storage=temp_storage)
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

        engine = ReplayEngine(storage=temp_storage)
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
            lineage=[],
            relations=[],
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
            lineage=[],
            relations=[],
            payload={"status": "online"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(evt_old)
        await temp_storage.append(evt_presence)
        await temp_storage.append(sample_event)

        engine = ReplayEngine(storage=temp_storage)

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
