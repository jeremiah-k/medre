"""Meshtastic delayed outbound native-reference callback tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import QueueDeliveryResult
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
    OutboundNativeRefRecord,
)
from medre.core.events.canonical import CanonicalEvent
from tests.helpers.meshtastic import make_meshtastic_config


def _context(
    callback: Callable[[OutboundNativeRefRecord], Awaitable[None]] | None = None,
) -> AdapterContext:
    async def noop_publish(_event: CanonicalEvent) -> None:
        return None

    return AdapterContext(
        adapter_id="mesh-1",
        event_bus=None,
        publish_inbound=noop_publish,
        logger=logging.getLogger("test.mesh-1"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        record_outbound_native_ref=callback,
    )


async def test_event_id_flows_to_outbound_native_ref_record() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    recorded: list[OutboundNativeRefRecord] = []

    async def on_outbound_ref(record: OutboundNativeRefRecord) -> None:
        recorded.append(record)

    adapter.ctx = _context(on_outbound_ref)
    event_id = "$evt-delayed-001"
    item: dict[str, Any] = {
        "payload": {"text": "hello mesh", "channel_index": 0},
        "channel_index": 0,
        "event_id": event_id,
    }
    delivery = AdapterDeliveryResult(
        native_message_id="987654321",
        native_channel_id="0",
        confirmation_level="local_transport",
        metadata=MappingProxyType(
            {"meshtastic": {"packet_id": 987654321, "channel": 0}}
        ),
    )
    result = QueueDeliveryResult(item=item, delivery_result=delivery)

    await adapter._record_delayed_outbound_ref(result, event_id, delivery)

    assert len(recorded) == 1
    ref = recorded[0]
    assert ref.event_id == event_id
    assert ref.adapter == "mesh-1"
    assert ref.native_channel_id == "0"
    assert ref.native_message_id == "987654321"
    assert ref.confirmation_level == "local_transport"
    assert ref.metadata["meshtastic"]["packet_id"] == 987654321
    assert ref.metadata["meshtastic"]["channel"] == 0
    assert ref.metadata["meshtastic"]["text"] == "hello mesh"


async def test_missing_callback_is_ignored() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    adapter.ctx = _context()
    item: dict[str, Any] = {
        "payload": {"text": "test"},
        "channel_index": 0,
        "event_id": "$evt-no-cb",
    }
    delivery = AdapterDeliveryResult(
        native_message_id="111",
        native_channel_id="0",
        metadata=MappingProxyType({}),
    )
    result = QueueDeliveryResult(item=item, delivery_result=delivery)

    await adapter._record_delayed_outbound_ref(result, "$evt-no-cb", delivery)


async def test_payload_fields_stay_in_meshtastic_metadata_namespace() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    recorded: list[OutboundNativeRefRecord] = []

    async def on_outbound_ref(record: OutboundNativeRefRecord) -> None:
        recorded.append(record)

    adapter.ctx = _context(on_outbound_ref)
    item: dict[str, Any] = {
        "payload": {
            "text": "reaction text",
            "channel_index": 2,
            "reply_id": 42,
            "emoji": 1,
            "channel_name": "ch2",
        },
        "channel_index": 2,
        "event_id": "$evt-full-meta",
    }
    delivery = AdapterDeliveryResult(
        native_message_id="555",
        native_channel_id="2",
        metadata=MappingProxyType(
            {"meshtastic": {"packet_id": 555, "channel": 2, "reply_id": 42}}
        ),
    )
    result = QueueDeliveryResult(item=item, delivery_result=delivery)

    await adapter._record_delayed_outbound_ref(result, "$evt-full-meta", delivery)

    assert len(recorded) == 1
    mesh_metadata = recorded[0].metadata["meshtastic"]
    assert mesh_metadata == {
        "schema_version": 1,
        "packet_id": 555,
        "channel": 2,
        "reply_id": 42,
        "text": "reaction text",
        "emoji": 1,
        "channel_name": "ch2",
    }
    assert not {
        "reply_id",
        "emoji",
        "text",
        "meshnet_name",
        "channel_name",
    }.intersection(recorded[0].metadata)
