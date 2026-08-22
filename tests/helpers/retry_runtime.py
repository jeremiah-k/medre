"""Shared runtime helpers for retry integration tests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting


def make_retry_event(event_id: str | None = None) -> CanonicalEvent:
    """Create a canonical event for retry runtime tests."""
    return CanonicalEvent(
        event_id=event_id or f"evt-{uuid.uuid4()}",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_source",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello from integration test"},
        metadata=EventMetadata(),
    )


def build_retry_runner(
    storage: SQLiteStorage,
    adapters: dict,
    router: Router,
    accounting: RuntimeAccounting,
    *,
    fallback_resolver: FallbackResolver | None = None,
) -> PipelineRunner:
    """Build a real pipeline runner for retry runtime tests."""
    render_pipe = RenderingPipeline()
    render_pipe.register(TextRenderer(), priority=100)
    for adapter_id, adapter in adapters.items():
        platform = getattr(adapter, "platform", None)
        if isinstance(platform, str):
            render_pipe.register_platforms_from({adapter_id: platform})

    config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=fallback_resolver or FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters,
        event_bus=EventBus(),
        rendering_pipeline=render_pipe,
        diagnostician=Diagnostician(),
        route_stats=RouteStats(),
        runtime_accounting=accounting,
    )
    return PipelineRunner(config)


async def start_retry_adapters(adapters: dict) -> None:
    """Start retry test adapters with isolated adapter contexts."""
    for adapter_id, adapter in adapters.items():
        context = AdapterContext(
            adapter_id=adapter_id,
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger(f"test.{adapter_id}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(context)
