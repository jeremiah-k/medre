"""Tests for Meshtastic delayed outbound ref: correlation, propagation, metadata.

Verifies:
- Supplemental receipt channel correlation (queued → sent by outbox_id).
- delivery_plan_id propagation through the Meshtastic queue path.
- Metadata key splitting (meshtastic namespace vs transport keys).
- Transport-specific namespace facts (reply_id, emoji, channel, packet_id).

These tests exercise the adapter's _record_delayed_outbound_ref and
_append_queued_to_sent_receipt code paths, not the queue evidence counters
(which live in test_meshtastic_queue_evidence.py).
"""

from __future__ import annotations

from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

# ===================================================================
# Supplemental receipt correlation (queued → sent by channel)
# ===================================================================


class TestSupplementalReceiptChannelCorrelation:
    """_append_queued_to_sent_receipt correlates by event_id + adapter + channel.

    When one event fanouts to the same adapter on multiple channels,
    the supplemental "sent" receipt must attach to the correct queued
    parent (matching by channel).  Ambiguous cases produce no receipt.
    """

    async def test_two_channels_correlate_correctly(self, temp_storage) -> None:
        """One event → two queued receipts (ch 0 and ch 1) on same adapter.
        Callback for ch 0 → sent receipt parents ch 0 queued receipt."""
        from datetime import datetime, timezone

        from medre.core.contracts.adapter import OutboundNativeRefRecord
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.routing import Router
        from medre.core.storage.backend import DeliveryOutboxItem

        event_id = "evt-two-ch"

        # Manually insert two queued receipts on different channels.
        now = datetime.now(tz=timezone.utc)
        rcpt_ch0 = DeliveryReceipt(
            receipt_id="rcpt-ch0",
            event_id=event_id,
            delivery_plan_id="plan-ch0",
            target_adapter="mesh-1",
            target_channel="0",
            route_id="route-a",
            status="queued",
            created_at=now,
            outbox_id="obox-ch0",
        )
        rcpt_ch1 = DeliveryReceipt(
            receipt_id="rcpt-ch1",
            event_id=event_id,
            delivery_plan_id="plan-ch1",
            target_adapter="mesh-1",
            target_channel="1",
            route_id="route-b",
            status="queued",
            created_at=now,
            outbox_id="obox-ch1",
        )
        await temp_storage.append_receipt(rcpt_ch0)
        await temp_storage.append_receipt(rcpt_ch1)

        # Create matching outbox items for exact correlation.
        obox_ch0 = DeliveryOutboxItem(
            outbox_id="obox-ch0",
            event_id=event_id,
            route_id="route-a",
            delivery_plan_id="plan-ch0",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        obox_ch1 = DeliveryOutboxItem(
            outbox_id="obox-ch1",
            event_id=event_id,
            route_id="route-b",
            delivery_plan_id="plan-ch1",
            target_adapter="mesh-1",
            target_channel="1",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(obox_ch0)
        await temp_storage.create_outbox_item(obox_ch1)
        await temp_storage.mark_outbox_queued("obox-ch0")
        await temp_storage.mark_outbox_queued("obox-ch1")

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={},
                event_bus=EventBus(),
            )
        )

        # Callback for channel "0".
        record_ch0 = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-0",
            delivery_plan_id="plan-ch0",
            outbox_id="obox-ch0",
            attempt_number=1,
        )
        await runner._append_queued_to_sent_receipt(record=record_ch0, now=now)

        # Callback for channel "1".
        record_ch1 = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id="1",
            native_message_id="packet-1",
            delivery_plan_id="plan-ch1",
            outbox_id="obox-ch1",
            attempt_number=1,
        )
        await runner._append_queued_to_sent_receipt(record=record_ch1, now=now)

        # Verify both supplemental receipts created.
        receipts = await temp_storage.list_receipts_for_event(event_id)
        sent_receipts = [r for r in receipts if r.status == "sent"]
        assert len(sent_receipts) == 2

        # Channel 0 sent receipt → parents ch0 queued receipt.
        sent_ch0 = [r for r in sent_receipts if r.target_channel == "0"]
        assert len(sent_ch0) == 1
        assert sent_ch0[0].parent_receipt_id == "rcpt-ch0"
        assert sent_ch0[0].delivery_plan_id == "plan-ch0"
        assert sent_ch0[0].route_id == "route-a"
        assert sent_ch0[0].adapter_message_id == "packet-0"

        # Channel 1 sent receipt → parents ch1 queued receipt.
        sent_ch1 = [r for r in sent_receipts if r.target_channel == "1"]
        assert len(sent_ch1) == 1
        assert sent_ch1[0].parent_receipt_id == "rcpt-ch1"
        assert sent_ch1[0].delivery_plan_id == "plan-ch1"
        assert sent_ch1[0].route_id == "route-b"
        assert sent_ch1[0].adapter_message_id == "packet-1"

    async def test_ambiguous_no_channel_produces_no_receipt(self, temp_storage) -> None:
        """Multiple queued candidates + no channel on record → no receipt."""
        from datetime import datetime, timezone

        from medre.core.contracts.adapter import OutboundNativeRefRecord
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.routing import Router

        event_id = "evt-ambiguous"
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts with SAME delivery_plan_id, different channels.
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-a",
                event_id=event_id,
                delivery_plan_id="plan-shared",
                target_adapter="mesh-1",
                target_channel="0",
                route_id="route-x",
                status="queued",
                created_at=now,
            )
        )
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-b",
                event_id=event_id,
                delivery_plan_id="plan-shared",
                target_adapter="mesh-1",
                target_channel="1",
                route_id="route-y",
                status="queued",
                created_at=now,
            )
        )

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={},
                event_bus=EventBus(),
            )
        )

        # Record with NO channel → ambiguous (same plan, different channels).
        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="packet-amb",
            delivery_plan_id="plan-shared",
        )
        await runner._append_queued_to_sent_receipt(record=record, now=now)

        receipts = await temp_storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_single_candidate_no_channel_succeeds(self, temp_storage) -> None:
        """One queued candidate + no channel on record → receipt appended."""
        from datetime import datetime, timezone

        from medre.core.contracts.adapter import OutboundNativeRefRecord
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.routing import Router
        from medre.core.storage.backend import DeliveryOutboxItem

        event_id = "evt-single-cand"
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-only",
                event_id=event_id,
                delivery_plan_id="plan-only",
                target_adapter="mesh-1",
                target_channel="0",
                route_id="route-z",
                status="queued",
                created_at=now,
                outbox_id="obox-single",
            )
        )

        # Create matching outbox item for exact correlation.
        await temp_storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="obox-single",
                event_id=event_id,
                route_id="route-z",
                delivery_plan_id="plan-only",
                target_adapter="mesh-1",
                target_channel="0",
                status="in_progress",
            )
        )
        await temp_storage.mark_outbox_queued("obox-single")

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={},
                event_bus=EventBus(),
            )
        )

        # No channel but only one candidate → OK.
        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="packet-single",
            delivery_plan_id="plan-only",
            outbox_id="obox-single",
            attempt_number=1,
        )
        await runner._append_queued_to_sent_receipt(record=record, now=now)

        receipts = await temp_storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-only"
        assert sent[0].delivery_plan_id == "plan-only"
        assert sent[0].adapter_message_id == "packet-single"

    async def test_retry_chooses_most_recent(self, temp_storage) -> None:
        """Multiple queued receipts on same channel (retries) → last one wins."""
        from datetime import datetime, timedelta, timezone

        from medre.core.contracts.adapter import OutboundNativeRefRecord
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.routing import Router
        from medre.core.storage.backend import DeliveryOutboxItem

        event_id = "evt-retry"
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts on the same channel (retry scenario).
        # Same plan_id = retry lineage. Both carry the same outbox_id so the
        # exact outbox correlation filter can find them.
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-first",
                event_id=event_id,
                delivery_plan_id="plan-retry",
                target_adapter="mesh-1",
                target_channel="0",
                route_id="route-r",
                status="queued",
                attempt_number=1,
                created_at=now - timedelta(minutes=5),
                outbox_id="obox-retry",
            )
        )
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-retry",
                event_id=event_id,
                delivery_plan_id="plan-retry",
                target_adapter="mesh-1",
                target_channel="0",
                route_id="route-r",
                status="queued",
                attempt_number=2,
                created_at=now,
                outbox_id="obox-retry",
            )
        )

        # Create matching outbox item for exact correlation (attempt 2 = most recent).
        obox = DeliveryOutboxItem(
            outbox_id="obox-retry",
            event_id=event_id,
            route_id="route-r",
            delivery_plan_id="plan-retry",
            target_adapter="mesh-1",
            target_channel="0",
            attempt_number=2,
            status="in_progress",
        )
        await temp_storage.create_outbox_item(obox)
        await temp_storage.mark_outbox_queued("obox-retry")

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={},
                event_bus=EventBus(),
            )
        )

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-retry",
            delivery_plan_id="plan-retry",
            outbox_id="obox-retry",
            attempt_number=2,
        )
        await runner._append_queued_to_sent_receipt(record=record, now=now)

        receipts = await temp_storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"]
        assert len(sent) == 1
        # Should parent the RETRY (most recent) receipt, not the first.
        assert sent[0].parent_receipt_id == "rcpt-retry"
        assert sent[0].delivery_plan_id == "plan-retry"
        assert sent[0].attempt_number == 2


