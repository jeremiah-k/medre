"""Shared helpers for replay engine tests.

Provides the StubPipeline, engine factory, and event builders used across
the split replay test modules (engine, policy, accounting, capacity,
traceability).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.planning import FallbackResolver
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Router
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import ReplayEngine
from medre.core.runtime.accounting import RuntimeAccounting


# ---------------------------------------------------------------------------
# Stub pipeline
# ---------------------------------------------------------------------------


class StubPipeline:
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
        """Identity transform -- no-op for testing."""
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
        """No-op delivery for testing -- returns plans as pseudo-receipts."""
        return plans


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def make_engine(
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


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def make_second_event(sample_event: CanonicalEvent) -> CanonicalEvent:
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


def make_events(n: int, sample_event: CanonicalEvent) -> list[CanonicalEvent]:
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rendering_pipeline() -> RenderingPipeline:
    """RenderingPipeline with TextRenderer registered."""
    pipeline = RenderingPipeline()
    pipeline.register(TextRenderer(), priority=100)
    return pipeline
