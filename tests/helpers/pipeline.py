"""Shared helpers for pipeline integration tests.

Provides factory functions for creating CanonicalEvent and PipelineConfig
instances used across the split pipeline test modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from medre.core.engine.pipeline import PipelineConfig
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.routing import Router
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend


def make_event(
    event_id: str = "evt-001",
    event_kind: str = "message.created",
    source_adapter: str = "fake_transport",
    source_channel_id: str | None = "ch-0",
    payload: dict | None = None,
    source_native_ref: NativeRef | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for pipeline tests."""
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
        relations=relations or (),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
        source_native_ref=source_native_ref,
    )


def make_pipeline_config_for_pipeline(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict | None = None,
    event_bus: EventBus | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with sensible defaults for testing.

    Uses ``cast(StorageBackend, storage)`` for type compatibility with
    the PipelineConfig constructor, matching the original test helpers.
    """
    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
    )