# ===================================================================
# delivery_plan_id propagation through Meshtastic queue
# ===================================================================


class TestDeliveryPlanIdQueuePropagation:
    """Verify delivery_plan_id flows through the Meshtastic queue path
    from enqueue → queue item → OutboundNativeRefRecord.
    """

    async def test_enqueue_stores_delivery_plan_id(self) -> None:
        """enqueue() stores delivery_plan_id in the queue item dict."""
        q = MeshtasticOutboundQueue()
        await q.enqueue(
            {"text": "hello"},
            channel_index=0,
            event_id="evt-1",
            delivery_plan_id="plan-42",
        )

        item = await q.dequeue()
        assert item is not None
        assert item["delivery_plan_id"] == "plan-42"
        assert item["event_id"] == "evt-1"

    async def test_enqueue_without_delivery_plan_id_stores_none(self) -> None:
        """enqueue() without delivery_plan_id stores None."""
        q = MeshtasticOutboundQueue()
        await q.enqueue(
            {"text": "hello"},
            channel_index=0,
            event_id="evt-2",
        )

        item = await q.dequeue()
        assert item is not None
        assert item["delivery_plan_id"] is None

    async def test_process_one_preserves_delivery_plan_id_in_item(self) -> None:
        """process_one() returns item with delivery_plan_id intact."""
        q = MeshtasticOutboundQueue()

        async def fake_send(_item):
            return {"packet_id": "42"}

        await q.enqueue(
            {"text": "hello"},
            channel_index=0,
            event_id="evt-3",
            delivery_plan_id="plan-xyz",
        )
        result = await q.process_one(send_fn=fake_send)

        assert result is not None
        assert result.item["delivery_plan_id"] == "plan-xyz"

    async def test_adapter_deliver_propagates_delivery_plan_id(self) -> None:
        """MeshtasticAdapter.deliver() propagates delivery_plan_id to queue."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus
        from medre.core.rendering.renderer import RenderingResult

        config = MeshtasticConfig(
            adapter_id="test-dpid",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="test-dpid",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            result = RenderingResult(
                event_id="evt-dpid",
                target_adapter="test-dpid",
                target_channel="0",
                payload={"text": "hello", "channel_index": 0},
                delivery_plan_id="plan-via-adapter",
            )
            await adapter.deliver(result)

            # Dequeue and verify delivery_plan_id propagated.
            item = await adapter._queue.dequeue()
            assert item is not None
            assert item["delivery_plan_id"] == "plan-via-adapter"
            assert item["event_id"] == "evt-dpid"
        finally:
            await adapter.stop()

    async def test_record_delayed_outbound_ref_includes_delivery_plan_id(
        self,
    ) -> None:
        """_record_delayed_outbound_ref builds record with delivery_plan_id."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(
            adapter_id="test-rec",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)

        recorded_refs: list[object] = []

        async def mock_record_callback(record: object) -> None:
            recorded_refs.append(record)

        ctx = AdapterContext(
            adapter_id="test-rec",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=mock_record_callback,
        )
        await adapter.start(ctx)
        try:
            # Simulate a queue delivery result with delivery_plan_id.
            queue_result = QueueDeliveryResult(
                item={
                    "payload": {"text": "test msg"},
                    "channel_index": 0,
                    "event_id": "evt-rec",
                    "delivery_plan_id": "plan-propagated",
                },
                delivery_result=AdapterDeliveryResult(
                    native_message_id="pkt-123",
                    native_channel_id="0",
                    delivery_status="sent",
                ),
            )

            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-rec",
                delivery=queue_result.delivery_result,
            )

            # Verify the OutboundNativeRefRecord has delivery_plan_id.
            assert len(recorded_refs) == 1
            ref = recorded_refs[0]
            assert hasattr(ref, "delivery_plan_id")
            assert ref.delivery_plan_id == "plan-propagated"
            assert ref.event_id == "evt-rec"
            assert ref.native_message_id == "pkt-123"
        finally:
            await adapter.stop()


