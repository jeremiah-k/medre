"""Tests for queue terminal outcome reporting and outbox_id correlation.

Exercises:
1. Stale callback rejection (terminal outbox statuses).
2. Terminal outcome reporting (exhausted retry budget).
3. Exact outbox_id correlation (queued -> sent transition).
4. Duplicate callback idempotent (no-op on second call).
5. Cancellation reporting (cancelled in-flight + abandoned remaining).
6. Correlation IDs not leaked into rendered payload.

Uses real MeshtasticOutboundQueue and SQLiteStorage instances — no mocks
for queue/storage behaviour (only send_fn where noted).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import (
    MeshtasticOutboundQueue,
    QueueDeliveryResult,
    QueueTerminalResult,
)
from medre.core.contracts.adapter import (
    AdapterContext,
    OutboundNativeRefRecord,
    QueueTerminalRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from medre.core.storage.sqlite.storage import SQLiteStorage

# ===================================================================
# Local helpers (replicated from lifecycle conftest — file must be
# self-contained per task requirements)
# ===================================================================


def _make_lifecycle() -> DeliveryLifecycleService:
    return DeliveryLifecycleService(
        logger=logging.getLogger("test.delivery_lifecycle"),
    )


def _make_receipt(
    receipt_id: str = "rcpt-001",
    status: str = "queued",
    attempt_number: int = 1,
    event_id: str = "evt-001",
    adapter: str = "test_adapter",
    channel: str | None = None,
    plan_id: str = "plan-001",
    route_id: str = "route-001",
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
    source: str = "live",
    replay_run_id: str | None = None,
    outbox_id: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status=status,
        error=None,
        failure_kind=failure_kind,
        created_at=datetime.now(tz=timezone.utc),
        attempt_number=attempt_number,
        next_retry_at=next_retry_at,
        source=source,
        replay_run_id=replay_run_id,
        outbox_id=outbox_id,
    )


def _make_adapter_ctx(
    adapter_id: str = "mesh-test",
    record_outbound_terminal: Any = None,
) -> AdapterContext:
    """Build a real AdapterContext for testing the adapter's queue methods."""
    _events: list[Any] = []

    async def _publish_inbound(event: Any) -> None:
        _events.append(event)

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish_inbound,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        record_outbound_terminal=record_outbound_terminal,
    )


