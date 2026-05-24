"""Tests for Meshtastic outbound queue bounded send-failure retry semantics.

Verifies:
- Success sent: process_one returns result, increments total_sent.
- Transient requeue: MeshtasticSendError(transient=True) front-requeues
  when attempts remain; increments total_requeued.
- Transient eventually succeeds: after N-1 transient failures, the Nth
  attempt succeeds; total_sent incremented, total_requeued correct.
- Transient exhaustion: after max_attempts transient failures the item
  is dropped; total_exhausted and total_failed incremented.
- Permanent no requeue: MeshtasticSendError(transient=False) drops
  immediately; total_permanent_failed and total_failed incremented.
- Unknown exception bounded retry: plain Exception treated as transient
  with bounded retry; exhausted after max_attempts.
- CancelledError re-raises: asyncio.CancelledError is re-raised; item
  attempt counter is NOT bumped.  The item has already been dequeued
  so shutdown-time cancellation can abandon the in-flight item; this
  is not durable delivery.
- Front requeue order: failed item goes to front before newer items.
- queue_health fields: all new counters present with correct values.
- Adapter diagnostics fields: queue_-prefixed new counters present.
- Queue-full still rejects/no eviction: retry does not bypass capacity.
- Diagnostics no message body/secrets: health dict contains no payloads.
- Config validation: queue_send_max_attempts validated for bool/non-int/<=0.
- max_attempts constructor validation.
"""

from __future__ import annotations

import asyncio

import pytest

from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

# ===================================================================
# Helpers
# ===================================================================


def _item_payloads_in_queue(q: MeshtasticOutboundQueue) -> list[str]:
    """Extract payload text from all items in queue order."""
    return [item["payload"]["text"] for item in q._queue]


# ===================================================================
# Success path
# ===================================================================


class TestSuccessSent:
    """process_one returns result and increments total_sent."""

    async def test_success_returns_result(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def fake_send(item):
            return {"packet_id": "99"}

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=fake_send)

        assert result is not None
        assert result.delivery_result.native_message_id == "99"
        assert q.total_sent == 1
        assert q.total_failed == 0
        assert q.total_requeued == 0
        assert q.total_exhausted == 0

    async def test_success_attempt_counter_is_1(self) -> None:
        """First successful send has _attempt=1 on the result item."""
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def fake_send(item):
            return {"packet_id": "1"}

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=fake_send)

        assert result is not None
        assert result.item["_attempt"] == 1


# ===================================================================
# Transient requeue
# ===================================================================


class TestTransientRequeue:
    """MeshtasticSendError(transient=True) front-requeues when
    attempts remain."""

    async def test_transient_failure_requeues(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def fail_once(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=fail_once)

        # Item is requeued, not returned.
        assert result is None
        assert q.total_requeued == 1
        assert q.total_failed == 0
        assert q.total_exhausted == 0
        assert q.pending_count == 1

    async def test_transient_requeue_increments_attempt(self) -> None:
        """After requeue, the item's _attempt is incremented."""
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def always_fail(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "hello"}, channel_index=0)
        await q.process_one(send_fn=always_fail)

        # Dequeue manually to inspect _attempt
        item = await q.dequeue()
        assert item is not None
        assert item["_attempt"] == 2  # was 1, incremented before requeue


# ===================================================================
# Transient eventually succeeds
# ===================================================================


