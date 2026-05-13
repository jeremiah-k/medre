"""Replay engine tests: ReplayEngine with SQLiteStorage and pipeline components.

Tests the five replay modes (STRICT, RE_RENDER, RE_ROUTE, BEST_EFFORT,
DRY_RUN), count_matching, empty-result handling, deterministic ordering,
diagnostician wiring, target_adapters filtering, schema-version
compatibility, dead-letter retry semantics, and the guarantee that
non-BEST_EFFORT modes do not mutate storage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
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
    ReplayState,
    collect_replay_state,
)
from medre.core.runtime.accounting import RuntimeAccounting


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


def _make_engine(
    storage: SQLiteStorage,
    pipeline: Any | None = None,
    accounting: RuntimeAccounting | None = None,
) -> ReplayEngine:
    """Create a ReplayEngine with the storage cast to StorageBackend protocol.

    SQLiteStorage implements the async-generator style ``query`` method which
    Pyright considers incompatible with the Protocol's ``async def query``.
    The runtime behaviour is correct; the cast bridges the static check gap.
    """
    return ReplayEngine(
        storage=cast(StorageBackend, storage),
        pipeline=pipeline,
        accounting=accounting,
    )


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
        lineage=(),
        relations=(),
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

        engine = _make_engine(temp_storage)
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
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)

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
    # DRY_RUN mode
    # ------------------------------------------------------------------

    async def test_dry_run_executes_all_stages_except_delivery(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN runs store, route, plan, render but skips delivery."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.DRY_RUN,
        )

        results = [r async for r in engine.replay(request)]

        # store + route + plan + render + deliver (skipped)
        assert len(results) == 5
        stages = [r.stage for r in results]
        assert stages == ["store", "route", "plan", "render", "deliver"]

        # All stages pass except deliver which is skipped
        assert results[0].status == "passed"
        assert results[1].status == "passed"
        assert results[2].status == "passed"
        assert results[3].status == "passed"
        assert results[4].status == "skipped"
        assert "dry_run" in (results[4].error or "")

    async def test_dry_run_produces_no_storage_side_effects(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN mode does not change stored event count."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        _ = [r async for r in engine.replay(request)]

        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    # ------------------------------------------------------------------
    # Deterministic ordering
    # ------------------------------------------------------------------

    async def test_results_are_ordered_deterministically(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Multiple events produce results in consistent order."""
        second = _second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = _make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2

        # Run again – order must be identical
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

        pipeline = _StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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

        pipeline = _StubPipeline(router=router_with_routes)
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
        engine = _make_engine(temp_storage, pipeline=pipeline)
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
    # Failed historical adapter (route to adapter that no longer exists)
    # ------------------------------------------------------------------

    async def test_best_effort_adapter_failure_diagnostics(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT adapter failure records diagnostic events."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Adapter 'gone' not registered"),
        )

        diag = Diagnostician()
        engine = _make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 5
        assert results[4].status == "error"

        snap = diag.snapshot()
        assert len(snap["adapter_failures"]) > 0

    # ------------------------------------------------------------------
    # Non-BEST_EFFORT modes: no storage side effects
    # ------------------------------------------------------------------

    async def test_re_route_no_storage_mutation(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_ROUTE never writes to storage."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(router=router_with_routes)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        _ = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        assert count_before == count_after == 1

    async def test_dry_run_no_storage_mutation(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN never writes to storage."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        _ = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        assert count_before == count_after == 1

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

        engine = _make_engine(temp_storage)
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

        engine = _make_engine(temp_storage)
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
        engine = _make_engine(temp_storage)
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
        engine = _make_engine(temp_storage)
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

        # Empty router – no routes will match
        empty_router = Router(routes=[])
        pipeline = _StubPipeline(router=empty_router)

        diag = Diagnostician()
        engine = _make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # store + route (failed)
        assert len(results) == 3
        assert results[1].status == "failed"

        snap = diag.snapshot()
        assert "No routes matched" in snap["replay_skips"]

    # ------------------------------------------------------------------
    # target_adapters filtering
    # ------------------------------------------------------------------

    async def test_target_adapters_filters_delivery(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """target_adapters excludes delivery to non-listed adapters."""
        await temp_storage.append(sample_event)

        # Create a plan-like object with a target.adapter attribute
        class _FakeTarget:
            def __init__(self, adapter: str) -> None:
                self.adapter = adapter

        class _FakePlan:
            def __init__(self, adapter: str) -> None:
                self.target = _FakeTarget(adapter)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=[_FakePlan("adapter_a")])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
            target_adapters=["other_adapter"],
        )

        results = [r async for r in engine.replay(request)]
        # The deliver stage should be skipped because no plans match
        deliver_result = results[4]
        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "skipped"
        assert "target_adapters" in (deliver_result.error or "")

    # ------------------------------------------------------------------
    # Replay never mutates CanonicalEvent
    # ------------------------------------------------------------------

    async def test_replay_never_mutates_historical_event(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """All modes preserve the original event bytes and fields."""
        await temp_storage.append(sample_event)

        # Snapshot original
        orig_id = sample_event.event_id
        orig_kind = sample_event.event_kind
        orig_payload = dict(sample_event.payload)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)

        for mode in (
            ReplayMode.STRICT,
            ReplayMode.RE_RENDER,
            ReplayMode.RE_ROUTE,
            ReplayMode.DRY_RUN,
        ):
            request = ReplayRequest(mode=mode)
            _ = [r async for r in engine.replay(request)]

            # Original object unchanged
            assert sample_event.event_id == orig_id
            assert sample_event.event_kind == orig_kind
            assert dict(sample_event.payload) == orig_payload

            # Stored version unchanged
            stored = await temp_storage.get(sample_event.event_id)
            assert stored is not None
            assert stored.event_id == orig_id
            assert stored.event_kind == orig_kind
            assert dict(stored.payload) == orig_payload

    # ------------------------------------------------------------------
    # Dead-letter / retry replay semantics
    # ------------------------------------------------------------------

    async def test_dead_letter_retry_replay_via_best_effort(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT replay of a previously-failed event captures error.

        This tests the 'retry' semantics: replaying an event whose prior
        delivery failed.  In Phase 1, retry is BEST_EFFORT replay scoped
        to events with failed delivery receipts, not a separate mode.
        """
        from medre.core.events import DeliveryReceipt

        # Store event and a failed receipt for it
        await temp_storage.append(sample_event)

        failed_receipt = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-retry-001",
            event_id=sample_event.event_id,
            delivery_plan_id="plan-001",
            target_adapter="broken_adapter",
            status="failed",
            error="Connection refused",
        )
        await temp_storage.append_receipt(failed_receipt)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Still broken"),
        )

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            correlation_ids=[sample_event.event_id],
            mode=ReplayMode.BEST_EFFORT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 5
        deliver_result = results[4]
        assert deliver_result.status == "error"
        assert "Still broken" in (deliver_result.error or "")

    # ------------------------------------------------------------------
    # collect_replay_state aggregation
    # ------------------------------------------------------------------

    async def test_collect_replay_state_aggregates(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """collect_replay_state correctly aggregates multi-event results."""
        second = _second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = _make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        state = await collect_replay_state(engine.replay(request))

        assert state.events_processed == 2
        assert state.events_passed == 2
        assert state.events_failed == 0
        assert state.events_skipped == 0

    # ------------------------------------------------------------------
    # ReplayState recording
    # ------------------------------------------------------------------

    def test_replay_state_records_all_statuses(self) -> None:
        """ReplayState correctly counts passed, skipped, failed, error."""
        state = ReplayState()

        state.record(ReplayResult(event_id="a", stage="store", status="passed"))
        state.record(ReplayResult(event_id="b", stage="store", status="skipped"))
        state.record(
            ReplayResult(event_id="c", stage="store", status="failed", error="bad")
        )
        state.record(
            ReplayResult(event_id="d", stage="store", status="error", error="boom")
        )

        assert state.events_processed == 4
        assert state.events_passed == 1
        assert state.events_skipped == 1
        assert state.events_failed == 2
        assert state.errors == ["bad", "boom"]

    def test_replay_state_lineage_tracking(self) -> None:
        """ReplayState updates current_lineage from results."""
        state = ReplayState()

        state.record(ReplayResult(
            event_id="a", stage="store", status="passed",
            lineage=["parent-1"],
        ))
        assert state.current_lineage == ["parent-1"]

        state.record(ReplayResult(
            event_id="b", stage="store", status="passed",
            lineage=["parent-2", "parent-3"],
        ))
        assert state.current_lineage == ["parent-2", "parent-3"]

    # ------------------------------------------------------------------
    # Stage resolution
    # ------------------------------------------------------------------

    def test_resolve_stages_all_modes(self) -> None:
        """_resolve_stages returns correct stages for each mode."""
        from medre.core.storage.replay import _resolve_stages

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
        from medre.core.storage.replay import _resolve_stages

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            target_stages=["store", "render"],
        )
        stages = _resolve_stages(request)
        assert stages == ("store", "render")  # ordered by mode definition

    def test_resolve_stages_target_stages_subset(self) -> None:
        """target_stages only returns stages allowed by the mode."""
        from medre.core.storage.replay import _resolve_stages

        # STRICT only allows "store"; requesting "render" is a no-op
        request = ReplayRequest(
            mode=ReplayMode.STRICT,
            target_stages=["render"],
        )
        stages = _resolve_stages(request)
        assert stages == ()


# ===================================================================
# Track 6: Stress-style replay with deterministic fixtures
# ===================================================================


class TestStressReplay:
    """Bulk replay determinism, mixed-status aggregation, and observability."""

    @staticmethod
    def _make_events(
        n: int, sample_event: CanonicalEvent,
    ) -> list[CanonicalEvent]:
        """Create *n* deterministic events with sequential IDs."""
        events: list[CanonicalEvent] = []
        base_ts = sample_event.timestamp
        for i in range(n):
            events.append(
                CanonicalEvent(
                    event_id=f"stress-{i:04d}",
                    event_kind="message.created",
                    schema_version=1,
                    timestamp=base_ts + timedelta(seconds=i),
                    source_adapter="fake_transport",
                    source_transport_id="node-123",
                    source_channel_id="ch-0",
                    parent_event_id=None,
                    lineage=(),
                    relations=(),
                    payload={"text": f"event {i}"},
                    metadata=EventMetadata(),
                )
            )
        return events

    async def test_stress_replay_50_events_strict(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT replay of 50 events: all pass, deterministic order."""
        events = self._make_events(50, sample_event)
        for e in events:
            await temp_storage.append(e)

        engine = _make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 50

        # All passed
        assert all(r.status == "passed" for r in results)

        # Deterministic order: sequential by timestamp
        result_ids = [r.event_id for r in results]
        assert result_ids == [f"stress-{i:04d}" for i in range(50)]

        # Replay again — identical ordering
        results2 = [r async for r in engine.replay(request)]
        assert [r.event_id for r in results2] == result_ids

    async def test_stress_replay_50_events_re_render(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """RE_RENDER replay of 50 events: store + render for each, no mutation."""
        events = self._make_events(50, sample_event)
        for e in events:
            await temp_storage.append(e)

        pipeline = _StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 100  # 50 store + 50 render

        # Every other result is a store pass
        store_results = results[::2]
        render_results = results[1::2]
        assert all(r.stage == "store" for r in store_results)
        assert all(r.stage == "render" for r in render_results)
        assert all(r.status == "passed" for r in store_results)
        assert all(r.status == "passed" for r in render_results)

        # Rendered outputs contain the correct text
        for i, r in enumerate(render_results):
            assert f"event {i}" in (r.output.payload.get("text", ""))

        # No storage mutation
        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 50

    async def test_replay_mixed_statuses_aggregation(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Mixed events: 5 good, 3 unregistered kind, 2 missing → correct state."""
        from medre.core.observability.metrics import Diagnostician

        # 5 good events
        good_events = self._make_events(5, sample_event)
        for e in good_events:
            await temp_storage.append(e)

        # 3 unregistered kind events
        for i in range(3):
            bad = CanonicalEvent(
                event_id=f"bad-{i}",
                event_kind=f"unknown.type_{i}",
                schema_version=1,
                timestamp=sample_event.timestamp + timedelta(seconds=100 + i),
                source_adapter="test",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "bad"},
                metadata=EventMetadata(),
            )
            await temp_storage.append(bad)

        diag = Diagnostician()
        engine = _make_engine(temp_storage)
        engine._diagnostician = diag

        # Replay all events
        state = await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.STRICT)),
        )
        assert state.events_processed == 8
        assert state.events_passed == 5
        assert state.events_failed == 3
        assert len(state.errors) == 3

        # Now replay 2 missing IDs via correlation_ids
        request = ReplayRequest(
            correlation_ids=["stress-0049", "nonexistent-a", "nonexistent-b"],
            mode=ReplayMode.STRICT,
        )
        state2 = await collect_replay_state(engine.replay(request))
        # stress-0049 doesn't exist (we only have stress-0000..0004)
        assert state2.events_processed == 3
        assert state2.events_failed == 3

    async def test_replay_observability_snapshot(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """Diagnostician snapshot reflects replay failures across categories."""
        from medre.core.observability.metrics import Diagnostician

        # Store a good event and an unregistered kind event
        await temp_storage.append(sample_event)
        bad_event = CanonicalEvent(
            event_id="obs-bad-001",
            event_kind="unknown.type",
            schema_version=1,
            timestamp=sample_event.timestamp,
            source_adapter="test",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "bad"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(bad_event)

        diag = Diagnostician()
        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        # STRICT replay — one passes, one fails (unregistered kind)
        await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.STRICT)),
        )
        snap = diag.snapshot()
        assert len(snap["replay_downgrades"]) > 0

        # RE_RENDER with a pipeline that raises on render
        bad_pipeline = AsyncMock()
        bad_pipeline.transform_event = AsyncMock(return_value=sample_event)
        bad_pipeline.render_event = AsyncMock(
            side_effect=RuntimeError("renderer crashed"),
        )
        engine2 = _make_engine(temp_storage, pipeline=bad_pipeline)
        engine2._diagnostician = diag

        await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_RENDER)),
        )
        snap2 = diag.snapshot()
        assert len(snap2["renderer_failures"]) > 0

    async def test_stress_replay_dry_run_no_side_effects(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN of 20 events: all stages except deliver, no storage mutation."""
        events = self._make_events(20, sample_event)
        for e in events:
            await temp_storage.append(e)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        results = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        # 20 events × 5 stages = 100 results
        assert len(results) == 100
        assert count_before == count_after == 20

        # Every 5th result (deliver stage) is skipped
        deliver_results = results[4::5]
        assert all(r.stage == "deliver" for r in deliver_results)
        assert all(r.status == "skipped" for r in deliver_results)


# ===================================================================
# Replay accounting tests
# ===================================================================


class TestReplayAccounting:
    """Focused tests for RuntimeAccounting replay counter wiring."""

    async def test_strict_replay_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Successful STRICT replay increments replay_processed."""
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_re_render_replay_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """Successful RE_RENDER replay increments replay_processed."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(rendering_pipeline=rendering_pipeline)
        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, pipeline=pipeline, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_missing_event_increments_rejected(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing event via correlation_ids increments replay_rejected."""
        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(
            correlation_ids=["nonexistent-id"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        c = acc.counters()
        assert c.replay_rejected == 1
        assert c.replay_processed == 0

    async def test_best_effort_crash_increments_rejected(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT replay where _replay_event raises increments rejected.

        All individual stages catch their own exceptions, so triggering this
        path requires an error in the orchestration layer itself.  We patch
        _replay_event to raise, simulating an unrecoverable crash.
        """
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        import unittest.mock

        async def _fake_replay_event(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("unrecoverable crash")
            yield  # noqa: unreachable — makes this an async generator

        with unittest.mock.patch.object(
            engine, "_replay_event", _fake_replay_event,
        ):
            results = [r async for r in engine.replay(request)]

        assert len(results) == 1
        assert results[0].status == "error"
        assert "unrecoverable crash" in (results[0].error or "")

        c = acc.counters()
        assert c.replay_rejected == 1
        assert c.replay_processed == 0

    async def test_multiple_events_accumulate_counters(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Two successful events + one missing → processed=2, rejected=1."""
        second = _second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, accounting=acc)

        # Two real events via query → processed.
        request = ReplayRequest(mode=ReplayMode.STRICT)
        results = [r async for r in engine.replay(request)]
        assert len(results) == 2
        assert acc.counters().replay_processed == 2

        # One missing event via correlation_ids → rejected.
        request_missing = ReplayRequest(
            correlation_ids=["ghost-id"],
            mode=ReplayMode.STRICT,
        )
        _ = [r async for r in engine.replay(request_missing)]
        assert acc.counters().replay_rejected == 1

    async def test_no_accounting_is_graceful(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """ReplayEngine works correctly when accounting is None (default)."""
        await temp_storage.append(sample_event)

        engine = _make_engine(temp_storage)  # No accounting
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]

        assert len(results) == 1
        assert results[0].status == "passed"

    async def test_dry_run_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN replay increments replay_processed (event was replayed)."""
        await temp_storage.append(sample_event)

        pipeline = _StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        acc = RuntimeAccounting()
        engine = _make_engine(temp_storage, pipeline=pipeline, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_other_counters_unchanged_by_replay(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Replay accounting does not touch unrelated counters."""
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        engine = _make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.inbound_accepted == 1
        assert c.outbound_attempts == 1
        assert c.outbound_delivered == 0
        assert c.outbound_failed == 0
        assert c.loop_prevented == 0
        assert c.capacity_rejections == 0
