"""Extra tests for MeshtasticAdapter delivery: queue lifecycle evidence
(enqueued vs sent delivery_status).

Split from test_meshtastic_adapter_delivery.py to stay under 1500 lines.
"""

from __future__ import annotations

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.core.contracts.adapter import AdapterDeliveryResult
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_rendering_result,
)


# ===================================================================
# Queue lifecycle evidence: delivery_status enqueued vs sent
# ===================================================================


class TestDeliveryStatusEnqueuedVsSent:
    """Evidence gap bridge: delivery_status distinguishes enqueued from sent.

    Meshtastic is the only adapter where deliver() returns
    native_message_id=None.  The real native ID arrives later via
    async queue callback.  These tests prove:

    1. deliver() returns delivery_status='enqueued' (not 'sent').
    2. queue.process_one() returns delivery_status='sent' with real ID.
    3. If stop/crash occurs between enqueue and send, evidence shows
       enqueued-only state (native_message_id=None).
    4. The two states are distinguishable via delivery_status.
    """

    async def test_deliver_returns_enqueued_status(self) -> None:
        """deliver() returns delivery_status='enqueued'."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_queue_process_one_returns_sent_status(self) -> None:
        """process_one with send_fn returns delivery_status='sent'."""
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 42}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.delivery_status == "sent"
        assert result.delivery_result.native_message_id == "42"

    async def test_crash_between_enqueue_and_send_shows_enqueued_only(
        self,
    ) -> None:
        """Simulate stop/crash between enqueue and send.

        After enqueue, the queue contains the item but process_one has
        not been called.  The only evidence is the AdapterDeliveryResult
        from deliver(), which shows:
          - delivery_status='enqueued'
          - native_message_id=None
          - delivery_note='locally enqueued'

        This is the correct evidence for an enqueue-only state.
        """
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        # Enqueue without processing — simulates crash between enqueue
        # and queue drain.
        result = make_meshtastic_rendering_result(event_id="evt-crash")
        delivery = await adapter.deliver(result)

        # Evidence shows enqueued-only state
        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

        # Queue still has the item — it was never sent
        assert adapter._queue.queue_depth == 1
        assert adapter._queue.total_sent == 0

    async def test_enqueue_then_send_produces_both_states(self) -> None:
        """Full lifecycle: enqueue produces 'enqueued', then process_one
        produces 'sent' with real native_message_id."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        # Phase 1: Enqueue
        result = make_meshtastic_rendering_result(event_id="evt-lifecycle")
        delivery = await adapter.deliver(result)

        # Enqueue evidence
        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None

        # Phase 2: Simulate queue drain by calling process_one
        # (adapter._session is None in fake mode, so send_one returns None)
        # Instead, directly process from the queue.
        queue = adapter._queue

        async def fake_send(item):
            return {"packet_id": 999}

        queue_result = await queue.process_one(send_fn=fake_send)

        # Send evidence
        assert queue_result is not None
        assert queue_result.delivery_result.delivery_status == "sent"
        assert queue_result.delivery_result.native_message_id == "999"
        assert queue_result.item.get("event_id") == "evt-lifecycle"

    async def test_enqueued_and_sent_are_distinguishable(self) -> None:
        """Two AdapterDeliveryResult instances — one enqueued, one sent —
        are distinguishable by delivery_status."""
        # Enqueued result (from adapter.deliver)
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        enqueue_result = await adapter.deliver(make_meshtastic_rendering_result())
        assert enqueue_result is not None
        assert enqueue_result.delivery_status == "enqueued"

        # Sent result (from queue.process_one)
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 123}

        sent_result = await queue.process_one(send_fn=fake_send)
        assert sent_result is not None
        assert sent_result.delivery_result.delivery_status == "sent"

        # They are different
        assert enqueue_result.delivery_status != sent_result.delivery_result.delivery_status

    async def test_default_delivery_status_is_sent(self) -> None:
        """AdapterDeliveryResult defaults to delivery_status='sent'."""
        result = AdapterDeliveryResult(native_message_id="123")
        assert result.delivery_status == "sent"

    async def test_multiple_enqueues_all_show_enqueued(self) -> None:
        """Multiple deliver() calls all return delivery_status='enqueued'."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        for i in range(3):
            result = make_meshtastic_rendering_result(event_id=f"evt-{i}")
            delivery = await adapter.deliver(result)
            assert delivery is not None
            assert delivery.delivery_status == "enqueued"
            assert delivery.native_message_id is None

        # All 3 are enqueued, none sent
        assert adapter._queue.queue_depth == 3
        assert adapter._queue.total_sent == 0