class TestTransientEventuallySucceeds:
    """After N-1 transient failures, the Nth attempt succeeds."""

    async def test_second_attempt_succeeds(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)
        attempt = 0

        async def fail_then_succeed(item):
            nonlocal attempt
            attempt += 1
            if attempt < 2:
                raise MeshtasticSendError("radio busy", transient=True)
            return {"packet_id": "42"}

        await q.enqueue({"text": "hello"}, channel_index=0)

        # First try: transient failure → requeue.
        result1 = await q.process_one(send_fn=fail_then_succeed)
        assert result1 is None
        assert q.total_requeued == 1

        # Second try: success.
        result2 = await q.process_one(send_fn=fail_then_succeed)
        assert result2 is not None
        assert result2.item["_attempt"] == 2
        assert q.total_sent == 1
        assert q.pending_count == 0

    async def test_third_attempt_succeeds(self) -> None:
        """Item succeeds on the last allowed attempt (attempt 3 of 3)."""
        q = MeshtasticOutboundQueue(max_attempts=3)
        attempt = 0

        async def fail_twice_then_succeed(item):
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise MeshtasticSendError("radio busy", transient=True)
            return {"packet_id": "77"}

        await q.enqueue({"text": "hello"}, channel_index=0)

        # Attempt 1: transient → requeue.
        await q.process_one(send_fn=fail_twice_then_succeed)
        # Attempt 2: transient → requeue.
        await q.process_one(send_fn=fail_twice_then_succeed)
        # Attempt 3: success.
        result = await q.process_one(send_fn=fail_twice_then_succeed)
        assert result is not None
        assert result.item["_attempt"] == 3
        assert q.total_sent == 1
        assert q.total_requeued == 2


# ===================================================================
# Transient exhaustion
# ===================================================================


class TestTransientExhaustion:
    """After max_attempts transient failures, item is dropped."""

    async def test_exhausted_after_max_attempts(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=2)

        async def always_transient(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "hello"}, channel_index=0)

        # Attempt 1: transient → requeue.
        result1 = await q.process_one(send_fn=always_transient)
        assert result1 is None
        assert q.total_requeued == 1
        assert q.pending_count == 1

        # Attempt 2: exhausted → dropped.
        result2 = await q.process_one(send_fn=always_transient)
        assert result2 is None
        assert q.total_exhausted == 1
        assert q.total_failed == 1
        assert q.pending_count == 0

    async def test_exhausted_with_max_attempts_1(self) -> None:
        """max_attempts=1 means no retries; first transient exhausts."""
        q = MeshtasticOutboundQueue(max_attempts=1)

        async def always_transient(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=always_transient)

        assert result is None
        assert q.total_exhausted == 1
        assert q.total_failed == 1
        assert q.total_requeued == 0
        assert q.pending_count == 0


# ===================================================================
# Permanent no requeue
# ===================================================================


class TestPermanentNoRequeue:
    """MeshtasticSendError(transient=False) drops immediately."""

    async def test_permanent_failure_drops_immediately(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def permanent_fail(item):
            raise MeshtasticSendError("payload too large", transient=False)

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=permanent_fail)

        assert result is None
        assert q.total_permanent_failed == 1
        assert q.total_failed == 1
        assert q.total_requeued == 0
        assert q.total_exhausted == 0
        assert q.pending_count == 0

    async def test_permanent_ignores_remaining_attempts(self) -> None:
        """Even with max_attempts=10, permanent failure drops at first try."""
        q = MeshtasticOutboundQueue(max_attempts=10)

        async def permanent_fail(item):
            raise MeshtasticSendError("invalid payload", transient=False)

        await q.enqueue({"text": "hello"}, channel_index=0)
        await q.process_one(send_fn=permanent_fail)

        assert q.total_permanent_failed == 1
        assert q.total_failed == 1
        assert q.pending_count == 0


# ===================================================================
# Unknown exception bounded retry
# ===================================================================


