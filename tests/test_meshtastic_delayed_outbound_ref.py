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
    AdapterHandoffResult,
)
from medre.core.contracts.delivery import DeferredHandoffCompleted
from medre.core.events.canonical import CanonicalEvent
from tests.helpers.delivery_callbacks import make_attempt_provenance
from tests.helpers.meshtastic import make_meshtastic_config


def _context(
    callback: Callable[[DeferredHandoffCompleted], Awaitable[None]] | None = None,
) -> AdapterContext:
    async def noop_publish(_event: CanonicalEvent) -> None:
        return None

    return AdapterContext(
        adapter_id="mesh-1",
        publish_inbound=noop_publish,
        logger=logging.getLogger("test.mesh-1"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        report_delivery_feedback=callback,
    )


async def test_event_id_flows_to_outbound_native_ref_record() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    recorded: list[DeferredHandoffCompleted] = []

    async def on_outbound_ref(record: DeferredHandoffCompleted) -> None:
        recorded.append(record)

    adapter.ctx = _context(on_outbound_ref)
    event_id = "$evt-delayed-001"
    item: dict[str, Any] = {
        "payload": {"text": "hello mesh", "channel_index": 0},
        "channel_index": 0,
        "event_id": event_id,
        "attempt_provenance": make_attempt_provenance(
            event_id=event_id,
            target_adapter="mesh-1",
            outbox_id="outbox-delayed-001",
            attempt_number=1,
            delivery_plan_id="plan-delayed-001",
            target_channel="0",
        ),
    }
    delivery = AdapterHandoffResult(
        native_message_id="987654321",
        native_channel_id="0",
        confirmation_level="local_transport",
        metadata=MappingProxyType(
            {"meshtastic": {"packet_id": 987654321, "channel": 0}}
        ),
    )
    result = QueueDeliveryResult(item=item, handoff=delivery)

    await adapter._report_deferred_completion(result)

    assert len(recorded) == 1
    feedback = recorded[0]
    assert feedback.attempt_provenance.event_id == event_id
    assert feedback.attempt_provenance.target_adapter == "mesh-1"
    handoff = feedback.handoff
    assert handoff.native_channel_id == "0"
    assert handoff.native_message_id == "987654321"
    assert handoff.confirmation_level == "local_transport"
    assert handoff.metadata["meshtastic"]["packet_id"] == 987654321
    assert handoff.metadata["meshtastic"]["channel"] == 0
    assert handoff.metadata["meshtastic"]["text"] == "hello mesh"


async def test_missing_callback_is_ignored() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    adapter.ctx = _context()
    item: dict[str, Any] = {
        "payload": {"text": "test"},
        "channel_index": 0,
        "event_id": "$evt-no-cb",
    }
    delivery = AdapterHandoffResult(
        native_message_id="111",
        native_channel_id="0",
        metadata=MappingProxyType({}),
    )
    result = QueueDeliveryResult(item=item, handoff=delivery)

    await adapter._report_deferred_completion(result)


async def test_outboxless_delivery_does_not_emit_native_ref_callback() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    recorded: list[DeferredHandoffCompleted] = []

    async def on_outbound_ref(record: DeferredHandoffCompleted) -> None:
        recorded.append(record)

    adapter.ctx = _context(on_outbound_ref)
    item: dict[str, Any] = {
        "payload": {"text": "direct"},
        "channel_index": 0,
        "event_id": "$evt-direct",
    }
    delivery = AdapterHandoffResult(
        native_message_id="222",
        native_channel_id="0",
        metadata=MappingProxyType({}),
    )

    await adapter._report_deferred_completion(
        QueueDeliveryResult(item=item, handoff=delivery)
    )

    assert recorded == []


async def test_payload_fields_stay_in_meshtastic_metadata_namespace() -> None:
    adapter = MeshtasticAdapter(make_meshtastic_config())
    recorded: list[DeferredHandoffCompleted] = []

    async def on_outbound_ref(record: DeferredHandoffCompleted) -> None:
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
        "attempt_provenance": make_attempt_provenance(
            event_id="$evt-full-meta",
            target_adapter="mesh-1",
            outbox_id="outbox-full-meta",
            attempt_number=1,
            delivery_plan_id="plan-full-meta",
            target_channel="2",
        ),
    }
    delivery = AdapterHandoffResult(
        native_message_id="555",
        native_channel_id="2",
        metadata=MappingProxyType(
            {"meshtastic": {"packet_id": 555, "channel": 2, "reply_id": 42}}
        ),
    )
    result = QueueDeliveryResult(item=item, handoff=delivery)

    await adapter._report_deferred_completion(result)

    assert len(recorded) == 1
    mesh_metadata = recorded[0].handoff.metadata["meshtastic"]
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
    }.intersection(recorded[0].handoff.metadata)