@pytest.fixture
async def temp_storage() -> Any:
    """SQLiteStorage backed by a temporary file, cleaned up after test."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    storage = SQLiteStorage(db_path=db_path)
    try:
        await storage.initialize()
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(db_path)
        raise
    try:
        yield storage
    finally:
        await storage.close()
        with suppress(FileNotFoundError):
            os.unlink(db_path)


# ===================================================================
# 1. Stale callback rejection
# ===================================================================


class TestStaleCallbackRejection:
    """Verify that callbacks arriving for terminal-status outbox items
    are rejected (no supplemental receipt created, warning logged)."""

    @pytest.mark.asyncio
    async def test_dead_lettered_outbox_rejects_callback(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox_id with status=dead_lettered -> callback rejected."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-dl",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-dl",
            )
        )

        # Create and transition outbox to dead_lettered.
        # NOTE: mark_outbox_dead_lettered only allows in_progress/retry_wait,
        # so we skip mark_outbox_queued and go directly in_progress -> dead_lettered.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-dl",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-dl",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_dead_lettered(
            "obox-dl",
            failure_kind="adapter_transient",
            error_summary="exhausted",
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-dl-stale",
            delivery_plan_id="plan-dl",
            outbox_id="obox-dl",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        # No supplemental sent receipt.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

        # Stale callback warning logged.
        assert "Stale callback rejected" in caplog.text
        assert "dead_lettered" in caplog.text

    @pytest.mark.asyncio
    async def test_sent_outbox_rejects_callback(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox_id with status=sent -> callback rejected."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-sent",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-sent",
            )
        )

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-sent",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-sent",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-sent")
        await temp_storage.mark_outbox_sent("obox-sent")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-sent-stale",
            delivery_plan_id="plan-sent",
            outbox_id="obox-sent",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert "Stale callback rejected" in caplog.text

    @pytest.mark.asyncio
    async def test_cancelled_outbox_rejects_callback(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox_id with status=cancelled -> callback rejected."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-cancel",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-cancel",
            )
        )

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-cancel",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-cancel",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-cancel")
        await temp_storage.mark_outbox_cancelled("obox-cancel")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-cancel-stale",
            delivery_plan_id="plan-cancel",
            outbox_id="obox-cancel",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert "Stale callback rejected" in caplog.text

    @pytest.mark.asyncio
    async def test_abandoned_outbox_rejects_callback(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox_id with status=abandoned -> callback rejected."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-abandon",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-abandon",
            )
        )

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-abandon",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-abandon",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-abandon")
        await temp_storage.mark_outbox_abandoned("obox-abandon")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-abandon-stale",
            delivery_plan_id="plan-abandon",
            outbox_id="obox-abandon",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert "Stale callback rejected" in caplog.text


class TestStaleCallbackAfterRetryReclaim:
    """Verify that a stale callback from an old retry attempt is rejected
    while a newly retried attempt (different outbox_id) succeeds.

    Scenario:
    1. Attempt A (outbox_id=obox-A) is queued, then expires/reclaimed by retry.
    2. Retry reissues attempt B (outbox_id=obox-B) which is now queued.
    3. Old callback A arrives late → rejected because obox-A is no longer
       queued/in_progress.
    4. Callback B arrives → succeeds, transitions obox-B to sent.
    """

    @pytest.mark.asyncio
    async def test_old_callback_rejected_new_callback_succeeds(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # --- Attempt A: original delivery ---
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-attempt-a",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-retry-a",
                event_id="evt-retry",
            )
        )
        outbox_a = DeliveryOutboxItem(
            outbox_id="obox-a",
            event_id="evt-retry",
            route_id="route-001",
            delivery_plan_id="plan-retry-a",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_a)
        # Simulate retry reclaim: obox-a transitions from in_progress to
        # retry_wait.  In production the retry worker claims the stale
        # queued item; here we go directly from creation status.
        await temp_storage.mark_outbox_retry_wait(
            "obox-a",
            next_attempt_at="2099-01-01T00:00:00+00:00",
            failure_kind="adapter_transient",
            error_summary="stale queued reclaim",
        )

        # --- Attempt B: retry delivery (new outbox item, different plan_id
        # to avoid the UNIQUE constraint on
        # (delivery_plan_id, target_adapter, target_channel, attempt_number)) ---
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-attempt-b",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-retry-b",
                event_id="evt-retry",
                outbox_id="obox-b",
            )
        )
        outbox_b = DeliveryOutboxItem(
            outbox_id="obox-b",
            event_id="evt-retry",
            route_id="route-001",
            delivery_plan_id="plan-retry-b",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_b)
        await temp_storage.mark_outbox_queued("obox-b")

        # --- Stale callback A arrives late ---
        stale_record = OutboundNativeRefRecord(
            event_id="evt-retry",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-stale-a",
            delivery_plan_id="plan-retry-a",
            outbox_id="obox-a",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=stale_record,
                now=now,
            )

        # Stale callback rejected: obox-a is retry_wait, not queued/in_progress.
        stale_receipts = await temp_storage.list_receipts_for_event("evt-retry")
        stale_sent = [r for r in stale_receipts if r.status == "sent"]
        assert len(stale_sent) == 0
        assert "Stale callback rejected" in caplog.text

        # --- Fresh callback B arrives ---
        fresh_record = OutboundNativeRefRecord(
            event_id="evt-retry",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-fresh-b",
            delivery_plan_id="plan-retry-b",
            outbox_id="obox-b",
            attempt_number=1,
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=fresh_record,
            now=now,
        )

        # Fresh callback succeeds: obox-b was queued.
        final_receipts = await temp_storage.list_receipts_for_event("evt-retry")
        final_sent = [r for r in final_receipts if r.status == "sent"]
        assert len(final_sent) == 1
        assert final_sent[0].adapter_message_id == "pkt-fresh-b"

        # Verify obox-b transitioned to sent.
        obox_b = await temp_storage.get_outbox_item("obox-b")
        assert obox_b is not None
        assert obox_b.status == "sent"

        # Verify obox-a remains in retry_wait (not corrupted by stale callback).
        obox_a = await temp_storage.get_outbox_item("obox-a")
        assert obox_a is not None
        assert obox_a.status == "retry_wait"


# ===================================================================
# 2. Terminal outcome reporting (exhausted)
# ===================================================================


class TestTerminalOutcomeExhausted:
    """Verify MeshtasticOutboundQueue.process_one returns
    QueueTerminalResult(outcome="exhausted") when retry budget is
    exhausted, and the adapter's _report_queue_terminal constructs the
    correct QueueTerminalRecord."""

    @pytest.mark.asyncio
    async def test_exhausted_after_max_transient_retries(self) -> None:
        """Transient errors up to max_attempts produce exhausted result."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
            max_attempts=2,
        )
        await queue.enqueue(
            payload={"text": "hello"},
            channel_index=0,
            event_id="evt-exhaust",
            delivery_plan_id="plan-ex",
            outbox_id="obox-ex",
            attempt_number=1,
        )

        # First attempt: transient -> front-requeue.
        send_fn = AsyncMock(
            side_effect=MeshtasticSendError("radio busy", transient=True),
        )
        result1 = await queue.process_one(send_fn=send_fn)
        # Requeued (None), not terminal yet.
        assert result1 is None
        assert queue.queue_depth == 1

        # Second attempt: transient -> exhausted (max_attempts=2).
        send_fn2 = AsyncMock(
            side_effect=MeshtasticSendError("radio busy again", transient=True),
        )
        result2 = await queue.process_one(send_fn=send_fn2)
        assert isinstance(result2, QueueTerminalResult)
        assert result2.outcome == "exhausted"
        assert result2.item["event_id"] == "evt-exhaust"
        assert result2.item["outbox_id"] == "obox-ex"
        assert result2.item["delivery_plan_id"] == "plan-ex"
        assert queue.queue_depth == 0

    @pytest.mark.asyncio
    async def test_report_queue_terminal_constructs_correct_record(self) -> None:
        """_report_queue_terminal builds QueueTerminalRecord with correct fields.

        Uses a real MeshtasticAdapter in fake mode to exercise the actual
        _report_queue_terminal method.  The record_outbound_terminal callback
        captures the record for assertion.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="mesh-test",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)

        captured_records: list[QueueTerminalRecord] = []

        async def _on_terminal(rec: QueueTerminalRecord) -> None:
            captured_records.append(rec)

        ctx = _make_adapter_ctx(
            adapter_id="mesh-test",
            record_outbound_terminal=_on_terminal,
        )
        await adapter.start(ctx)
        try:
            # Build a terminal result with all fields populated.
            terminal = QueueTerminalResult(
                item={
                    "payload": {"text": "test"},
                    "channel_index": 3,
                    "event_id": "evt-term",
                    "delivery_plan_id": "plan-term",
                    "outbox_id": "obox-term",
                    "attempt_number": 2,
                    "_attempt": 3,
                },
                outcome="exhausted",
                error="radio timeout after 3 attempts",
            )
            await adapter._report_queue_terminal(terminal)

            assert len(captured_records) == 1
            rec = captured_records[0]
            assert rec.event_id == "evt-term"
            assert rec.adapter == "mesh-test"
            assert rec.outbox_id == "obox-term"
            assert rec.delivery_plan_id == "plan-term"
            assert rec.attempt_number == 2
            assert rec.native_channel_id == "3"
            assert rec.outcome == "exhausted"
            assert rec.error == "radio timeout after 3 attempts"
        finally:
            await adapter.stop(timeout=1.0)

    @pytest.mark.asyncio
    async def test_permanent_failure_terminal_record(self) -> None:
        """Permanent failure returns QueueTerminalResult(outcome=permanent_failed)."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
            max_attempts=3,
        )
        await queue.enqueue(
            payload={"text": "permanent"},
            channel_index=0,
            event_id="evt-perm",
            outbox_id="obox-perm",
        )

        send_fn = AsyncMock(
            side_effect=MeshtasticSendError("invalid payload", transient=False),
        )
        result = await queue.process_one(send_fn=send_fn)
        assert isinstance(result, QueueTerminalResult)
        assert result.outcome == "permanent_failed"
        assert result.item["event_id"] == "evt-perm"
        assert result.item["outbox_id"] == "obox-perm"
        assert "invalid payload" in (result.error or "")


