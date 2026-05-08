"""Replay engine tests: ReplayEngine with SQLiteStorage and pipeline components.

Tests the four replay modes (STRICT, RE_RENDER, RE_ROUTE, BEST_EFFORT),
count_matching, empty-result handling, and the guarantee that non-BEST_EFFORT
modes do not mutate storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from meshnet_framework.core.events import CanonicalEvent, EventMetadata
from meshnet_framework.core.planning import FallbackResolver
from meshnet_framework.core.rendering import RenderingPipeline, TextRenderer
from meshnet_framework.core.routing import Route, RouteSource, RouteTarget, Router
from meshnet_framework.core.storage import EventFilter, SQLiteStorage
from meshnet_framework.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
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