class TestMetadataKeySplitting:
    """_record_delayed_outbound_ref splits delivery.metadata into namespaces.

    Covers adapter.py lines 975-981: the 3-branch loop body that sorts
    metadata keys into ``meshtastic_meta`` or ``send_meta``.

    - key == "meshtastic" + isinstance(v, dict) → merge into meshtastic namespace
    - key in transport_keys → put into meshtastic namespace
    - everything else → defensively normalized into meshtastic namespace
      (non-namespaced delivery keys should not leak to top level)
    """

    async def test_nested_meshtastic_dict_merged(self) -> None:
        """Metadata key ``meshtastic`` with dict value merges into namespace."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-meta1", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded_refs: list[object] = []

        async def mock_record_callback(record: object) -> None:
            recorded_refs.append(record)

        ctx = AdapterContext(
            adapter_id="test-meta1",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=mock_record_callback,
        )
        await adapter.start(ctx)
        try:
            queue_result = QueueDeliveryResult(
                item={"payload": {"text": "hi"}, "channel_index": 0},
                delivery_result=AdapterDeliveryResult(
                    native_message_id="pkt-m1",
                    native_channel_id="0",
                    metadata=MappingProxyType(
                        {"meshtastic": {"hop_limit": 3, "priority": "high"}}
                    ),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-m1",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded_refs) == 1
            ref = recorded_refs[0]
            # Nested dict merged under "meshtastic" key; payload text also
            # lands in the meshtastic namespace per the namespace contract.
            assert ref.metadata["meshtastic"]["hop_limit"] == 3
            assert ref.metadata["meshtastic"]["priority"] == "high"
            assert ref.metadata["meshtastic"]["text"] == "hi"
        finally:
            await adapter.stop()

    async def test_transport_key_goes_to_meshtastic_namespace(self) -> None:
        """Transport keys (channel, packet_id, etc.) go into meshtastic namespace."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-meta2", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded_refs: list[object] = []

        async def mock_record_callback(record: object) -> None:
            recorded_refs.append(record)

        ctx = AdapterContext(
            adapter_id="test-meta2",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=mock_record_callback,
        )
        await adapter.start(ctx)
        try:
            queue_result = QueueDeliveryResult(
                item={"payload": {"text": "hi"}, "channel_index": 0},
                delivery_result=AdapterDeliveryResult(
                    native_message_id="pkt-m2",
                    native_channel_id="0",
                    metadata=MappingProxyType({"channel": 1, "packet_id": 99}),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-m2",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded_refs) == 1
            ref = recorded_refs[0]
            # Transport keys grouped under "meshtastic"
            assert ref.metadata["meshtastic"]["channel"] == 1
            assert ref.metadata["meshtastic"]["packet_id"] == 99
        finally:
            await adapter.stop()

    async def test_other_key_normalised_into_meshtastic_namespace(self) -> None:
        """Non-transport, non-meshtastic keys are defensively normalised into
        the meshtastic namespace rather than leaking to the top level.
        Payload text is transport context and also lands in the meshtastic namespace."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-meta3", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded_refs: list[object] = []

        async def mock_record_callback(record: object) -> None:
            recorded_refs.append(record)

        ctx = AdapterContext(
            adapter_id="test-meta3",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=mock_record_callback,
        )
        await adapter.start(ctx)
        try:
            queue_result = QueueDeliveryResult(
                item={"payload": {"text": "hi"}, "channel_index": 0},
                delivery_result=AdapterDeliveryResult(
                    native_message_id="pkt-m3",
                    native_channel_id="0",
                    metadata=MappingProxyType({"source_bridge": "matrix", "seq": 7}),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-m3",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded_refs) == 1
            ref = recorded_refs[0]
            # Legacy/non-namespaced keys defensively normalised into meshtastic namespace
            assert "source_bridge" not in ref.metadata
            assert "seq" not in ref.metadata
            assert ref.metadata["meshtastic"]["source_bridge"] == "matrix"
            assert ref.metadata["meshtastic"]["seq"] == 7
            # Payload text also in meshtastic namespace (transport context).
            assert ref.metadata["meshtastic"]["text"] == "hi"
        finally:
            await adapter.stop()

    async def test_mixed_metadata_all_three_branches(self) -> None:
        """All three branches exercised in a single call."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-meta4", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded_refs: list[object] = []

        async def mock_record_callback(record: object) -> None:
            recorded_refs.append(record)

        ctx = AdapterContext(
            adapter_id="test-meta4",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=mock_record_callback,
        )
        await adapter.start(ctx)
        try:
            queue_result = QueueDeliveryResult(
                item={"payload": {"text": "hi"}, "channel_index": 0},
                delivery_result=AdapterDeliveryResult(
                    native_message_id="pkt-m4",
                    native_channel_id="0",
                    metadata=MappingProxyType(
                        {
                            "meshtastic": {"hop_limit": 3},
                            "channel": 2,
                            "custom": "value",
                        }
                    ),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-m4",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded_refs) == 1
            ref = recorded_refs[0]
            # Nested meshtastic dict merged with transport key
            mesh_ns = ref.metadata["meshtastic"]
            assert mesh_ns["hop_limit"] == 3
            assert mesh_ns["channel"] == 2
            # Other key defensively normalised into meshtastic namespace
            assert "custom" not in ref.metadata
            assert mesh_ns["custom"] == "value"
        finally:
            await adapter.stop()


class TestDelayedOutboundRefMeshtasticNamespaceFacts:
    """Verify that _record_delayed_outbound_ref stores all transport-specific
    data under the meshtastic namespace in OutboundNativeRefRecord.metadata.
    No transport keys (reply_id, emoji, channel, packet_id, meshnet_name,
    channel_name, text) should appear at the top level of metadata."""

    async def test_reply_id_and_emoji_in_meshtastic_namespace(self) -> None:
        """When the delivery metadata has meshtastic.reply_id and
        meshtastic.emoji from a structured send, these are preserved in
        the OutboundNativeRefRecord.metadata meshtastic namespace."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
            OutboundNativeRefRecord,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-ns-facts", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded: list[OutboundNativeRefRecord] = []

        async def on_ref(record: OutboundNativeRefRecord) -> None:
            recorded.append(record)

        ctx = AdapterContext(
            adapter_id="test-ns-facts",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=on_ref,
        )
        await adapter.start(ctx)
        try:
            # Simulate delivery result from a structured send (reply + emoji).
            queue_result = QueueDeliveryResult(
                item={
                    "payload": {
                        "text": "👍",
                        "channel_index": 0,
                        "reply_id": 42,
                        "emoji": 1,
                    },
                    "channel_index": 0,
                    "event_id": "evt-reaction-ns",
                    "delivery_plan_id": "plan-ns",
                },
                delivery_result=AdapterDeliveryResult(
                    native_message_id="789",
                    native_channel_id="0",
                    delivery_status="sent",
                    metadata=MappingProxyType(
                        {
                            "meshtastic": {
                                "packet_id": 789,
                                "channel": 0,
                                "reply_id": 42,
                                "emoji": 1,
                            },
                        }
                    ),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-reaction-ns",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded) == 1
            ref = recorded[0]
            mesh_ns = ref.metadata["meshtastic"]

            # All transport facts from the delivery snapshot are present.
            assert mesh_ns["packet_id"] == 789
            assert mesh_ns["channel"] == 0
            assert mesh_ns["reply_id"] == 42
            assert mesh_ns["emoji"] == 1

            # Payload-level facts also present in meshtastic namespace.
            assert mesh_ns["text"] == "👍"

            # No transport keys leak to top-level metadata.
            assert "reply_id" not in ref.metadata
            assert "emoji" not in ref.metadata
            assert "channel" not in ref.metadata
            assert "packet_id" not in ref.metadata
            assert "meshnet_name" not in ref.metadata
            assert "channel_name" not in ref.metadata
            assert "text" not in ref.metadata
        finally:
            await adapter.stop()

    async def test_send_without_relation_fields_no_reply_emoji_in_namespace(
        self,
    ) -> None:
        """When the delivery snapshot has no reply_id/emoji, the meshtastic
        namespace should not contain them."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.queue import QueueDeliveryResult
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import (
            AdapterContext,
            AdapterDeliveryResult,
            OutboundNativeRefRecord,
        )
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="test-ns-plain", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        recorded: list[OutboundNativeRefRecord] = []

        async def on_ref(record: OutboundNativeRefRecord) -> None:
            recorded.append(record)

        ctx = AdapterContext(
            adapter_id="test-ns-plain",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
            record_outbound_native_ref=on_ref,
        )
        await adapter.start(ctx)
        try:
            queue_result = QueueDeliveryResult(
                item={
                    "payload": {"text": "plain msg", "channel_index": 0},
                    "channel_index": 0,
                    "event_id": "evt-plain-ns",
                },
                delivery_result=AdapterDeliveryResult(
                    native_message_id="321",
                    native_channel_id="0",
                    delivery_status="sent",
                    metadata=MappingProxyType(
                        {"meshtastic": {"packet_id": 321, "channel": 0}}
                    ),
                ),
            )
            await adapter._record_delayed_outbound_ref(
                queue_result,
                event_id="evt-plain-ns",
                delivery=queue_result.delivery_result,
            )

            assert len(recorded) == 1
            ref = recorded[0]
            mesh_ns = ref.metadata["meshtastic"]
            assert mesh_ns["packet_id"] == 321
            assert mesh_ns["channel"] == 0
            assert "reply_id" not in mesh_ns
            assert "emoji" not in mesh_ns
        finally:
            await adapter.stop()