# ===================================================================
# 3. Exact outbox_id correlation
# ===================================================================


class TestExactOutboxIdCorrelation:
    """Verify append_queued_to_sent_receipt correlates exactly via outbox_id
    and transitions the outbox from queued to sent."""

    @pytest.mark.asyncio
    async def test_outbox_id_exact_match_transitions_to_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """outbox_id on record matches queued outbox item -> sent transition."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-correlate",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-exact",
                outbox_id="obox-exact",
            )
        )

        # Create and transition outbox item to "queued".
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-exact",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-exact",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-exact")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-exact-42",
            delivery_plan_id="plan-exact",
            outbox_id="obox-exact",
            attempt_number=1,
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Outbox should now be sent.
        updated = await temp_storage.get_outbox_item("obox-exact")
        assert updated is not None
        assert updated.status == "sent"

        # Supplemental sent receipt created with correct fields.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "pkt-exact-42"
        assert sent[0].parent_receipt_id == "rcpt-correlate"

    @pytest.mark.asyncio
    async def test_outbox_id_no_heuristic_fallback_used(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """When outbox_id is provided, only exact outbox match is used —
        no heuristic fallback based on delivery_plan_id alone."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts with same adapter/channel but different plans.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-plan-a",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-a",
                outbox_id="obox-plan-a",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-plan-b",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-b",
            )
        )

        # Create outbox item for plan-a only.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-plan-a",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-a",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-plan-a")

        # Record targets obox-plan-a explicitly — must NOT match plan-b.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-plan-a-only",
            delivery_plan_id="plan-a",
            outbox_id="obox-plan-a",
            attempt_number=1,
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-plan-a"
        # plan-b receipt untouched.
        plan_b_sent = [
            r
            for r in all_receipts
            if r.delivery_plan_id == "plan-b" and r.status == "sent"
        ]
        assert len(plan_b_sent) == 0

    @pytest.mark.asyncio
    async def test_outbox_id_not_found_logs_warning(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox_id on record but no matching outbox item -> warning logged."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-missing",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-missing",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-missing",
            delivery_plan_id="plan-missing",
            outbox_id="obox-nonexistent",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert "Stale callback" in caplog.text
        assert "obox-nonexistent" in caplog.text


# ===================================================================
# 4. Duplicate callback idempotent
# ===================================================================


class TestDuplicateCallbackIdempotent:
    """Verify that a second append_queued_to_sent_receipt with the same
    outbox_id does not create a duplicate receipt or crash."""

    @pytest.mark.asyncio
    async def test_second_callback_no_duplicate_receipt(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """First call transitions outbox queued->sent; second call is
        rejected as stale (outbox already in 'sent' status)."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-dup",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-dup",
                outbox_id="obox-dup",
            )
        )

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-dup",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-dup",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-dup")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-dup-1",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup",
            attempt_number=1,
        )

        # First call: should succeed.
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Verify outbox is now sent.
        updated = await temp_storage.get_outbox_item("obox-dup")
        assert updated is not None
        assert updated.status == "sent"

        # Second call with same outbox_id: should be a no-op (stale).
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        # Still only 1 sent receipt.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "pkt-dup-1"

        # Stale-callback protection kicked in on second call.
        assert "Stale callback rejected" in caplog.text

    @pytest.mark.asyncio
    async def test_second_callback_no_crash_different_native_id(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Second callback with same outbox_id but different
        native_message_id — also rejected as stale, no crash."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-dup2",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-dup2",
                event_id="evt-002",
                outbox_id="obox-dup2",
            )
        )

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-dup2",
            event_id="evt-002",
            route_id="route-001",
            delivery_plan_id="plan-dup2",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-dup2")

        record1 = OutboundNativeRefRecord(
            event_id="evt-002",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-first",
            delivery_plan_id="plan-dup2",
            outbox_id="obox-dup2",
            attempt_number=1,
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record1,
            now=now,
        )

        # Second callback with different native_message_id.
        record2 = OutboundNativeRefRecord(
            event_id="evt-002",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-second",
            delivery_plan_id="plan-dup2",
            outbox_id="obox-dup2",
            attempt_number=1,
        )
        # Must not raise.
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record2,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-002")
        sent = [r for r in all_receipts if r.status == "sent"]
        # Only the first one succeeded.
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "pkt-first"


# ===================================================================
# 5. Cancellation reporting
# ===================================================================


class TestCancellationReporting:
    """Verify that CancelledError during process_one stores the in-flight
    item via pop_cancelled_item(), and remaining items are drained and
    reported as abandoned."""

    @pytest.mark.asyncio
    async def test_cancelled_item_stored_via_pop(self) -> None:
        """CancelledError during send_fn stores item for pop_cancelled_item."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
            max_attempts=3,
        )
        await queue.enqueue(
            payload={"text": "inflight"},
            channel_index=1,
            event_id="evt-cancel",
            outbox_id="obox-cancel",
            attempt_number=1,
        )

        async def _cancel_send(item: dict[str, Any]) -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await queue.process_one(send_fn=_cancel_send)

        cancelled = queue.pop_cancelled_item()
        assert cancelled is not None
        assert cancelled["event_id"] == "evt-cancel"
        assert cancelled["outbox_id"] == "obox-cancel"
        assert cancelled["payload"]["text"] == "inflight"

        # pop_cancelled_item clears the reference.
        assert queue.pop_cancelled_item() is None

    @pytest.mark.asyncio
    async def test_remaining_items_drained_as_abandoned(self) -> None:
        """drain_all() returns remaining items; they are reported as abandoned."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
        )
        await queue.enqueue(
            payload={"text": "item-1"},
            channel_index=0,
            event_id="evt-ab1",
            outbox_id="obox-ab1",
        )
        await queue.enqueue(
            payload={"text": "item-2"},
            channel_index=0,
            event_id="evt-ab2",
            outbox_id="obox-ab2",
        )

        remaining = queue.drain_all()
        assert len(remaining) == 2
        assert remaining[0]["event_id"] == "evt-ab1"
        assert remaining[1]["event_id"] == "evt-ab2"

        # Queue should be empty now.
        assert queue.queue_depth == 0

    @pytest.mark.asyncio
    async def test_report_cancelled_and_drain_produces_correct_records(self) -> None:
        """_report_cancelled_and_drain reports cancelled (in-flight) and
        abandoned (remaining) via record_outbound_terminal callback."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="mesh-cancel",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)

        captured: list[QueueTerminalRecord] = []

        async def _on_terminal(rec: QueueTerminalRecord) -> None:
            captured.append(rec)

        ctx = _make_adapter_ctx(
            adapter_id="mesh-cancel",
            record_outbound_terminal=_on_terminal,
        )
        await adapter.start(ctx)
        try:
            # Enqueue two items into the adapter's queue.
            await adapter._queue.enqueue(
                payload={"text": "remaining-1"},
                channel_index=0,
                event_id="evt-rem1",
                outbox_id="obox-rem1",
                attempt_number=1,
            )
            await adapter._queue.enqueue(
                payload={"text": "remaining-2"},
                channel_index=0,
                event_id="evt-rem2",
                outbox_id="obox-rem2",
                attempt_number=1,
            )

            # Simulate a cancelled in-flight item by directly setting
            # the internal _last_cancelled_item.
            adapter._queue._last_cancelled_item = {
                "payload": {"text": "inflight"},
                "channel_index": 0,
                "event_id": "evt-inflight",
                "outbox_id": "obox-inflight",
                "attempt_number": 2,
            }

            await adapter._report_cancelled_and_drain()

            # Should have 3 records: 1 cancelled + 2 abandoned.
            assert len(captured) == 3

            cancelled_recs = [r for r in captured if r.outcome == "cancelled"]
            abandoned_recs = [r for r in captured if r.outcome == "abandoned"]

            assert len(cancelled_recs) == 1
            assert cancelled_recs[0].event_id == "evt-inflight"
            assert cancelled_recs[0].outbox_id == "obox-inflight"
            assert cancelled_recs[0].adapter == "mesh-cancel"

            assert len(abandoned_recs) == 2
            abandoned_ids = {r.event_id for r in abandoned_recs}
            assert abandoned_ids == {"evt-rem1", "evt-rem2"}
        finally:
            await adapter.stop(timeout=1.0)


# ===================================================================
# 6. Correlation ID not in rendered payload
# ===================================================================


class TestCorrelationIdNotInPayload:
    """Verify that outbox_id and attempt_number are NOT present in the
    rendered payload dict that gets sent to the radio. They should only
    exist in the queue item metadata, never in item['payload']."""

    @pytest.mark.asyncio
    async def test_enqueue_separates_payload_from_metadata(self) -> None:
        """Enqueue stores outbox_id and attempt_number outside payload."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
        )

        payload = {"text": "hello world", "channel_index": 1}
        await queue.enqueue(
            payload=payload,
            channel_index=1,
            event_id="evt-payload",
            delivery_plan_id="plan-p",
            outbox_id="obox-p",
            attempt_number=3,
        )

        # Dequeue to inspect the internal item structure.
        item = await queue.dequeue()
        assert item is not None

        # Payload must not contain correlation IDs.
        assert "outbox_id" not in item["payload"]
        assert "attempt_number" not in item["payload"]
        assert "event_id" not in item["payload"]
        assert "delivery_plan_id" not in item["payload"]

        # Payload contains only the original content.
        assert item["payload"]["text"] == "hello world"

        # Metadata is stored at the item level.
        assert item["outbox_id"] == "obox-p"
        assert item["attempt_number"] == 3
        assert item["event_id"] == "evt-payload"
        assert item["delivery_plan_id"] == "plan-p"

    @pytest.mark.asyncio
    async def test_process_one_preserves_payload_purity(self) -> None:
        """After process_one, the delivered item's payload still has no
        correlation IDs."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
        )

        await queue.enqueue(
            payload={"text": "radio message"},
            channel_index=2,
            event_id="evt-pure",
            delivery_plan_id="plan-pure",
            outbox_id="obox-pure",
            attempt_number=1,
        )

        async def _fake_send(item: dict[str, Any]) -> dict[str, Any]:
            # Verify the payload has no correlation IDs at send time.
            assert "outbox_id" not in item["payload"]
            assert "attempt_number" not in item["payload"]
            assert "event_id" not in item["payload"]
            return {"id": "pkt-pure-99"}

        result = await queue.process_one(send_fn=_fake_send)
        assert isinstance(result, QueueDeliveryResult)
        assert result.delivery_result.native_message_id == "pkt-pure-99"

        # Item metadata is accessible on the result.
        assert result.item["outbox_id"] == "obox-pure"
        assert result.item["event_id"] == "evt-pure"
        assert result.item["payload"]["text"] == "radio message"

    @pytest.mark.asyncio
    async def test_terminal_result_preserves_payload_purity(self) -> None:
        """QueueTerminalResult.item payload has no correlation IDs."""
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0,
            max_attempts=1,
        )

        await queue.enqueue(
            payload={"text": "will-fail"},
            channel_index=0,
            event_id="evt-fail",
            outbox_id="obox-fail",
            attempt_number=1,
        )

        async def _fail_send(item: dict[str, Any]) -> None:
            raise MeshtasticSendError("permanent", transient=False)

        result = await queue.process_one(send_fn=_fail_send)
        assert isinstance(result, QueueTerminalResult)
        assert result.outcome == "permanent_failed"

        # Payload is clean.
        assert "outbox_id" not in result.item["payload"]
        assert "attempt_number" not in result.item["payload"]

        # Metadata is accessible at item level.
        assert result.item["outbox_id"] == "obox-fail"
        assert result.item["event_id"] == "evt-fail"
