"""Focused unit tests for DeliveryLifecycleService.

Exercises ``medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService``
directly with small local fakes, verifying lifecycle decision semantics
for retry classification, dead-letter progression, attempt context,
next_retry_at computation, supplemental receipt generation, suppression
receipt creation, outbox finalization, and terminal-state determination.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

import pytest

from medre.core.contracts.adapter import (
    AdapterPermanentError,
    AdapterSendError,
    OutboundNativeRefRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.routing.models import RouteTarget
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle() -> DeliveryLifecycleService:
    return DeliveryLifecycleService(
        logger=logging.getLogger("test.delivery_lifecycle"),
    )


def _make_plan(
    plan_id: str = "plan-001",
    adapter_id: str = "test_adapter",
    retry_policy: RetryPolicy | None = None,
) -> DeliveryPlan:
    target = RouteTarget(adapter=adapter_id, channel=None)
    return DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=retry_policy,
    )


def _make_receipt(
    receipt_id: str = "rcpt-001",
    status: Literal[
        "queued", "sent", "failed", "dead_lettered", "suppressed"
    ] = "failed",
    attempt_number: int = 1,
    event_id: str = "evt-001",
    adapter: str = "test_adapter",
    channel: str | None = None,
    plan_id: str = "plan-001",
    route_id: str = "route-001",
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
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
    )


# ===================================================================
# Attempt context computation
# ===================================================================


class TestComputeAttemptContext:
    """Verify attempt_number and parent_receipt_id from previous_receipt."""

    def test_first_attempt(self) -> None:
        """No previous receipt → attempt=1, parent=None."""
        lifecycle = _make_lifecycle()
        attempt, parent = lifecycle.compute_attempt_context(None)
        assert attempt == 1
        assert parent is None

    def test_retry_attempt_increments(self) -> None:
        """Previous receipt attempt=2 → attempt=3, parent=previous id."""
        lifecycle = _make_lifecycle()
        prev = _make_receipt(receipt_id="rcpt-prev", attempt_number=2)
        attempt, parent = lifecycle.compute_attempt_context(prev)
        assert attempt == 3
        assert parent == "rcpt-prev"

    def test_attempt_number_one_from_first_receipt(self) -> None:
        """Previous receipt attempt=1 → attempt=2."""
        lifecycle = _make_lifecycle()
        prev = _make_receipt(receipt_id="rcpt-first", attempt_number=1)
        attempt, parent = lifecycle.compute_attempt_context(prev)
        assert attempt == 2
        assert parent == "rcpt-first"


# ===================================================================
# Retry field extraction
# ===================================================================


class TestExtractRetryFields:
    """Verify retry policy field extraction from delivery plan."""

    def test_no_retry_policy(self) -> None:
        """Plan without retry policy → all None."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        fields = lifecycle.extract_retry_fields(plan)
        assert fields["retry_max_attempts"] is None
        assert fields["retry_backoff_base"] is None
        assert fields["retry_max_delay"] is None
        assert fields["retry_jitter"] is None

    def test_with_retry_policy(self) -> None:
        """Plan with retry policy → fields populated."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(
            max_attempts=5,
            backoff_base=3.0,
            max_delay_seconds=120.0,
            jitter=False,
        )
        plan = _make_plan(retry_policy=policy)
        fields = lifecycle.extract_retry_fields(plan)
        assert fields["retry_max_attempts"] == 5
        assert fields["retry_backoff_base"] == 3.0
        assert fields["retry_max_delay"] == 120.0
        assert fields["retry_jitter"] is False


# ===================================================================
# Failure classification
# ===================================================================


class TestClassifyFailure:
    """Verify RetryExecutor.classify_failure passthrough."""

    def test_transient_error(self) -> None:
        """AdapterSendError(transient=True) → ADAPTER_TRANSIENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            AdapterSendError("timeout", transient=True),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_permanent_error(self) -> None:
        """AdapterPermanentError → ADAPTER_PERMANENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            AdapterPermanentError("malformed"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT

    def test_connection_error_transient(self) -> None:
        """ConnectionError → ADAPTER_TRANSIENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            ConnectionError("refused"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_generic_runtime_error_permanent(self) -> None:
        """Generic RuntimeError → ADAPTER_PERMANENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            RuntimeError("unknown"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT


# ===================================================================
# Retryable / permanent classification
# ===================================================================


class TestIsRetryable:
    """Verify is_retryable delegates to DeliveryFailureKind."""

    def test_transient_is_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.ADAPTER_TRANSIENT) is True

    def test_permanent_not_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.ADAPTER_PERMANENT) is False

    def test_renderer_failure_not_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.RENDERER_FAILURE) is False


# ===================================================================
# Dead-letter determination
# ===================================================================


class TestShouldDeadLetter:
    """Verify dead-letter transition logic."""

    def test_failed_exhausted_policy(self) -> None:
        """Failed + exhausted policy → dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 1) is True

    def test_failed_with_retries_remaining(self) -> None:
        """Failed + retries remaining → no dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 1) is False

    def test_sent_no_dead_letter(self) -> None:
        """Sent status → no dead-letter regardless of policy."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("sent", plan, 1) is False

    def test_failed_no_policy(self) -> None:
        """Failed + no retry policy → no dead-letter."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        assert lifecycle.should_dead_letter("failed", plan, 1) is False

    def test_failed_exhausted_at_max(self) -> None:
        """Failed at max_attempts → dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 3) is True


