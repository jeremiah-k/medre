"""Tests for outbox-strict field validations in append_queued_to_sent_receipt.

Validates that adapter, delivery_plan_id, native_channel_id, and
attempt_number on the callback record are checked against the authoritative
outbox item before a supplemental sent receipt is created.  Also exercises
exact outbox_id-based receipt selection and idempotency.
"""

from __future__ import annotations

from datetime import datetime, timezone

from msgspec.structs import replace

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_lifecycle, _make_receipt


def _make_queued_receipt_with_outbox(
    outbox_id: str,
    receipt_id: str = "rcpt-q",
    adapter: str = "mesh-1",
    channel: str = "0",
    plan_id: str = "plan-q",
    attempt_number: int = 1,
    event_id: str = "evt-001",
) -> "DeliveryReceipt":
    """Create a queued receipt with outbox_id set."""
    base = _make_receipt(
        receipt_id=receipt_id,
        status="queued",
        adapter=adapter,
        channel=channel,
        plan_id=plan_id,
        attempt_number=attempt_number,
        event_id=event_id,
    )
    return replace(base, outbox_id=outbox_id)


def _make_outbox_item(
    outbox_id: str = "obox-001",
    event_id: str = "evt-001",
    route_id: str = "route-001",
    plan_id: str = "plan-q",
    target_adapter: str = "mesh-1",
    target_channel: str = "0",
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Create a standard outbox item for testing."""
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        status="in_progress",
        attempt_number=attempt_number,
    )


async def _setup_outbox_and_receipt(
    storage: StorageBackend,
    outbox_id: str = "obox-001",
    receipt_id: str = "rcpt-q",
    adapter: str = "mesh-1",
    channel: str = "0",
    plan_id: str = "plan-q",
    attempt_number: int = 1,
    event_id: str = "evt-001",
) -> None:
    """Create outbox item + queued receipt with matching outbox_id."""
    outbox = _make_outbox_item(
        outbox_id=outbox_id,
        event_id=event_id,
        plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        attempt_number=attempt_number,
    )
    await storage.create_outbox_item(outbox)
    await storage.mark_outbox_queued(outbox_id)

    queued = _make_queued_receipt_with_outbox(
        outbox_id=outbox_id,
        receipt_id=receipt_id,
        adapter=adapter,
        channel=channel,
        plan_id=plan_id,
        attempt_number=attempt_number,
        event_id=event_id,
    )
    await storage.append_receipt(queued)


# ===================================================================
# Field mismatch rejection tests
# ===================================================================


class TestFieldMismatchRejected:
    """Verify that mismatched outbox fields prevent supplemental receipts."""

    async def test_wrong_delivery_plan_id_rejected(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """outbox_id correct but record.delivery_plan_id differs
        → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-plan-mismatch",
            receipt_id="rcpt-plan-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-correct",
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-plan-bad",
            delivery_plan_id="plan-wrong",
            outbox_id="obox-plan-mismatch",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_wrong_native_channel_id_rejected(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """outbox_id correct but record.native_channel_id differs
        → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-ch-mismatch",
            receipt_id="rcpt-ch-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-ch",
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="wrong-channel",
            native_message_id="pkt-ch-bad",
            delivery_plan_id="plan-ch",
            outbox_id="obox-ch-mismatch",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_wrong_adapter_rejected(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """outbox_id correct but record.adapter differs
        → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-adapter-mismatch",
            receipt_id="rcpt-adapter-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-adapt",
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="wrong-adapter",
            native_channel_id="0",
            native_message_id="pkt-adapter-bad",
            delivery_plan_id="plan-adapt",
            outbox_id="obox-adapter-mismatch",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_wrong_attempt_number_rejected(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """outbox_id correct but record.attempt_number differs
        → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-attempt-mismatch",
            receipt_id="rcpt-attempt-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-attempt",
            attempt_number=2,
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-attempt-bad",
            delivery_plan_id="plan-attempt",
            outbox_id="obox-attempt-mismatch",
            attempt_number=5,
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0


# ===================================================================
# Exact outbox_id selection
# ===================================================================


class TestExactOutboxSelection:
    """Verify receipt selection uses outbox_id, not plan+channel."""

    async def test_different_outbox_id_not_selected(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Two queued receipts with same plan/channel but different
        outbox_id → only matching outbox_id selected."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # First outbox + receipt (attempt 1).
        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-first",
            receipt_id="rcpt-first",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-shared",
            attempt_number=1,
        )

        # Second outbox + receipt, same plan/channel, different outbox_id
        # (attempt 2 — retry).  Uses different attempt_number to satisfy
        # the outbox UNIQUE(plan_id, adapter, channel, attempt) constraint.
        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-second",
            receipt_id="rcpt-second",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-shared",
            attempt_number=2,
        )

        # Callback targets obox-second → should only match rcpt-second.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-second",
            delivery_plan_id="plan-shared",
            outbox_id="obox-second",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-second"
        assert sent[0].adapter_message_id == "pkt-second"

        # First receipt should remain queued (no supplemental sent).
        first_queued = [
            r
            for r in all_receipts
            if r.receipt_id == "rcpt-first" and r.status == "queued"
        ]
        assert len(first_queued) == 1


# ===================================================================
# Happy path and idempotency
# ===================================================================


class TestExactCallbackHappyPath:
    """Verify correct callback produces supplemental sent receipt."""

    async def test_valid_exact_callback_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Correct outbox_id, adapter, plan, channel, attempt
        → supplemental sent receipt created."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-valid",
            receipt_id="rcpt-valid-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-valid",
            attempt_number=1,
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-valid-42",
            delivery_plan_id="plan-valid",
            outbox_id="obox-valid",
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
        assert sent[0].parent_receipt_id == "rcpt-valid-q"
        assert sent[0].adapter_message_id == "pkt-valid-42"
        assert sent[0].delivery_plan_id == "plan-valid"
        assert sent[0].outbox_id == "obox-valid"

        # Outbox should be transitioned to sent.
        updated_outbox = await temp_storage.get_outbox_item("obox-valid")
        assert updated_outbox is not None
        assert updated_outbox.status == "sent"

    async def test_duplicate_exact_callback_no_duplicate(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Call twice with same outbox_id → only one supplemental sent
        receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await _setup_outbox_and_receipt(
            temp_storage,
            outbox_id="obox-dup",
            receipt_id="rcpt-dup-q",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-dup",
            attempt_number=1,
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-dup",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup",
            attempt_number=1,
        )

        # First call — should create supplemental sent receipt.
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Second call — the original queued receipt is gone (replaced by
        # sent), so the candidate filter `r.status == "queued"` won't
        # match it.  A second supplemental receipt should NOT be created.
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-dup-q"
