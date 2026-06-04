"""Tests for delivery_outbox atomic create semantics: idempotent key handling,
terminal replacement, no-steal guarantees for in_progress/queued rows."""

from __future__ import annotations

import uuid

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage


def _make_outbox_item(
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    attempt_number: int = 1,
    status: str = "pending",
    next_attempt_at: str | None = None,
) -> DeliveryOutboxItem:
    """Build a minimal DeliveryOutboxItem for tests."""
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id="evt-1",
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


# ===================================================================
# Atomic create_outbox_item
# ===================================================================


class TestAtomicCreateOutboxItem:
    """Verify idempotent and terminal-replacement semantics of
    create_outbox_item."""

    async def test_concurrent_create_same_active_key_returns_one_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Two creates with identical non-terminal key return the same row."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-1",
            target_channel="ch-atomic",
        )
        created1 = await temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-atomic-1",
            target_adapter="fake_presentation",
            target_channel="ch-atomic",
            attempt_number=1,
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Both returns point to the same row
        assert created1.outbox_id == created2.outbox_id

        # Only one row in the table for this key
        all_items = await temp_storage.list_outbox_items()
        matching = [
            i
            for i in all_items
            if i.delivery_plan_id == "plan-atomic-1" and i.target_channel == "ch-atomic"
        ]
        assert len(matching) == 1

    async def test_terminal_replacement_does_not_lose_rows(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After terminal, re-create with same key produces a new row."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-term",
            target_channel="ch-term",
        )
        created1 = await temp_storage.create_outbox_item(item1)

        # Transition to terminal via claim then sent
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) >= 1
        await temp_storage.mark_outbox_sent(created1.outbox_id, receipt_id="rcpt-1")

        # Re-create with same key tuple but new outbox_id
        item2 = _make_outbox_item(
            delivery_plan_id="plan-atomic-term",
            target_channel="ch-term",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # New row was created
        assert created2.outbox_id == item2.outbox_id
        assert created2.status == "pending"

    async def test_non_terminal_existing_row_returned_unchanged(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating with different metadata but same non-terminal key
        returns the original row unchanged."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-unchanged",
            target_channel="ch-unchanged",
        )
        item1.metadata = {"original": True}
        created1 = await temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-atomic-unchanged",
            target_adapter="fake_presentation",
            target_channel="ch-unchanged",
            attempt_number=1,
        )
        item2.metadata = {"modified": True}
        created2 = await temp_storage.create_outbox_item(item2)

        # Returns the first row
        assert created2.outbox_id == created1.outbox_id
        # Original metadata preserved
        assert created2.metadata == {"original": True}

    async def test_idempotent_create_same_delivery_plan(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """All fields match when creating the same delivery plan twice."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-idem",
            target_channel="ch-idem",
        )
        created1 = await temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-atomic-idem",
            target_adapter="fake_presentation",
            target_channel="ch-idem",
            attempt_number=1,
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == created1.outbox_id
        assert created2.event_id == created1.event_id
        assert created2.route_id == created1.route_id
        assert created2.delivery_plan_id == created1.delivery_plan_id
        assert created2.target_adapter == created1.target_adapter
        assert created2.target_channel == created1.target_channel
        assert created2.attempt_number == created1.attempt_number
        assert created2.status == created1.status

    async def test_idempotent_create_reclaims_pending_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating with status=in_progress should reclaim a pending row.

        The pipeline creates outbox items with status=in_progress.  If an
        existing pending row is found by the idempotent key, create_outbox_item
        must reclaim it — updating status, worker_id, locked_at, and
        lease_until — so that finalize can transition in_progress → sent.
        """
        # First create: pending (default status, no worker/lease).
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim",
            target_channel="ch-reclaim",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        assert created1.status == "pending"
        assert created1.worker_id is None

        # Second create: same key but in_progress with worker/lease.
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-reclaim",
            target_adapter="fake_presentation",
            target_channel="ch-reclaim",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:abc123",
            locked_at="2026-01-01T00:00:00",
            lease_until="2026-01-01T00:01:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Same row (idempotent on key tuple).
        assert created2.outbox_id == created1.outbox_id
        # But reclaimed with new status/worker/lease.
        assert created2.status == "in_progress"
        assert created2.worker_id == "pipeline:abc123"
        assert created2.locked_at == "2026-01-01T00:00:00"
        assert created2.lease_until == "2026-01-01T00:01:00"


# ===================================================================
# create_outbox_item must not steal active work
# ===================================================================


class TestCreateOutboxNoSteal:
    """create_outbox_item must not overwrite in_progress or queued rows.
    It may only reclaim pending/retry_wait rows and replace terminal rows."""

    async def test_create_does_not_steal_in_progress(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An existing in_progress row must be returned unchanged when
        create_outbox_item is called with the same key tuple."""
        # Create and claim to in_progress
        item1 = _make_outbox_item(
            delivery_plan_id="plan-nosteal-ip",
            target_channel="ch-nosteal-ip",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-original",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) >= 1
        original_oid = created1.outbox_id

        # Try to create a new item with same key tuple but different outbox_id
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-nosteal-ip",
            target_adapter="fake_presentation",
            target_channel="ch-nosteal-ip",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:new",
            locked_at="2026-01-01T00:00:00",
            lease_until="2026-01-01T00:05:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Should return the existing row, not the new one
        assert created2.outbox_id == original_oid
        assert created2.status == "in_progress"
        # Worker ID should NOT have been changed to pipeline:new
        assert created2.worker_id == "worker-original"

    async def test_create_does_not_steal_queued(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An existing queued row must be returned unchanged when
        create_outbox_item is called with the same key tuple."""
        # Create, claim, then queue
        item1 = _make_outbox_item(
            delivery_plan_id="plan-nosteal-q",
            target_channel="ch-nosteal-q",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) >= 1
        await temp_storage.mark_outbox_queued(created1.outbox_id)
        original_oid = created1.outbox_id

        # Try to create a new item with same key tuple
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-nosteal-q",
            target_adapter="fake_presentation",
            target_channel="ch-nosteal-q",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:new",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Should return the existing queued row unchanged
        assert created2.outbox_id == original_oid
        assert created2.status == "queued"

    async def test_create_still_reclaims_pending(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating with same key tuple on a pending row still reclaims."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim-pending",
            target_channel="ch-reclaim-p",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        assert created1.status == "pending"

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-reclaim-pending",
            target_adapter="fake_presentation",
            target_channel="ch-reclaim-p",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:abc",
            locked_at="2026-01-01T00:00:00",
            lease_until="2026-01-01T00:05:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Reclaimed — same outbox_id, new status/worker
        assert created2.outbox_id == created1.outbox_id
        assert created2.status == "in_progress"
        assert created2.worker_id == "pipeline:abc"

    async def test_create_still_reclaims_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating with same key tuple on a retry_wait row still reclaims."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim-rw",
            target_channel="ch-reclaim-rw",
        )
        await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) >= 1
        oid = claimed[0].outbox_id
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )

        # Create new item with same key
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-reclaim-rw",
            target_adapter="fake_presentation",
            target_channel="ch-reclaim-rw",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:reclaim",
            locked_at="2026-01-01T00:30:00",
            lease_until="2026-01-01T00:35:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Reclaimed
        assert created2.outbox_id == oid
        assert created2.status == "in_progress"
        assert created2.worker_id == "pipeline:reclaim"
        assert (
            created2.next_attempt_at is None
        ), "Reclaiming a retry_wait row must clear next_attempt_at"