class TestUnknownExceptionBoundedRetry:
    """Plain Exception is treated as transient with bounded retry."""

    async def test_unknown_exception_requeues(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def raise_runtime(item):
            raise RuntimeError("unexpected")

        await q.enqueue({"text": "hello"}, channel_index=0)
        result = await q.process_one(send_fn=raise_runtime)

        assert result is None
        assert q.total_requeued == 1
        assert q.total_failed == 0
        assert q.pending_count == 1

    async def test_unknown_exception_exhausts(self) -> None:
        """After max_attempts unknown errors, item is dropped."""
        q = MeshtasticOutboundQueue(max_attempts=2)

        async def always_runtime(item):
            raise RuntimeError("unexpected")

        await q.enqueue({"text": "hello"}, channel_index=0)

        # Attempt 1: requeue.
        await q.process_one(send_fn=always_runtime)
        assert q.total_requeued == 1

        # Attempt 2: exhausted.
        await q.process_one(send_fn=always_runtime)
        assert q.total_exhausted == 1
        assert q.total_failed == 1
        assert q.pending_count == 0


# ===================================================================
# CancelledError re-raises
# ===================================================================


class TestCancelledErrorReRaises:
    """asyncio.CancelledError is re-raised without requeue or drop."""

    async def test_cancelled_error_reraises(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def raise_cancelled(item):
            raise asyncio.CancelledError()

        await q.enqueue({"text": "hello"}, channel_index=0)

        with pytest.raises(asyncio.CancelledError):
            await q.process_one(send_fn=raise_cancelled)

        # Item should NOT be requeued or dropped.
        assert q.total_requeued == 0
        assert q.total_failed == 0
        assert q.total_exhausted == 0
        assert q.total_permanent_failed == 0
        # Item was already dequeued; CancelledError does not requeue it.
        assert q.pending_count == 0


# ===================================================================
# Front requeue order
# ===================================================================


class TestFrontRequeueOrder:
    """Failed item goes to front before newer items."""

    async def test_failed_item_goes_to_front(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)

        fail_count = 0

        async def fail_once_then_succeed(item):
            nonlocal fail_count
            fail_count += 1
            if fail_count == 1:
                raise MeshtasticSendError("radio busy", transient=True)
            return {"packet_id": "1"}

        # Enqueue two items: first will fail, second is waiting.
        await q.enqueue({"text": "first"}, channel_index=0)
        await q.enqueue({"text": "second"}, channel_index=0)

        # Process first item → transient failure → front-requeue.
        result1 = await q.process_one(send_fn=fail_once_then_succeed)
        assert result1 is None

        # "first" should now be at the front (ahead of "second").
        assert _item_payloads_in_queue(q) == ["first", "second"]

    async def test_front_requeue_preserves_urgency(self) -> None:
        """Front requeue ensures the failed item is retried before
        any items that were enqueued later."""
        q = MeshtasticOutboundQueue(max_attempts=3)

        async def always_fail(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "A"}, channel_index=0)
        await q.enqueue({"text": "B"}, channel_index=0)
        await q.enqueue({"text": "C"}, channel_index=0)

        # Process A → transient → front-requeue.
        await q.process_one(send_fn=always_fail)
        # Order: [A, B, C] (A requeued to front)
        assert _item_payloads_in_queue(q) == ["A", "B", "C"]

        # Process A again → front-requeue (attempt 2).
        await q.process_one(send_fn=always_fail)
        assert _item_payloads_in_queue(q) == ["A", "B", "C"]


# ===================================================================
# Queue health fields
# ===================================================================


class TestQueueHealthFields:
    """queue_health includes all new counters and max_attempts."""

    async def test_queue_health_has_all_fields(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=10, max_attempts=3)
        health = q.queue_health

        expected_keys = {
            "pending_count",
            "total_sent",
            "total_failed",
            "total_enqueued",
            "total_dequeued",
            "total_rejected",
            "total_requeued",
            "total_exhausted",
            "total_permanent_failed",
            "max_queue_size",
            "max_attempts",
            "utilization_pct",
            "delay_between_messages",
            "last_send_time",
        }
        assert expected_keys == set(health.keys())

    async def test_queue_health_max_attempts_value(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=5)
        assert q.queue_health["max_attempts"] == 5

    async def test_queue_health_counters_after_retry(self) -> None:
        """Counters reflect state after a transient retry cycle."""
        q = MeshtasticOutboundQueue(max_attempts=2)
        attempt = 0

        async def fail_once(item):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise MeshtasticSendError("radio busy", transient=True)
            return {"packet_id": "1"}

        await q.enqueue({"text": "hello"}, channel_index=0)

        # Attempt 1: transient → requeue.
        await q.process_one(send_fn=fail_once)
        # Attempt 2: success.
        await q.process_one(send_fn=fail_once)

        health = q.queue_health
        assert health["total_sent"] == 1
        assert health["total_requeued"] == 1
        assert health["total_exhausted"] == 0
        assert health["total_permanent_failed"] == 0
        assert health["total_failed"] == 0

    async def test_queue_health_after_exhaustion(self) -> None:
        """Counters reflect state after item exhaustion."""
        q = MeshtasticOutboundQueue(max_attempts=2)

        async def always_fail(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "hello"}, channel_index=0)

        # Attempt 1: requeue.
        await q.process_one(send_fn=always_fail)
        # Attempt 2: exhausted.
        await q.process_one(send_fn=always_fail)

        health = q.queue_health
        assert health["total_exhausted"] == 1
        assert health["total_failed"] == 1
        assert health["total_requeued"] == 1


