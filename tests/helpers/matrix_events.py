"""Shared event-building helpers for Matrix renderer and relay attribution
tests. Extracted from test_matrix_renderer.py so that split test modules can
reuse them without cross-module test imports (forbidden by suite policy)."""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)


def make_matrix_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    """Build a minimal CanonicalEvent with generic source adapter."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


class StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for source-config resolution tests."""

    def __init__(
        self,
        adapter_id: str = "radio-alpha",
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


def make_meshtastic_event(
    source_adapter: str = "radio-alpha",
    payload: dict | None = None,
    relations: tuple | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating a Meshtastic source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-mesh-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-42",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello mesh"},
        metadata=metadata,
    )
