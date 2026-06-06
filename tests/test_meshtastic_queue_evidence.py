"""Tests for Meshtastic outbound queue evidence counters and overflow semantics.

Verifies:
- Queue enqueue under capacity succeeds.
- Queue enqueue at capacity raises MeshtasticSendError(transient=True).
- After queue full rejection, existing queue items are NOT evicted.
- Adapter deliver() on full queue raises AdapterSendError(transient=True).
- Queue diagnostics include depth, max_size, enqueued, rejected counts.
- Queue processing increments sent/failed correctly.
- Adapter diagnostics include queue stats and classifier counters.
- Queue rejection classifies as adapter_transient.
- Docs/examples do not claim queued = RF delivered.
"""

from __future__ import annotations

import pytest

from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryExecutor,
)


class TestQueueMaxQueueSizeValidation:
    """max_queue_size validation: None, positive int, 0, negative, bool."""

    async def test_none_allows_enqueue_and_reports_none(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=None)
        await q.enqueue({"text": "hello"}, channel_index=0)
        assert q.queue_depth == 1
        assert q.max_queue_size is None

    def test_zero_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="max_queue_size must be > 0"):
            MeshtasticOutboundQueue(max_queue_size=0)

    def test_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="max_queue_size must be > 0"):
            MeshtasticOutboundQueue(max_queue_size=-1)

    def test_bool_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="max_queue_size must not be a bool"):
            MeshtasticOutboundQueue(max_queue_size=True)


class TestQueueEnqueueSuccess:
    """Queue enqueue under capacity succeeds and increments counters."""

    async def test_enqueue_under_capacity_succeeds(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=5)
        await q.enqueue({"text": "hello"}, channel_index=0)
        assert q.queue_depth == 1
        assert q.total_enqueued == 1

    async def test_multiple_enqueues_increment_counter(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=10)
        for i in range(5):
            await q.enqueue({"text": f"msg-{i}"}, channel_index=0)
        assert q.queue_depth == 5
        assert q.total_enqueued == 5


class TestQueueFullRejection:
    """Queue rejects on full with MeshtasticSendError(transient=True)."""

    async def test_full_queue_raises_send_error(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=2)
        await q.enqueue({"text": "a"}, channel_index=0)
        await q.enqueue({"text": "b"}, channel_index=0)

        with pytest.raises(MeshtasticSendError, match="queue is full") as exc_info:
            await q.enqueue({"text": "c"}, channel_index=0)
        assert exc_info.value.transient is True

    async def test_rejected_enqueue_increments_rejected_counter(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=1)
        await q.enqueue({"text": "first"}, channel_index=0)

        for _ in range(3):
            with pytest.raises(MeshtasticSendError):
                await q.enqueue({"text": "overflow"}, channel_index=0)

        assert q.total_rejected == 3
        assert q.total_enqueued == 1

    async def test_existing_items_not_evicted_on_rejection(self) -> None:
        """After queue full rejection, existing items are NOT evicted."""
        q = MeshtasticOutboundQueue(max_queue_size=3)
        await q.enqueue({"text": "msg-0"}, channel_index=0)
        await q.enqueue({"text": "msg-1"}, channel_index=0)
        await q.enqueue({"text": "msg-2"}, channel_index=0)

        with pytest.raises(MeshtasticSendError):
            await q.enqueue({"text": "overflow"}, channel_index=0)

        # All three original items should still be present.
        assert q.queue_depth == 3
        item = await q.dequeue()
        assert item is not None
        assert item["payload"]["text"] == "msg-0"
        item = await q.dequeue()
        assert item is not None
        assert item["payload"]["text"] == "msg-1"
        item = await q.dequeue()
        assert item is not None
        assert item["payload"]["text"] == "msg-2"