# ===================================================================
# Adapter diagnostics fields
# ===================================================================


class TestAdapterDiagnosticsFields:
    """Adapter diagnostics include new queue_-prefixed counters."""

    async def test_diagnostics_has_new_queue_fields(self) -> None:
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(
            adapter_id="diag-retry",
            connection_type="fake",
            queue_send_max_attempts=5,
        )
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="diag-retry",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert "queue_total_requeued" in diag
            assert "queue_total_exhausted" in diag
            assert "queue_total_permanent_failed" in diag
            assert "queue_send_max_attempts" in diag
            assert diag["queue_send_max_attempts"] == 5
            assert diag["queue_total_requeued"] == 0
            assert diag["queue_total_exhausted"] == 0
            assert diag["queue_total_permanent_failed"] == 0
        finally:
            await adapter.stop()

    async def test_diagnostics_reflects_queue_retry_state(self) -> None:
        """Adapter diagnostics reflect queue retry counters after activity."""
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(
            adapter_id="diag-active",
            connection_type="fake",
            queue_send_max_attempts=2,
        )
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="diag-active",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            # Directly exercise the queue with a transient failure.
            q = adapter._queue

            async def transient_fail(item):
                raise MeshtasticSendError("radio busy", transient=True)

            await q.enqueue({"text": "test"}, channel_index=0)
            # Attempt 1: requeue.
            await q.process_one(send_fn=transient_fail)
            # Attempt 2: exhausted.
            await q.process_one(send_fn=transient_fail)

            diag = adapter.diagnostics()
            assert diag["queue_total_requeued"] == 1
            assert diag["queue_total_exhausted"] == 1
            assert diag["queue_total_failed"] == 1
        finally:
            await adapter.stop()


# ===================================================================
# Queue-full still rejects / no eviction
# ===================================================================


class TestQueueFullStillRejects:
    """Retry does not bypass capacity; queue-full still rejects."""

    async def test_full_queue_rejects_even_with_retry_items(self) -> None:
        """A requeued item occupies a slot; new enqueues are still rejected."""
        q = MeshtasticOutboundQueue(max_queue_size=1, max_attempts=3)

        async def transient_fail(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "first"}, channel_index=0)
        assert q.pending_count == 1

        # Process → transient failure → front-requeue (still occupies slot).
        await q.process_one(send_fn=transient_fail)
        assert q.pending_count == 1

        # New enqueue should be rejected; existing item not evicted.
        with pytest.raises(MeshtasticSendError, match="queue is full") as exc_info:
            await q.enqueue({"text": "second"}, channel_index=0)
        assert exc_info.value.transient is True
        assert q.total_rejected == 1

    async def test_no_eviction_on_requeue(self) -> None:
        """Front-requeue does not evict other items."""
        q = MeshtasticOutboundQueue(max_queue_size=2, max_attempts=3)

        async def transient_fail(item):
            raise MeshtasticSendError("radio busy", transient=True)

        await q.enqueue({"text": "A"}, channel_index=0)
        await q.enqueue({"text": "B"}, channel_index=0)

        # Process A → transient → front-requeue.
        await q.process_one(send_fn=transient_fail)
        # Both A and B should still be present.
        assert q.pending_count == 2
        assert _item_payloads_in_queue(q) == ["A", "B"]


# ===================================================================
# Diagnostics no message body / secrets
# ===================================================================


