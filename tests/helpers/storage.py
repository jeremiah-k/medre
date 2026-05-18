"""Shared helpers for storage tests."""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
)


def make_storage_event(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    payload: dict | None = None,
    source_adapter: str = "fake_transport",
    source_channel_id: str | None = "ch-0",
    relations: tuple[EventRelation, ...] | None = None,
) -> CanonicalEvent:
    """Build a minimal CanonicalEvent for storage tests."""
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
    )