class TestAdapterDeliverOnFullQueue:
    """Adapter deliver() on full queue raises AdapterSendError(transient=True)."""

    async def test_deliver_on_full_queue_raises_adapter_send_error(self) -> None:
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext, AdapterSendError
        from medre.core.events.bus import EventBus
        from medre.core.rendering.renderer import RenderingResult

        config = MeshtasticConfig(
            adapter_id="test-full",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="test-full",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            # Fill the queue.
            for i in range(adapter._queue.max_queue_size):
                result = RenderingResult(
                    event_id=f"evt-{i}",
                    target_adapter="test-full",
                    target_channel="0",
                    payload={"text": f"msg-{i}", "channel_index": 0},
                )
                await adapter.deliver(result)

            # One more should trigger AdapterSendError(transient=True).
            overflow_result = RenderingResult(
                event_id="evt-overflow",
                target_adapter="test-full",
                target_channel="0",
                payload={"text": "overflow", "channel_index": 0},
            )
            with pytest.raises(AdapterSendError) as exc_info:
                await adapter.deliver(overflow_result)
            assert exc_info.value.transient is True
        finally:
            await adapter.stop()


class TestQueueDiagnostics:
    """Queue diagnostics include all required counters."""

    async def test_queue_health_includes_all_fields(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=5)
        await q.enqueue({"text": "hello"}, channel_index=0)

        health = q.queue_health
        # Current contract: no total_dropped (rejected replaces dropped).
        assert "total_dropped" not in health
        # All expected fields must be present.
        assert "pending_count" in health
        assert "total_sent" in health
        assert "total_failed" in health
        assert "total_enqueued" in health
        assert "total_dequeued" in health
        assert "total_rejected" in health
        assert "max_queue_size" in health
        assert "utilization_pct" in health
        assert "delay_between_messages" in health
        assert "last_send_time" in health

    async def test_queue_health_values_after_operations(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=3)
        await q.enqueue({"text": "a"}, channel_index=0)
        await q.enqueue({"text": "b"}, channel_index=0)

        health = q.queue_health
        assert health["pending_count"] == 2
        assert health["total_enqueued"] == 2
        assert health["total_rejected"] == 0
        assert health["max_queue_size"] == 3

    async def test_utilization_pct_calculation(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=10)
        for i in range(5):
            await q.enqueue({"text": f"msg-{i}"}, channel_index=0)

        health = q.queue_health
        assert health["utilization_pct"] == 50.0

    async def test_utilization_pct_empty_queue(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=10)
        assert q.queue_health["utilization_pct"] == 0.0

    async def test_utilization_pct_unbounded_queue(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=None)
        assert q.queue_health["utilization_pct"] == 0.0


class TestQueueProcessingCounters:
    """Queue processing increments sent/failed correctly."""

    async def test_process_one_increments_sent_on_success(self) -> None:
        q = MeshtasticOutboundQueue()

        async def fake_send(_item):
            return {"packet_id": "42"}

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=fake_send)

        assert result is not None
        assert q.total_sent == 1
        assert q.total_enqueued == 1
        assert q.total_dequeued == 1

    async def test_process_one_increments_failed_on_error(self) -> None:
        """Unknown exceptions are treated as transient and requeued.

        With bounded retry (default max_attempts=3), an unknown exception
        like RuntimeError causes the item to be front-requeued rather than
        dropped.  To verify failed/exhausted behavior, use max_attempts=1.
        """
        q = MeshtasticOutboundQueue(max_attempts=1)

        async def failing_send(_item):
            raise RuntimeError("send failed")

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=failing_send)

        # Item exhausted and dropped (max_attempts=1 → no retries).
        assert result is None
        assert q.total_failed == 1
        assert q.total_exhausted == 1
        assert q.total_sent == 0
        assert q.total_dequeued == 1