# ===================================================================
# Next retry time computation
# ===================================================================


class TestComputeNextRetryAt:
    """Verify next_retry_at calculation for retryable transient failures."""

    def test_retryable_transient_returns_time(self) -> None:
        """Transient failure with policy and retries remaining → next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is not None
        assert result > now

    def test_permanent_failure_no_retry_time(self) -> None:
        """Permanent failure -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_PERMANENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_exhausted_no_retry_time(self) -> None:
        """Exhausted retry -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_no_policy_no_retry_time(self) -> None:
        """No retry policy -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_sent_no_retry_time(self) -> None:
        """Sent status → no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "sent",
            None,
            plan,
            1,
            now,
        )
        assert result is None

    def test_backoff_increases_with_attempts(self) -> None:
        """Higher attempt numbers produce later next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=5, backoff_base=1.0, jitter=False)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        r1 = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        r2 = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            2,
            now,
        )
        assert r1 is not None
        assert r2 is not None
        # Attempt 2 backoff (2s) > attempt 1 backoff (1s).
        assert r2 > r1


# ===================================================================
# Dead-letter receipt creation (integration with real storage)
# ===================================================================


class TestBuildAndPersistDeadLetterReceipt:
    """Verify dead-letter receipt construction and persistence."""

    async def test_dead_letter_receipt_persisted(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Dead-letter receipt is appended to storage."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)

        receipt = await lifecycle.build_and_persist_dead_letter_receipt(
            temp_storage,
            event_id="evt-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            previous_receipt_id="rcpt-primary",
            attempt_number=1,
            error="boom",
            source="live",
            replay_run_id=None,
            target_channel=None,
            plan=plan,
        )

        assert receipt.status == "dead_lettered"
        assert receipt.parent_receipt_id == "rcpt-primary"
        assert receipt.attempt_number == 2  # attempt_number + 1
        stored = await temp_storage.list_receipts_for_event("evt-001")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_dead_letter_receipt_with_replay(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Dead-letter receipt preserves replay_run_id and source."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)

        receipt = await lifecycle.build_and_persist_dead_letter_receipt(
            temp_storage,
            event_id="evt-002",
            delivery_plan_id="plan-002",
            target_adapter="mesh",
            previous_receipt_id="rcpt-orig",
            attempt_number=3,
            error="exhausted",
            source="replay",
            replay_run_id="run-42",
            target_channel="ch-0",
            plan=plan,
        )

        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-42"
        assert receipt.target_channel == "ch-0"
        assert receipt.attempt_number == 4


# ===================================================================
# Suppression receipt creation (integration with real storage)
# ===================================================================


class TestBuildAndPersistSuppressionReceipt:
    """Verify suppression receipt construction and persistence."""

    async def test_suppression_receipt_persisted(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Suppression receipt is appended to storage."""
        lifecycle = _make_lifecycle()

        receipt = await lifecycle.build_and_persist_suppression_receipt(
            temp_storage,
            event_id="evt-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            target_channel=None,
            route_id="route-001",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error="loop_prevented",
        )

        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "loop_suppressed"
        assert receipt.attempt_number == 1
        assert receipt.parent_receipt_id is None
        assert receipt.next_retry_at is None
        stored = await temp_storage.list_receipts_for_event("evt-001")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_suppression_receipt_with_replay(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Suppression receipt preserves replay context."""
        lifecycle = _make_lifecycle()

        receipt = await lifecycle.build_and_persist_suppression_receipt(
            temp_storage,
            event_id="evt-002",
            delivery_plan_id="plan-002",
            target_adapter="dest",
            target_channel="ch-1",
            route_id="route-002",
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            error="blocked",
            source="replay",
            replay_run_id="run-99",
        )

        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-99"
        assert receipt.target_channel == "ch-1"


# ===================================================================
# Supplemental queued→sent receipt (integration with real storage)
# ===================================================================


class TestAppendQueuedToSentReceipt:
    """Verify supplemental queued→sent receipt generation."""

    async def test_supplemental_sent_receipt_created(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Callback with matching queued receipt → supplemental sent receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-queued",
            status="queued",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-q",
        )
        await temp_storage.append_receipt(queued)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-42",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Should have 2 receipts now: original queued + supplemental sent.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-queued"
        assert sent[0].adapter_message_id == "packet-42"
        assert sent[0].delivery_plan_id == "plan-q"

    async def test_no_queued_receipt_no_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No queued receipt → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        record = OutboundNativeRefRecord(
            event_id="evt-noexist",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-x",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-noexist")
        assert len(all_receipts) == 0

    async def test_ambiguous_candidates_no_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Multiple queued candidates with no channel → no supplemental."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts on different channels, same adapter.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-a", status="queued", adapter="m", channel="0"
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-b", status="queued", adapter="m", channel="1"
            )
        )

        # Record with no channel → ambiguous.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_single_candidate_no_channel_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """One queued candidate + no channel on record → supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-only", status="queued", adapter="m", channel="0"
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-single",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "pkt-single"

    async def test_retry_chooses_most_recent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Multiple queued receipts on same channel (retries) → last one wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-first",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-retry",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=2,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-retry",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-retry"
        assert sent[0].attempt_number == 2


# ===================================================================
# Outbox finalization (integration with real storage)
# ===================================================================


class TestFinalizeOutboxOutcome:
    """Verify outbox finalization decisions."""

    async def test_no_outbox_skips(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No outbox_id → no action."""
        lifecycle = _make_lifecycle()
        # Should not raise.
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            None,
            False,
            None,
            None,
            None,
            None,
        )

    async def test_sent_receipt_marks_outbox_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Receipt with status='sent' → mark_outbox_sent."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        # Create an outbox item.
        item = DeliveryOutboxItem(
            outbox_id="obox-sent-test",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="sent")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-sent-test",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-sent-test")
        assert updated is not None
        assert updated.status == "sent"

    async def test_queued_receipt_marks_outbox_queued(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Receipt with status='queued' → mark_outbox_queued."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-queued-test",
            event_id="evt-q",
            route_id="route-q",
            delivery_plan_id="plan-q",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="queued", event_id="evt-q")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-queued-test",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-queued-test")
        assert updated is not None
        assert updated.status == "queued"

    async def test_permanent_failure_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Permanent failure → mark_outbox_dead_lettered."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-dl-test",
            event_id="evt-dl",
            route_id="route-dl",
            delivery_plan_id="plan-dl",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="failed", event_id="evt-dl")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-dl-test",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_PERMANENT,
            "malformed",
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-dl-test")
        assert updated is not None
        assert updated.status == "dead_lettered"

    async def test_retryable_failure_marks_retry_wait(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Retryable failure with policy -> mark_outbox_retry_wait."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-rw-test",
            event_id="evt-rw",
            route_id="route-rw",
            delivery_plan_id="plan-rw",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        retry_at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        receipt = _make_receipt(
            status="failed",
            event_id="evt-rw",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=retry_at,
        )
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-rw-test",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-rw-test")
        assert updated is not None
        assert updated.status == "retry_wait"

    async def test_retryable_no_policy_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Retryable failure without policy → dead_lettered (terminal)."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-rw-np",
            event_id="evt-rw-np",
            route_id="route-rw-np",
            delivery_plan_id="plan-rw-np",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="failed", event_id="evt-rw-np")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-rw-np",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            None,  # no retry policy → terminal
        )

        updated = await temp_storage.get_outbox_item("obox-rw-np")
        assert updated is not None
        assert updated.status == "dead_lettered"


# ===================================================================
# Terminal-state determination
# ===================================================================


class TestIsTerminalOutboxStatus:
    """Verify terminal outbox status identification."""

    def test_sent_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("sent") is True

    def test_dead_lettered_is_terminal(self) -> None:
        assert (
            DeliveryLifecycleService.is_terminal_outbox_status("dead_lettered") is True
        )

    def test_cancelled_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("cancelled") is True

    def test_abandoned_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("abandoned") is True

    def test_pending_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("pending") is False

    def test_in_progress_is_not_terminal(self) -> None:
        assert (
            DeliveryLifecycleService.is_terminal_outbox_status("in_progress") is False
        )

    def test_retry_wait_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("retry_wait") is False

    def test_queued_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("queued") is False


# ===================================================================
# PipelineRunner → DeliveryLifecycleService → TargetDeliveryService
# delegation integration test
# ===================================================================


class TestDelegationIntegration:
    """Verify PipelineRunner delegates to DeliveryLifecycleService."""

    async def test_runner_uses_lifecycle_for_suppression(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """PipelineRunner._persist_suppression_receipt delegates to lifecycle."""
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Router

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        receipt = await runner._persist_suppression_receipt(
            event_id="evt-s",
            delivery_plan_id="plan-s",
            target_adapter="dest",
            target_channel=None,
            route_id="route-s",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error="loop_prevented",
        )

        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "loop_suppressed"

        # Verify receipt persisted via lifecycle → storage.
        stored = await temp_storage.list_receipts_for_event("evt-s")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_runner_uses_lifecycle_for_queued_to_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """PipelineRunner._append_queued_to_sent_receipt delegates to lifecycle."""
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Router

        now = datetime.now(tz=timezone.utc)
        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-q",
            status="queued",
            adapter="mesh",
            channel="0",
        )
        await temp_storage.append_receipt(queued)

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh",
            native_channel_id="0",
            native_message_id="pkt-42",
        )
        await runner._append_queued_to_sent_receipt(record=record, now=now)

        stored = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in stored if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-q"
        assert sent[0].adapter_message_id == "pkt-42"


# ===================================================================
# Dead-letter receipt — runtime guard for missing retry_policy
# ===================================================================


class TestDeadLetterReceiptRuntimeGuard:
    """Verify build_and_persist_dead_letter_receipt raises without retry_policy."""

    async def test_raises_runtime_error_without_retry_policy(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Calling with plan.retry_policy=None raises RuntimeError."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)

        with pytest.raises(RuntimeError, match="retry_policy"):
            await lifecycle.build_and_persist_dead_letter_receipt(
                temp_storage,
                event_id="evt-guard",
                delivery_plan_id="plan-guard",
                target_adapter="test_adapter",
                previous_receipt_id="rcpt-prev",
                attempt_number=1,
                error="boom",
                source="live",
                replay_run_id=None,
                target_channel=None,
                plan=plan,
            )

        # No receipt should have been persisted.
        stored = await temp_storage.list_receipts_for_event("evt-guard")
        assert len(stored) == 0


# ===================================================================
# Supplemental queued→sent receipt — outbox transition
# ===================================================================


class TestSupplementalOutboxTransition:
    """Verify supplemental queued→sent receipt also transitions the outbox."""

    async def test_outbox_transitioned_from_queued_to_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Supplemental receipt transitions matching outbox item queued→sent."""
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-outbox-q",
            status="queued",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-outbox",
        )
        await temp_storage.append_receipt(queued)

        # Create a matching outbox item in "queued" status.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-supplemental",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-outbox",
            target_adapter="mesh-1",
            target_channel="0",
            status="queued",
        )
        await temp_storage.create_outbox_item(outbox_item)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-outbox-42",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Outbox should now be sent.
        updated = await temp_storage.get_outbox_item("obox-supplemental")
        assert updated is not None
        assert updated.status == "sent"

        # Supplemental sent receipt should exist.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "packet-outbox-42"


