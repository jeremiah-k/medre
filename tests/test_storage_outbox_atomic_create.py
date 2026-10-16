"""Tests for delivery_outbox atomic create semantics: idempotent key handling,
terminal immutability, no-steal guarantees for in_progress/queued rows,
preservation of pending work and retry schedules."""

from __future__ import annotations

import asyncio
import uuid

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item

# ===================================================================
# Atomic create_outbox_item
# ===================================================================


class TestAtomicCreateOutboxItem:
    """Verify idempotent and terminal-immutability semantics of
    create_outbox_item."""

    async def test_concurrent_create_same_active_key_returns_one_row(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Two concurrent creates with identical non-terminal key return the same row."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-1",
            target_channel="ch-atomic",
        )
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-atomic-1",
            target_adapter="fake_presentation",
            target_channel="ch-atomic",
            attempt_number=1,
        )
        created1, created2 = await asyncio.gather(
            outbox_temp_storage.create_outbox_item(item1),
            outbox_temp_storage.create_outbox_item(item2),
        )

        # Both returns point to the same row
        assert created1.outbox_id == created2.outbox_id

        # Only one row in the table for this key
        all_items = await outbox_temp_storage.list_outbox_items()
        matching = [
            i
            for i in all_items
            if i.delivery_plan_id == "plan-atomic-1" and i.target_channel == "ch-atomic"
        ]
        assert len(matching) == 1

    async def test_terminal_existing_row_returned_unchanged(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """If the existing row is terminal, re-creating with the same key
        returns the existing terminal row unchanged.  Terminal rows are
        immutable for lifecycle purposes."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-term-rtn",
            target_channel="ch-term",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)
        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="w1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        await outbox_temp_storage.mark_outbox_sent(
            created1.outbox_id, receipt_id="rcpt-1"
        )
        # Item is now terminal.  Re-create with same key tuple.
        item2 = _make_outbox_item(
            delivery_plan_id="plan-term-rtn",
            target_channel="ch-term",
        )
        created2 = await outbox_temp_storage.create_outbox_item(item2)
        # Must return the existing terminal row, not insert a new one.
        assert created2.outbox_id == created1.outbox_id
        # Status remains terminal (sent).
        assert created2.status == "sent"

    async def test_non_terminal_existing_row_returned_unchanged(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating with different metadata but same non-terminal key
        returns the original row unchanged."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-unchanged",
            target_channel="ch-unchanged",
        )
        item1.metadata = {"original": True}
        created1 = await outbox_temp_storage.create_outbox_item(item1)

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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        # Returns the first row
        assert created2.outbox_id == created1.outbox_id
        # Original metadata preserved
        assert created2.metadata == {"original": True}

    async def test_idempotent_create_same_delivery_plan(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """All fields match when creating the same delivery plan twice."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-atomic-idem",
            target_channel="ch-idem",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-atomic-idem",
            target_adapter="fake_presentation",
            target_channel="ch-idem",
            attempt_number=1,
        )
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == created1.outbox_id
        assert created2.event_id == created1.event_id
        assert created2.route_id == created1.route_id
        assert created2.delivery_plan_id == created1.delivery_plan_id
        assert created2.target_adapter == created1.target_adapter
        assert created2.target_channel == created1.target_channel
        assert created2.attempt_number == created1.attempt_number
        assert created2.status == created1.status

    async def test_idempotent_create_preserves_pending_row(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Creation preserves pending work for the recovery claim path."""
        # First create: pending (default status, no worker/lease).
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim",
            target_channel="ch-reclaim",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)
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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        # Same row (idempotent on key tuple).
        assert created2.outbox_id == created1.outbox_id
        assert created2 == created1


# ===================================================================
# create_outbox_item must not steal active work
# ===================================================================


class TestCreateOutboxNoSteal:
    """create_outbox_item must not overwrite in_progress or queued rows.
    All existing work is returned unchanged, including pending and retry_wait."""

    async def test_create_does_not_steal_in_progress(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """An existing in_progress row must be returned unchanged when
        create_outbox_item is called with the same key tuple."""
        # Create and claim to in_progress
        item1 = _make_outbox_item(
            delivery_plan_id="plan-nosteal-ip",
            target_channel="ch-nosteal-ip",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)
        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-original",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) == 1
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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        # Should return the existing row, not the new one
        assert created2.outbox_id == original_oid
        assert created2.status == "in_progress"
        # Worker ID should NOT have been changed to pipeline:new
        assert created2.worker_id == "worker-original"

    async def test_create_does_not_steal_queued(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """An existing queued row must be returned unchanged when
        create_outbox_item is called with the same key tuple."""
        # Create, claim, then queue
        item1 = _make_outbox_item(
            delivery_plan_id="plan-nosteal-q",
            target_channel="ch-nosteal-q",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)
        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) == 1
        await outbox_temp_storage.mark_outbox_queued(created1.outbox_id)
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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        # Should return the existing queued row unchanged
        assert created2.outbox_id == original_oid
        assert created2.status == "queued"

    async def test_create_preserves_pending(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating a pending row preserves its state and owner."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim-pending",
            target_channel="ch-reclaim-p",
        )
        created1 = await outbox_temp_storage.create_outbox_item(item1)
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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        # Preserve every field until an explicit recovery claim.
        assert created2.outbox_id == created1.outbox_id
        assert created2 == created1

    async def test_create_preserves_retry_wait_schedule(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Re-creating scheduled work preserves the backoff and attempt identity."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-reclaim-rw",
            target_channel="ch-reclaim-rw",
        )
        await outbox_temp_storage.create_outbox_item(item1)
        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id
        await outbox_temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )

        before = await outbox_temp_storage.get_outbox_item(oid)

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
        created2 = await outbox_temp_storage.create_outbox_item(item2)

        assert created2 == before
