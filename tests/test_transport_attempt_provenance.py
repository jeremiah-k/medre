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
from medre.core.contracts.adapter import AdapterContext, AdapterHandoffResult
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


def _context(*, feedback: AsyncMock) -> AdapterContext:
    async def _publish(_event) -> None:
        return None

    return AdapterContext(
        adapter_id="mesh-provenance",
        publish_inbound=_publish,
        logger=logging.getLogger("test.mesh-provenance"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        report_delivery_feedback=feedback,
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


async def test_meshtastic_queue_rejects_outbox_without_provenance() -> None:
    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)

    with pytest.raises(ValueError, match="require immutable attempt_provenance"):
        await queue.enqueue(
            {"text": "hello"},
            0,
            event_id="evt-unbound",
            outbox_id="outbox-unbound",
            attempt_number=1,
        )


async def test_meshtastic_queue_rejects_invalid_provenance_type() -> None:
    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)

    with pytest.raises(TypeError, match="DeliveryAttemptProvenance or None"):
        await queue.enqueue(
            {"text": "hello"},
            0,
            attempt_provenance={"outbox_id": "not-an-envelope"},  # type: ignore[arg-type]
        )


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
    feedback = AsyncMock()
    adapter = MeshtasticAdapter(
        make_meshtastic_config(adapter_id="mesh-provenance", connection_type="fake")
    )
    adapter.ctx = _context(feedback=feedback)

    queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
    await queue.enqueue({"text": "hello"}, 0, attempt_provenance=provenance)
    item = await queue.dequeue()
    assert item is not None

    await adapter._report_deferred_failure(
        QueueTerminalResult(
            item=item,
            outcome="permanent_failed",
            error="radio rejected send",
        )
    )
    terminal_record = feedback.await_args.args[0]
    assert terminal_record.attempt_provenance is provenance
    assert terminal_record.outcome == "permanent_failed"

    delivery = AdapterHandoffResult(
        native_message_id="12345",
        native_channel_id="0",
        confirmation_level="local_transport",
    )
    await adapter._report_deferred_completion(
        QueueDeliveryResult(item=item, handoff=delivery)
    )
    completion = feedback.await_args.args[0]
    assert completion.attempt_provenance is provenance
    assert completion.handoff.native_message_id == "12345"


async def test_meshtastic_callback_drops_corrupted_queue_mirror() -> None:
    provenance = _provenance()
    feedback = AsyncMock()
    adapter = MeshtasticAdapter(
        make_meshtastic_config(adapter_id="mesh-provenance", connection_type="fake")
    )
    adapter.ctx = _context(feedback=feedback)
    item = {
        "event_id": "evt-corrupted",
        "delivery_plan_id": provenance.delivery_plan_id,
        "outbox_id": provenance.outbox_id,
        "attempt_number": provenance.attempt_number,
        "channel_index": 0,
        "payload": {"text": "hello"},
        "attempt_provenance": provenance,
    }

    await adapter._report_deferred_failure(
        QueueTerminalResult(item=item, outcome="permanent_failed", error="failed")
    )

    feedback.assert_not_awaited()