class TestDiagnosticsNoMessageBodyOrSecrets:
    """queue_health does not expose payload text or secrets."""

    async def test_queue_health_no_payload_text(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)
        await q.enqueue({"text": "secret message"}, channel_index=0)

        health = q.queue_health
        health_str = str(health)
        assert "secret message" not in health_str

    async def test_queue_health_no_event_id(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=3)
        await q.enqueue({"text": "hello"}, channel_index=0, event_id="sensitive-evt-id")

        health = q.queue_health
        health_str = str(health)
        assert "sensitive-evt-id" not in health_str

    async def test_adapter_diagnostics_no_payload(self) -> None:
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.events.bus import EventBus

        config = MeshtasticConfig(adapter_id="diag-safe", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="diag-safe",
            event_bus=EventBus(),
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            # Enqueue a payload with potentially sensitive text.
            await adapter._queue.enqueue({"text": "secret radio text"}, channel_index=0)
            diag = adapter.diagnostics()
            diag_str = str(diag)
            assert "secret radio text" not in diag_str
        finally:
            await adapter.stop()


# ===================================================================
# Config validation for queue_send_max_attempts
# ===================================================================


class TestConfigQueueSendMaxAttemptsValidation:
    """queue_send_max_attempts validation: default, positive int, bool,
    non-int, <=0."""

    def test_default_value_is_3(self) -> None:
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test")
        assert config.queue_send_max_attempts == 3

    def test_positive_int_is_valid(self) -> None:
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test", queue_send_max_attempts=5)
        assert config.validate().queue_send_max_attempts == 5

    def test_bool_raises(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="test", queue_send_max_attempts=True  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_false_bool_raises(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="test", queue_send_max_attempts=False  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_non_int_raises(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="test", queue_send_max_attempts="3"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_zero_raises(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test", queue_send_max_attempts=0)
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_negative_raises(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test", queue_send_max_attempts=-1)
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()


# ===================================================================
# max_attempts constructor validation
# ===================================================================


class TestMaxAttemptsConstructorValidation:
    """MeshtasticOutboundQueue max_attempts validation."""

    def test_positive_int_is_valid(self) -> None:
        q = MeshtasticOutboundQueue(max_attempts=5)
        assert q.max_attempts == 5

    def test_bool_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must not be a bool"):
            MeshtasticOutboundQueue(max_attempts=True)  # type: ignore[arg-type]

    def test_non_int_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be an int"):
            MeshtasticOutboundQueue(max_attempts="3")  # type: ignore[arg-type]

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be > 0"):
            MeshtasticOutboundQueue(max_attempts=0)

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be > 0"):
            MeshtasticOutboundQueue(max_attempts=-1)


# ===================================================================
# Queue module docstring update
# ===================================================================


class TestQueueModuleDocstring:
    """Module docstring reflects bounded retry semantics."""

    def test_docstring_mentions_requeue(self) -> None:
        import medre.adapters.meshtastic.queue as queue_mod

        doc = queue_mod.__doc__ or ""
        assert "requeue" in doc.lower() or "front-requeue" in doc.lower()

    def test_docstring_no_permanent_drop_claim(self) -> None:
        """Module docstring no longer claims items are permanently dropped."""
        import medre.adapters.meshtastic.queue as queue_mod

        doc = queue_mod.__doc__ or ""
        assert "permanently dropped" not in doc

    def test_docstring_mentions_local_only(self) -> None:
        """Docstring clarifies local acceptance semantics."""
        import medre.adapters.meshtastic.queue as queue_mod

        doc = queue_mod.__doc__ or ""
        assert "local" in doc.lower() or "local-only" in doc.lower()


# ===================================================================
# Attempt metadata
# ===================================================================


class TestAttemptMetadata:
    """Queue item _attempt metadata: first send = attempt 1."""

    async def test_initial_attempt_is_1(self) -> None:
        """Enqueued item starts with _attempt=1."""
        q = MeshtasticOutboundQueue(max_attempts=3)
        await q.enqueue({"text": "hello"}, channel_index=0)

        item = await q.dequeue()
        assert item is not None
        assert item["_attempt"] == 1

    async def test_attempt_not_in_payload(self) -> None:
        """_attempt is internal metadata, not part of the radio payload."""
        q = MeshtasticOutboundQueue(max_attempts=3)
        await q.enqueue({"text": "hello"}, channel_index=0)

        item = await q.dequeue()
        assert item is not None
        # _attempt is a top-level key on the item, not nested in payload.
        assert "_attempt" not in item["payload"]
        assert "_attempt" in item