# ===================================================================
# finalize_outbox_outcome — storage error swallowed
# ===================================================================


class TestFinalizeOutboxSwallowsStorageErrors:
    """Verify finalize_outbox_outcome logs and swallows storage exceptions."""

    async def test_storage_error_does_not_propagate(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Exception from storage.mark_outbox_sent is caught and logged."""
        from unittest.mock import AsyncMock

        lifecycle = _make_lifecycle()
        receipt = _make_receipt(status="sent")

        # Patch the storage to raise on mark_outbox_sent.
        temp_storage.mark_outbox_sent = AsyncMock(  # type: ignore[assignment]
            side_effect=RuntimeError("storage is offline")
        )

        # Should NOT raise despite the broken storage method.
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-broken",
            True,
            receipt,
            None,
            None,
            None,
        )

        # Verify the method was actually called.
        temp_storage.mark_outbox_sent.assert_awaited_once()  # type: ignore[attr-defined]


# ===================================================================
# PipelineRunner._finalize_outbox_outcome delegates to lifecycle
# ===================================================================


class TestRunnerFinalizeOutboxDelegation:
    """Verify PipelineRunner._finalize_outbox_outcome delegates to lifecycle."""

    async def test_delegates_to_lifecycle(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Runner._finalize_outbox_outcome calls lifecycle.finalize_outbox_outcome."""
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Router
        from medre.core.storage.backend import DeliveryOutboxItem

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        # Create an outbox item.
        item = DeliveryOutboxItem(
            outbox_id="obox-delegate",
            event_id="evt-delegate",
            route_id="route-d",
            delivery_plan_id="plan-d",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="sent", event_id="evt-delegate")
        await runner._finalize_outbox_outcome(
            "obox-delegate",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-delegate")
        assert updated is not None
        assert updated.status == "sent"


# ===================================================================
# finalize_outbox_outcome — retry timestamp alignment
# ===================================================================


class TestFinalizeOutboxRetryTimestampAlignment:
    """Verify finalize_outbox_outcome aligns outbox retry_wait with receipt."""

    async def test_exhausted_transient_with_policy_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Transient failure, retry policy, next_retry_at=None -> dead_lettered.

        When the receipt has status='failed', failure_kind=adapter_transient,
        a retry policy exists, but next_retry_at is None (exhausted), the
        outbox must be marked dead_lettered, not retry_wait.
        """
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-exhausted",
            event_id="evt-exhausted",
            route_id="route-ex",
            delivery_plan_id="plan-ex",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        # Receipt: failed, transient, next_retry_at=None (exhausted).
        receipt = _make_receipt(
            status="failed",
            event_id="evt-exhausted",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=None,
        )
        policy = RetryPolicy(max_attempts=1, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-exhausted",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-exhausted")
        assert updated is not None
        assert updated.status == "dead_lettered"

    async def test_retry_wait_uses_receipt_next_retry_at(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """retry_wait uses exact receipt.next_retry_at when present.

        When the receipt has a non-None next_retry_at, the outbox
        retry_wait next_attempt_at must match it exactly, not be
        recomputed from backoff.
        """
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-aligned",
            event_id="evt-aligned",
            route_id="route-al",
            delivery_plan_id="plan-al",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        # Craft a receipt with a specific next_retry_at.
        expected_retry_at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        receipt = _make_receipt(
            status="failed",
            event_id="evt-aligned",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=expected_retry_at,
        )
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-aligned",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-aligned")
        assert updated is not None
        assert updated.status == "retry_wait"
        # The outbox next_attempt_at should match receipt.next_retry_at
        # exactly (both are ISO-formatted from the same datetime).
        assert updated.next_attempt_at is not None
        assert updated.next_attempt_at == expected_retry_at.isoformat()
