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

        async def fake_send(item):
            return {"packet_id": "42"}

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=fake_send)

        assert result is not None
        assert q.total_sent == 1
        assert q.total_enqueued == 1
        assert q.total_dequeued == 1

    async def test_process_one_increments_failed_on_error(self) -> None:
        q = MeshtasticOutboundQueue()

        async def failing_send(item):
            raise RuntimeError("send failed")

        await q.enqueue({"text": "hello"}, channel_index=0)
        with pytest.raises(RuntimeError, match="send failed"):
            await q.process_one(send_fn=failing_send)

        assert q.total_failed == 1
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
