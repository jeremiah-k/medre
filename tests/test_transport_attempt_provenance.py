"""Transport-boundary tests for immutable delivery attempt provenance."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import (
    MeshtasticOutboundQueue,
    QueueDeliveryResult,
    QueueTerminalResult,
)
from medre.core.contracts.adapter import AdapterContext, AdapterDeliveryResult
from medre.core.events import DeliveryAttemptProvenance
from tests.helpers.meshtastic import make_meshtastic_config


def _provenance() -> DeliveryAttemptProvenance:
    return DeliveryAttemptProvenance(
        event_id="evt-provenance",
        delivery_plan_id="plan-provenance",
        target_adapter="mesh-provenance",
        target_channel="0",
        outbox_id="outbox-provenance",
        attempt_number=2,
        source="retry",
        replay_run_id="run-7",
    )


def _context(*, terminal: AsyncMock, native_ref: AsyncMock) -> AdapterContext:
    async def _publish(_event) -> None:
        return None

    return AdapterContext(
        adapter_id="mesh-provenance",
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger("test.mesh-provenance"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        record_outbound_terminal=terminal,
        record_outbound_native_ref=native_ref,
    )


async def test_meshtastic_queue_carries_provenance_outside_wire_payload() -> None:
    provenance = _provenance()
    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)

    await queue.enqueue(
        {"text": "hello"},
        0,
        attempt_provenance=provenance,
    )
    item = await queue.dequeue()

    assert item is not None
    assert item["attempt_provenance"] is provenance
    assert item["event_id"] == provenance.event_id
    assert item["delivery_plan_id"] == provenance.delivery_plan_id
    assert item["outbox_id"] == provenance.outbox_id
    assert item["attempt_number"] == provenance.attempt_number
    assert item["payload"] == {"text": "hello"}
    assert "attempt_provenance" not in item["payload"]
    assert "outbox_id" not in item["payload"]


async def test_meshtastic_queue_rejects_mirror_contradiction() -> None:
    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)

    with pytest.raises(ValueError, match="outbox_id contradicts"):
        await queue.enqueue(
            {"text": "hello"},
            0,
            outbox_id="wrong-outbox",
            attempt_provenance=_provenance(),
        )


async def test_meshtastic_async_callbacks_echo_exact_provenance() -> None:
    provenance = _provenance()
    terminal = AsyncMock()
    native_ref = AsyncMock()
    adapter = MeshtasticAdapter(
        make_meshtastic_config(adapter_id="mesh-provenance", connection_type="fake")
    )
    adapter.ctx = _context(terminal=terminal, native_ref=native_ref)

    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
    await queue.enqueue({"text": "hello"}, 0, attempt_provenance=provenance)
    item = await queue.dequeue()
    assert item is not None

    await adapter._report_queue_terminal(
        QueueTerminalResult(
            item=item,
            outcome="permanent_failed",
            error="radio rejected send",
        )
    )
    terminal_record = terminal.await_args.args[0]
    assert terminal_record.attempt_provenance is provenance
    assert terminal_record.outbox_id == provenance.outbox_id
    assert terminal_record.attempt_number == provenance.attempt_number

    delivery = AdapterDeliveryResult(
        native_message_id="12345",
        native_channel_id="0",
        confirmation_level="local_transport",
    )
    await adapter._record_delayed_outbound_ref(
        QueueDeliveryResult(item=item, delivery_result=delivery),
        provenance.event_id,
        delivery,
    )
    native_record = native_ref.await_args.args[0]
    assert native_record.attempt_provenance is provenance
    assert native_record.outbox_id == provenance.outbox_id
    assert native_record.attempt_number == provenance.attempt_number