class TestAdapterDiagnosticsQueueStats:
    """Adapter diagnostics include queue stats."""

    async def test_adapter_diagnostics_includes_queue_counters(self) -> None:
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="diag-test", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="diag-test",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            # No total_dropped in diagnostics (rejected replaces dropped).
            assert "queue_total_dropped" not in diag
            # All expected queue fields must be present.
            assert "queue_pending" in diag
            assert "queue_total_sent" in diag
            assert "queue_total_failed" in diag
            assert "queue_total_enqueued" in diag
            assert "queue_total_dequeued" in diag
            assert "queue_total_rejected" in diag
            assert "queue_max_size" in diag
            assert "queue_utilization_pct" in diag
            assert "queue_delay_between_messages" in diag
            assert "queue_last_send_time" in diag
        finally:
            await adapter.stop()


class TestQueueDoesNotClaimRfDelivery:
    """Docs/examples do not claim queued = RF delivered."""

    def test_queue_module_docstring_no_rf_claim(self) -> None:
        """Module docstring should not claim queued items are RF-delivered."""
        import medre.adapters.meshtastic.queue as queue_mod

        doc = queue_mod.__doc__ or ""
        # Should NOT claim that enqueued = delivered
        assert "RF-delivered" not in doc
        # Should mention rejection/explicit behavior
        assert "reject" in doc.lower() or "raise" in doc.lower()


class TestAdapterDiagnosticsClassifierCounters:
    """Meshtastic adapter diagnostics include classifier_packets_* counters
    for inbound packet classification evidence."""

    async def test_diagnostics_includes_classifier_counter_keys(self) -> None:
        """All classifier_packets_* counters are present in diagnostics."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="cls-test", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="cls-test",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            # All expected classifier counters
            classifier_keys = [
                "classifier_packets_seen",
                "classifier_packets_relayed",
                "classifier_packets_ignored",
                "classifier_packets_dropped",
                "classifier_packets_deferred",
                "classifier_packets_malformed",
                "classifier_packets_encrypted_dropped",
                "classifier_packets_detection_sensor_deferred",
                "classifier_packets_dm_ignored",
                "classifier_packets_empty_text_ignored",
                "classifier_packets_unknown_portnum_deferred",
            ]
            for key in classifier_keys:
                assert key in diag, f"Missing classifier counter: {key}"
                assert isinstance(
                    diag[key], int
                ), f"Classifier counter {key} should be int, got {type(diag[key])}"
        finally:
            await adapter.stop()

    async def test_classifier_counters_start_at_zero(self) -> None:
        """Classifier counters initialise at zero before any packets."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="cls-zero", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="cls-zero",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert diag["classifier_packets_seen"] == 0
            assert diag["classifier_packets_relayed"] == 0
            assert diag["classifier_packets_ignored"] == 0
            assert diag["classifier_packets_dropped"] == 0
            assert diag["classifier_packets_deferred"] == 0
        finally:
            await adapter.stop()

    async def test_diagnostics_includes_queue_total_rejected(self) -> None:
        """queue_total_rejected counter is present in diagnostics."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="rej-test", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="rej-test",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert "queue_total_rejected" in diag
            assert isinstance(diag["queue_total_rejected"], int)
        finally:
            await adapter.stop()


class TestQueueRejectionTransientClassification:
    """Meshtastic queue-full rejection propagates as adapter_transient
    through the failure classification pipeline."""

    def test_meshtastic_send_error_is_transient(self) -> None:
        """MeshtasticSendError from queue full has transient=True."""
        err = MeshtasticSendError("queue is full", transient=True)
        assert err.transient is True

    def test_meshtastic_send_error_is_adapter_internal_not_core_error(self) -> None:
        """MeshtasticSendError is adapter-internal: it is NOT an AdapterSendError.

        Architecture: MeshtasticSendError lives inside the Meshtastic adapter
        and signals transient failures (e.g. queue full).  The adapter boundary
        (``MeshtasticAdapter.deliver()``) catches MeshtasticSendError and wraps
        it into an ``AdapterSendError(transient=True)`` so that the core retry
        classifier sees the standard contract type.
        """
        from medre.core.contracts.adapter import AdapterSendError

        raw_err = MeshtasticSendError("queue is full", transient=True)
        # MeshtasticSendError is adapter-internal — NOT a core AdapterSendError.
        assert not isinstance(raw_err, AdapterSendError)
        # Its transient flag is still True (adapter-internal signal).
        assert raw_err.transient is True

        # At the adapter boundary, the error is wrapped into AdapterSendError.
        boundary_err = AdapterSendError("queue is full", transient=True)
        assert isinstance(boundary_err, AdapterSendError)
        assert boundary_err.transient is True
        kind = RetryExecutor.classify_failure(boundary_err)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_meshtastic_queue_rejected_via_adapter_send_error(self) -> None:
        """AdapterSendError(transient=True) from queue rejection classifies correctly."""
        from medre.core.contracts.adapter import AdapterSendError

        err = AdapterSendError("queue rejected: capacity exceeded", transient=True)
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        assert kind.is_retryable is True


class TestQueueLifecycleFailureKindDetail:
    """failure_kind_detail patterns for Meshtastic queue lifecycle transitions."""

    def test_queue_drain_cancelled_detail(self) -> None:
        """'queue drain cancelled' derives meshtastic_queue_drain_cancelled."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        detail = _derive_failure_kind_detail(
            failure_kind="adapter_transient",
            error="queue drain cancelled during shutdown",
        )
        assert detail == "meshtastic_queue_drain_cancelled"

    def test_queue_abandoned_detail(self) -> None:
        """'queue abandoned' derives meshtastic_queue_drain_cancelled."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        detail = _derive_failure_kind_detail(
            failure_kind="adapter_transient",
            error="queue abandoned on crash",
        )
        assert detail == "meshtastic_queue_drain_cancelled"

    def test_queue_rejected_not_confused_with_drain_cancelled(self) -> None:
        """Queue full rejection is NOT meshtastic_queue_drain_cancelled."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        detail = _derive_failure_kind_detail(
            failure_kind="adapter_transient",
            error="queue is full; enqueue rejected",
        )
        assert detail == "meshtastic_queue_rejected"

    def test_delivery_status_distinguishes_enqueued(self) -> None:
        """AdapterDeliveryResult.delivery_status field distinguishes
        enqueued from sent."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        enqueued = AdapterDeliveryResult(
            native_message_id=None,
            delivery_status="enqueued",
            delivery_note="locally enqueued",
        )
        sent = AdapterDeliveryResult(
            native_message_id="123",
            delivery_status="sent",
        )
        assert enqueued.delivery_status == "enqueued"
        assert sent.delivery_status == "sent"
        assert enqueued.native_message_id is None
        assert sent.native_message_id == "123"

    def test_default_delivery_status_is_sent(self) -> None:
        """AdapterDeliveryResult.delivery_status defaults to 'sent'."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        result = AdapterDeliveryResult(native_message_id="42")
        assert result.delivery_status == "sent"


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
        )
        await temp_storage.append_receipt(rcpt_ch0)
        await temp_storage.append_receipt(rcpt_ch1)

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
        )
        await runner._append_queued_to_sent_receipt(record=record_ch0, now=now)

        # Callback for channel "1".
        record_ch1 = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id="1",
            native_message_id="packet-1",
            delivery_plan_id="plan-ch1",
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

        # No channel but only one candidate → OK.
        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="packet-single",
            delivery_plan_id="plan-only",
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

        event_id = "evt-retry"
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts on the same channel (retry scenario).
        # Same plan_id = retry lineage.
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

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-retry",
            delivery_plan_id="plan-retry",
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
# delivery_plan_id propagation through Meshtastic queue (Tranche 5)
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
    - everything else → keep in send_meta top-level
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

    async def test_other_key_stays_in_send_meta(self) -> None:
        """Non-transport, non-meshtastic keys stay at top level of send_meta.
        Payload text is transport context and lands in the meshtastic namespace."""
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
            # Non-transport keys stay at top level
            assert ref.metadata["source_bridge"] == "matrix"
            assert ref.metadata["seq"] == 7
            # Payload text lands in meshtastic namespace (transport context).
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
            # Other key at top level
            assert ref.metadata["custom"] == "value"
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
