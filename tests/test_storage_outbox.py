"""Tests for delivery_outbox storage operations: create, list, get, update,
claim, lease, idempotent create, status transitions, count, and persistence.

Also covers triage-confirmed fixes:
- Stale queued reclaim after grace period (fresh queued not claimed).
- create_outbox_item must not steal in_progress or queued rows.
- claim_due_outbox_items clears next_attempt_at.
- Aiosqlite write lock serialisation.
- DeliveryOutboxItem.is_claimable API clarity.
"""

from __future__ import annotations

import uuid

import pytest

from medre.core.storage import DeliveryOutboxItem, SQLiteStorage
from medre.core.storage.sqlite import STALE_QUEUED_GRACE_SECONDS


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
# Create / Get
# ===================================================================


class TestCreateAndGet:
    """create_outbox_item() then get_outbox_item() must return an equivalent item."""

    async def test_create_and_get_round_trip(self, temp_storage: SQLiteStorage) -> None:
        item = _make_outbox_item()
        created = await temp_storage.create_outbox_item(item)
        assert created.outbox_id == item.outbox_id
        assert created.status == "pending"

        retrieved = await temp_storage.get_outbox_item(item.outbox_id)
        assert retrieved is not None
        assert retrieved.outbox_id == item.outbox_id
        assert retrieved.event_id == "evt-1"
        assert retrieved.route_id == "route-1"
        assert retrieved.delivery_plan_id == "plan-1"
        assert retrieved.target_adapter == "fake_presentation"
        assert retrieved.target_channel == "ch-0"
        assert retrieved.attempt_number == 1
        assert retrieved.status == "pending"
        assert retrieved.created_at is not None
        assert retrieved.updated_at is not None

    async def test_get_returns_none_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        result = await temp_storage.get_outbox_item("does-not-exist")
        assert result is None


# ===================================================================
# Idempotent create
# ===================================================================


class TestIdempotentCreate:
    """Creating with the same (delivery_plan_id, target_adapter, target_channel,
    attempt_number) should not create duplicates."""

    async def test_duplicate_key_returns_existing(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item1 = _make_outbox_item(delivery_plan_id="plan-idem", target_channel="ch-1")
        created1 = await temp_storage.create_outbox_item(item1)
        assert created1.outbox_id == item1.outbox_id

        # Second create with same key tuple but different outbox_id.
        item2 = DeliveryOutboxItem(  # type: ignore[no-untyped-call]
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-idem",
            target_adapter="fake_presentation",
            target_channel="ch-1",
            attempt_number=1,
        )
        created2 = await temp_storage.create_outbox_item(item2)
        # Should return the existing item (item1's outbox_id).
        assert created2.outbox_id == item1.outbox_id

    async def test_different_channel_allows_separate(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item1 = _make_outbox_item(delivery_plan_id="plan-multi", target_channel="ch-1")
        item2 = _make_outbox_item(delivery_plan_id="plan-multi", target_channel="ch-2")
        created1 = await temp_storage.create_outbox_item(item1)
        created2 = await temp_storage.create_outbox_item(item2)
        assert created1.outbox_id != created2.outbox_id

    async def test_null_channel_duplicate_returns_existing(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Two items with NULL target_channel and same key tuple should
        not create duplicates (covered by partial UNIQUE index)."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-null-ch",
            target_channel=None,
        )
        created1 = await temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-null-ch",
            target_adapter="fake_presentation",
            target_channel=None,
            attempt_number=1,
        )
        created2 = await temp_storage.create_outbox_item(item2)
        # Should return existing item (idempotent).
        assert created2.outbox_id == created1.outbox_id

    async def test_different_attempt_allows_separate(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item1 = _make_outbox_item(delivery_plan_id="plan-attempt", attempt_number=1)
        item2 = _make_outbox_item(delivery_plan_id="plan-attempt", attempt_number=2)
        created1 = await temp_storage.create_outbox_item(item1)
        created2 = await temp_storage.create_outbox_item(item2)
        assert created1.outbox_id != created2.outbox_id

    async def test_recreate_after_terminal_allows_new_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After an item reaches a terminal status, creating a new item
        with the same key tuple should succeed (re-delivery)."""
        # Create, claim (pending -> in_progress), then mark dead_lettered.
        item1 = _make_outbox_item(
            delivery_plan_id="plan-recreate",
            target_channel="ch-r1",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="w1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        await temp_storage.mark_outbox_dead_lettered(
            created1.outbox_id, failure_kind="adapter_permanent"
        )

        # Re-create with same key tuple.
        item2 = _make_outbox_item(
            delivery_plan_id="plan-recreate",
            target_channel="ch-r1",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # Should succeed with a NEW outbox_id (terminal row was deleted).
        assert created2.outbox_id == item2.outbox_id
        assert created2.outbox_id != created1.outbox_id
        assert created2.status == "pending"

    async def test_recreate_after_sent_allows_new_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After an item is marked sent (terminal), re-creation should succeed."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-recreate-sent",
            target_channel="ch-rs",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="w1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        await temp_storage.mark_outbox_sent(created1.outbox_id, receipt_id="rcpt-1")

        item2 = _make_outbox_item(
            delivery_plan_id="plan-recreate-sent",
            target_channel="ch-rs",
        )
        created2 = await temp_storage.create_outbox_item(item2)
        assert created2.outbox_id == item2.outbox_id
        assert created2.outbox_id != created1.outbox_id


# ===================================================================
# List
# ===================================================================


class TestListOutboxItems:
    """list_outbox_items() with status and due filters."""

    async def test_list_all(self, temp_storage: SQLiteStorage) -> None:
        item1 = _make_outbox_item(delivery_plan_id="plan-list-1")
        item2 = _make_outbox_item(delivery_plan_id="plan-list-2")
        await temp_storage.create_outbox_item(item1)
        await temp_storage.create_outbox_item(item2)

        items = await temp_storage.list_outbox_items()
        assert len(items) >= 2

    async def test_list_by_status(self, temp_storage: SQLiteStorage) -> None:
        pending_item = _make_outbox_item(
            delivery_plan_id="plan-status-1", status="pending"
        )
        sent_item = _make_outbox_item(delivery_plan_id="plan-status-2", status="sent")
        await temp_storage.create_outbox_item(pending_item)
        await temp_storage.create_outbox_item(sent_item)

        pendings = await temp_storage.list_outbox_items(status_filter=["pending"])
        assert all(i.status == "pending" for i in pendings)

        sents = await temp_storage.list_outbox_items(status_filter=["sent"])
        assert all(i.status == "sent" for i in sents)

    async def test_list_limit_and_offset(self, temp_storage: SQLiteStorage) -> None:
        ids = []
        for i in range(5):
            item = _make_outbox_item(delivery_plan_id=f"plan-limit-{i}")
            await temp_storage.create_outbox_item(item)
            ids.append(item.outbox_id)

        page1 = await temp_storage.list_outbox_items(limit=2, offset=0)
        assert len(page1) == 2


# ===================================================================
# Claim
# ===================================================================


class TestClaimDueItems:
    """claim_due_outbox_items() should atomically claim eligible items."""

    async def test_claim_pending_item(self, temp_storage: SQLiteStorage) -> None:
        item = _make_outbox_item(delivery_plan_id="plan-claim-1")
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        c = claimed[0]
        assert c.outbox_id == item.outbox_id
        assert c.status == "in_progress"
        assert c.worker_id == "worker-1"
        assert c.locked_at is not None
        assert c.lease_until is not None

    async def test_claim_respects_limit(self, temp_storage: SQLiteStorage) -> None:
        for i in range(5):
            item = _make_outbox_item(delivery_plan_id=f"plan-climit-{i}")
            await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", limit=3
        )
        assert len(claimed) == 3

    async def test_claim_does_not_double_claim(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Two workers should claim disjoint sets."""
        for i in range(5):
            item = _make_outbox_item(delivery_plan_id=f"plan-double-{i}")
            await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        worker2 = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-2", lease_seconds=30, limit=10
        )
        # Worker 2 should get nothing — all items claimed by worker 1.
        assert len(worker2) == 0

    async def test_lease_expiry_allows_reclaim(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After lease expiry, in_progress items are reclaimable by another
        worker.  This prevents items from getting permanently stuck."""
        item = _make_outbox_item(delivery_plan_id="plan-lease-expire")
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed1 = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed1) == 1

        # After lease expiry, item is reclaimable by another worker.
        later = "2026-02-01T00:00:00"  # well past lease expiry
        claimed2 = await temp_storage.claim_due_outbox_items(
            now=later, worker_id="worker-2", lease_seconds=30, limit=10
        )
        assert len(claimed2) == 1
        assert claimed2[0].worker_id == "worker-2"

    async def test_claim_skips_sent_items(self, temp_storage: SQLiteStorage) -> None:
        sent_item = _make_outbox_item(delivery_plan_id="plan-sent-skip", status="sent")
        await temp_storage.create_outbox_item(sent_item)

        pending_item = _make_outbox_item(delivery_plan_id="plan-pending-claim")
        await temp_storage.create_outbox_item(pending_item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", limit=10
        )
        assert len(claimed) == 1
        assert claimed[0].delivery_plan_id == "plan-pending-claim"


# ===================================================================
# Status transitions
# ===================================================================


class TestStatusTransitions:
    """Outbox status methods correctly transition items."""

    async def _create_and_claim(self, storage: SQLiteStorage, plan_id: str) -> str:
        item = _make_outbox_item(delivery_plan_id=plan_id)
        await storage.create_outbox_item(item)
        claimed = await storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        return claimed[0].outbox_id

    async def test_mark_sent(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-sent")
        await temp_storage.mark_outbox_sent(oid, receipt_id="rcpt-sent-1")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"
        assert item.receipt_id == "rcpt-sent-1"
        assert item.locked_at is None  # terminal clears lease

    async def test_mark_queued(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-queued")
        await temp_storage.mark_outbox_queued(oid, receipt_id="rcpt-queued-1")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"

    async def test_mark_retry_wait(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-retry")
        next_at = "2026-01-01T01:00:00"
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at=next_at,
            failure_kind="adapter_transient",
            error_summary="Connection timeout",
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "retry_wait"
        assert item.next_attempt_at == next_at
        assert item.failure_kind == "adapter_transient"
        assert item.error_summary == "Connection timeout"
        assert item.locked_at is None  # retry_wait clears lease
        assert item.lease_until is None
        assert item.worker_id is None

    async def test_mark_dead_lettered(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-dl")
        await temp_storage.mark_outbox_dead_lettered(
            oid,
            failure_kind="adapter_permanent",
            error_summary="All retries exhausted",
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "dead_lettered"
        assert item.locked_at is None

    async def test_mark_cancelled(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-cancel")
        await temp_storage.mark_outbox_cancelled(oid, error_summary="Shutdown")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "cancelled"

    async def test_mark_abandoned(self, temp_storage: SQLiteStorage) -> None:
        oid = await self._create_and_claim(temp_storage, "plan-ts-abandon")
        await temp_storage.mark_outbox_abandoned(oid, error_summary="Drain timeout")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "abandoned"

    async def test_terminal_no_regression(self, temp_storage: SQLiteStorage) -> None:
        """Once sent, subsequent mark calls should be no-ops."""
        oid = await self._create_and_claim(temp_storage, "plan-ts-noregress")
        await temp_storage.mark_outbox_sent(oid)
        # Try to overwrite with queued — should be ignored.
        await temp_storage.mark_outbox_queued(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"  # unchanged


# ===================================================================
# Release claim
# ===================================================================


class TestReleaseClaim:
    """release_outbox_claim() clears lease fields and restores status."""

    async def test_release_claim_restores_pending(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item = _make_outbox_item(delivery_plan_id="plan-rel-pend")
        await temp_storage.create_outbox_item(item)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        await temp_storage.release_outbox_claim(
            oid, "worker-1", release_status="pending"
        )
        released = await temp_storage.get_outbox_item(oid)
        assert released is not None
        assert released.locked_at is None
        assert released.lease_until is None
        assert released.worker_id is None
        assert released.status == "pending"

    async def test_release_claim_restores_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item = _make_outbox_item(
            delivery_plan_id="plan-rel-rw",
            status="retry_wait",
            next_attempt_at="2026-01-01T00:05:00",
        )
        await temp_storage.create_outbox_item(item)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:05:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        # Claim clears next_attempt_at; release restores status to
        # retry_wait but does not recover the original next_attempt_at
        # (the claim consumed it).
        await temp_storage.release_outbox_claim(
            oid, "worker-1", release_status="retry_wait"
        )
        released = await temp_storage.get_outbox_item(oid)
        assert released is not None
        assert released.locked_at is None
        assert released.lease_until is None
        assert released.worker_id is None
        assert released.status == "retry_wait"
        # next_attempt_at was cleared by claim; release does not restore it.
        # The pipeline must set a new next_attempt_at when re-scheduling.
        assert released.next_attempt_at is None

    async def test_release_wrong_worker_noop(self, temp_storage: SQLiteStorage) -> None:
        item = _make_outbox_item(delivery_plan_id="plan-release-wrong")
        await temp_storage.create_outbox_item(item)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        await temp_storage.release_outbox_claim(oid, "worker-2")
        item_after = await temp_storage.get_outbox_item(oid)
        assert item_after is not None
        assert item_after.worker_id == "worker-1"  # unchanged


# ===================================================================
# Count by status
# ===================================================================


class TestCountByStatus:
    """count_outbox_by_status() groups counts correctly."""

    async def test_counts(self, temp_storage: SQLiteStorage) -> None:
        # Create items in various statuses
        pending1 = _make_outbox_item(delivery_plan_id="plan-cnt-p1")
        pending2 = _make_outbox_item(delivery_plan_id="plan-cnt-p2")
        sent1 = _make_outbox_item(delivery_plan_id="plan-cnt-s1", status="sent")
        await temp_storage.create_outbox_item(pending1)
        await temp_storage.create_outbox_item(pending2)
        await temp_storage.create_outbox_item(sent1)

        counts = await temp_storage.count_outbox_by_status()
        assert counts.get("pending", 0) == 2
        assert counts.get("sent", 0) == 1

    async def test_empty_db_returns_empty_dict(
        self, temp_storage: SQLiteStorage
    ) -> None:
        counts = await temp_storage.count_outbox_by_status()
        assert counts == {}


# ===================================================================
# Persistence across connection restart
# ===================================================================


class TestPersistence:
    """Outbox items survive SQLite close/reopen."""

    async def test_persistence_across_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        try:
            storage = SQLiteStorage(db_path=db_path)
            try:
                await storage.initialize()

                item = _make_outbox_item(delivery_plan_id="plan-persist")
                created = await storage.create_outbox_item(item)
            finally:
                await storage.close()

            # Re-open
            storage2 = SQLiteStorage(db_path=db_path)
            try:
                await storage2.initialize()
                retrieved = await storage2.get_outbox_item(created.outbox_id)
                assert retrieved is not None
                assert retrieved.status == "pending"
                assert retrieved.delivery_plan_id == "plan-persist"
            finally:
                await storage2.close()
        finally:
            os.unlink(db_path)


# ===================================================================
# Group 1: Status transition guard tests
# ===================================================================


class TestStatusTransitionGuards:
    """Verify _update_outbox_status allowed_from guards block invalid
    transitions and leave the row unchanged."""

    async def _create_pending(self, storage: SQLiteStorage, plan_id: str) -> str:
        """Create a pending outbox item and return its outbox_id."""
        item = _make_outbox_item(delivery_plan_id=plan_id)
        created = await storage.create_outbox_item(item)
        return created.outbox_id

    async def _create_in_progress(self, storage: SQLiteStorage, plan_id: str) -> str:
        """Create a pending item, claim it to in_progress, return outbox_id."""
        item = _make_outbox_item(delivery_plan_id=plan_id)
        await storage.create_outbox_item(item)
        claimed = await storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) >= 1
        return claimed[0].outbox_id

    async def test_pending_cannot_be_marked_sent_directly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """pending -> sent is blocked (must go through in_progress first)."""
        oid = await self._create_pending(temp_storage, "plan-guard-p2s")
        await temp_storage.mark_outbox_sent(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "pending"

    async def test_retry_wait_cannot_be_marked_queued(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """retry_wait -> queued is not an allowed transition."""
        oid = await self._create_in_progress(temp_storage, "plan-guard-rw2q")
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "retry_wait"

        # Attempt invalid transition retry_wait -> queued
        await temp_storage.mark_outbox_queued(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "retry_wait"

    async def test_queued_can_be_marked_sent(self, temp_storage: SQLiteStorage) -> None:
        """in_progress -> queued -> sent should work."""
        oid = await self._create_in_progress(temp_storage, "plan-guard-q2s")
        await temp_storage.mark_outbox_queued(oid, receipt_id="rcpt-q2s")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"

        await temp_storage.mark_outbox_sent(oid, receipt_id="rcpt-q2s-sent")
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"

    async def test_in_progress_can_transition_to_queued(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await self._create_in_progress(temp_storage, "plan-guard-ip2q")
        await temp_storage.mark_outbox_queued(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"

    async def test_in_progress_can_transition_to_sent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await self._create_in_progress(temp_storage, "plan-guard-ip2s")
        await temp_storage.mark_outbox_sent(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"

    async def test_in_progress_can_transition_to_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await self._create_in_progress(temp_storage, "plan-guard-ip2rw")
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "retry_wait"

    async def test_in_progress_can_transition_to_dead_lettered(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await self._create_in_progress(temp_storage, "plan-guard-ip2dl")
        await temp_storage.mark_outbox_dead_lettered(
            oid, failure_kind="adapter_permanent"
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "dead_lettered"

    async def test_terminal_states_cannot_regress(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Each terminal status should resist regression to non-terminal."""
        terminal_statuses = ["sent", "dead_lettered", "cancelled", "abandoned"]

        for ts_idx, terminal in enumerate(terminal_statuses):
            # Create in_progress then transition to terminal
            oid = await self._create_in_progress(
                temp_storage, f"plan-guard-term-{terminal}-{ts_idx}"
            )
            if terminal == "sent":
                await temp_storage.mark_outbox_sent(oid)
            elif terminal == "dead_lettered":
                await temp_storage.mark_outbox_dead_lettered(oid, failure_kind="test")
            elif terminal == "cancelled":
                await temp_storage.mark_outbox_cancelled(oid)
            elif terminal == "abandoned":
                await temp_storage.mark_outbox_abandoned(oid)

            # Try to regress by calling mark_outbox_queued (no-op on terminal)
            await temp_storage.mark_outbox_queued(oid)
            item = await temp_storage.get_outbox_item(oid)
            assert item is not None
            assert item.status == terminal

    async def test_invalid_transition_leaves_row_unchanged(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """pending -> sent is an invalid transition; status stays pending."""
        oid = await self._create_pending(temp_storage, "plan-guard-noop")
        await temp_storage.mark_outbox_sent(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "pending"


# ===================================================================
# Group 2: Atomic create_outbox_item tests
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
# Group 3: Queued lease semantics tests
# ===================================================================


class TestQueuedLeaseSemantics:
    """Verify that marking queued clears lease fields and that queued items
    are not claimable."""

    async def _create_in_progress(self, storage: SQLiteStorage, plan_id: str) -> str:
        """Create pending, claim to in_progress, return outbox_id."""
        item = _make_outbox_item(delivery_plan_id=plan_id)
        await storage.create_outbox_item(item)
        claimed = await storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) >= 1
        return claimed[0].outbox_id

    async def test_mark_queued_clears_lease_fields(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Transitioning in_progress -> queued clears all lease fields."""
        oid = await self._create_in_progress(temp_storage, "plan-lease-clear")

        # Verify lease fields are set after claim
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "in_progress"
        assert item.locked_at is not None
        assert item.lease_until is not None
        assert item.worker_id == "worker-1"

        # Mark queued
        await temp_storage.mark_outbox_queued(oid)

        # Lease fields should be cleared
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"
        assert item.locked_at is None
        assert item.lease_until is None
        assert item.worker_id is None

    async def test_queued_remains_not_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A queued item should not be returned by claim_due_outbox_items."""
        oid = await self._create_in_progress(temp_storage, "plan-lease-noclaim")
        await temp_storage.mark_outbox_queued(oid)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-2", lease_seconds=30, limit=10
        )
        assert not any(c.outbox_id == oid for c in claimed)

    async def test_queued_to_sent_still_works(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """in_progress -> queued -> sent transitions correctly and clears
        lease fields."""
        oid = await self._create_in_progress(temp_storage, "plan-lease-q2s")
        await temp_storage.mark_outbox_queued(oid)
        await temp_storage.mark_outbox_sent(oid, receipt_id="rcpt-final")

        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"
        assert item.locked_at is None
        assert item.lease_until is None
        assert item.worker_id is None
        assert item.receipt_id == "rcpt-final"


class TestAsyncTransactionRollback:
    """Regression: aiosqlite create_outbox_item must rollback on any failure
    between BEGIN IMMEDIATE and COMMIT, leaving the connection usable."""

    async def test_rollback_after_mid_transaction_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Force an error between BEGIN and INSERT, then verify the same
        storage connection can still create outbox items."""

        item = _make_outbox_item(delivery_plan_id="plan-txn-rollback")
        await temp_storage.create_outbox_item(item)
        fetched = await temp_storage.get_outbox_item(item.outbox_id)
        assert fetched is not None

        # Now force a failure inside the aiosqlite path by making execute
        # raise after the BEGIN.  We patch at the storage layer.
        if not temp_storage._use_aiosqlite:
            # Sync path uses threading.Lock and _sync_atomic_create_outbox
            # which already has proper rollback via BaseException handler.
            pytest.skip("aiosqlite not available")

        real_execute = temp_storage._db.execute
        call_count = 0

        def _flaky_execute(stmt, params=None) -> object:
            nonlocal call_count
            call_count += 1
            # Let BEGIN succeed (call 1), fail on the SELECT (call 2).
            if call_count == 2:
                raise RuntimeError("injected mid-transaction error")
            # Delegate to the real aiosqlite Connection.execute, which is a
            # regular function returning a Result that supports both ``await``
            # and ``async with``.
            if params is not None:
                return real_execute(stmt, params)
            return real_execute(stmt)

        temp_storage._db.execute = _flaky_execute  # type: ignore[assignment]

        try:
            item2 = _make_outbox_item(delivery_plan_id="plan-txn-rollback-2")
            with pytest.raises(RuntimeError, match="injected mid-transaction"):
                await temp_storage.create_outbox_item(item2)
        finally:
            temp_storage._db.execute = real_execute  # type: ignore[assignment]

        # The connection must still be usable after the failed transaction.
        item3 = _make_outbox_item(delivery_plan_id="plan-txn-recovery")
        await temp_storage.create_outbox_item(item3)
        fetched3 = await temp_storage.get_outbox_item(item3.outbox_id)
        assert fetched3 is not None
        assert fetched3.delivery_plan_id == "plan-txn-recovery"


# ===================================================================
# Group 4: Stale queued reclaim
# ===================================================================


class TestStaleQueuedReclaim:
    """Verify that claim_due_outbox_items reclaims stale queued rows
    (updated_at older than STALE_QUEUED_GRACE_SECONDS) while leaving
    fresh queued rows untouched."""

    async def _create_and_queue(
        self,
        storage: SQLiteStorage,
        plan_id: str,
        updated_at: str | None = None,
    ) -> str:
        """Create pending, claim to in_progress, mark queued. Returns outbox_id."""
        item = _make_outbox_item(delivery_plan_id=plan_id)
        await storage.create_outbox_item(item)
        claimed = await storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) >= 1
        oid = [c for c in claimed if c.delivery_plan_id == plan_id][0].outbox_id
        await storage.mark_outbox_queued(oid)

        # Optionally override updated_at to simulate a specific timestamp.
        if updated_at is not None:
            await storage._write(
                "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
                (updated_at, oid),
            )
        return oid

    async def test_stale_queued_claimed_after_grace(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A queued row whose updated_at is older than the grace period
        should be reclaimed by claim_due_outbox_items."""
        grace = STALE_QUEUED_GRACE_SECONDS
        now_claim = "2026-01-01T01:00:00"
        # Make the queued row appear stale: updated_at is well before
        # now_claim - grace.
        stale_updated = "2026-01-01T00:00:00"  # 1h before now_claim, > grace
        oid = await self._create_and_queue(
            temp_storage,
            plan_id="plan-stale-q-1",
            updated_at=stale_updated,
        )

        claimed = await temp_storage.claim_due_outbox_items(
            now=now_claim,
            worker_id="worker-2",
            lease_seconds=30,
            limit=10,
        )
        matched = [c for c in claimed if c.outbox_id == oid]
        assert len(matched) == 1
        assert matched[0].status == "in_progress"
        assert matched[0].worker_id == "worker-2"

    async def test_fresh_queued_not_claimed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A queued row whose updated_at is within the grace period
        should NOT be claimed."""
        now_claim = "2026-01-01T01:00:00"
        grace = STALE_QUEUED_GRACE_SECONDS
        # Set updated_at to exactly now_claim - grace + 10s (still fresh).
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.fromisoformat(now_claim) - timedelta(seconds=grace)
        fresh_updated = (cutoff + timedelta(seconds=10)).isoformat()
        oid = await self._create_and_queue(
            temp_storage,
            plan_id="plan-fresh-q-1",
            updated_at=fresh_updated,
        )

        claimed = await temp_storage.claim_due_outbox_items(
            now=now_claim,
            worker_id="worker-2",
            lease_seconds=30,
            limit=10,
        )
        assert not any(c.outbox_id == oid for c in claimed)

        # Row should still be queued
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"


# ===================================================================
# Group 5: create_outbox_item must not steal active work
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
        assert created2.next_attempt_at is None, (
            "Reclaiming a retry_wait row must clear next_attempt_at"
        )


# ===================================================================
# Group 6: claim_due_outbox_items clears next_attempt_at
# ===================================================================


class TestClaimClearsNextAttemptAt:
    """When claim_due_outbox_items moves a row to in_progress,
    next_attempt_at must be set to NULL."""

    async def test_claim_clears_next_attempt_at_from_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item with a scheduled next_attempt_at should have
        it cleared after being claimed."""
        item = _make_outbox_item(
            delivery_plan_id="plan-clear-naa",
            status="retry_wait",
            next_attempt_at="2026-01-01T00:05:00",
        )
        await temp_storage.create_outbox_item(item)

        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:05:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        assert claimed[0].status == "in_progress"
        assert claimed[0].next_attempt_at is None

        # Verify in storage too
        stored = await temp_storage.get_outbox_item(claimed[0].outbox_id)
        assert stored is not None
        assert stored.next_attempt_at is None

    async def test_claim_clears_next_attempt_at_from_pending(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A pending item with NULL next_attempt_at should remain NULL
        after claim."""
        item = _make_outbox_item(delivery_plan_id="plan-clear-naa-p")
        await temp_storage.create_outbox_item(item)

        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        assert claimed[0].next_attempt_at is None


# ===================================================================
# Group 7: Aiosqlite write lock serialisation
# ===================================================================


class TestAiosqliteWriteLock:
    """Verify that the asyncio write lock prevents aiosqlite write
    interleaving.  This test uses a single-event-loop concurrency
    pattern rather than timing-based checks."""

    async def test_concurrent_writes_are_serialised(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Two concurrent _write calls should not interleave on the
        aiosqlite connection.  We verify serialisation by checking that
        both writes complete successfully without corruption."""
        import asyncio

        item1 = _make_outbox_item(delivery_plan_id="plan-lock-1")
        item2 = _make_outbox_item(delivery_plan_id="plan-lock-2")

        # Fire two create_outbox_item calls concurrently.
        # Both should succeed; the write lock ensures serialisation.
        results = await asyncio.gather(
            temp_storage.create_outbox_item(item1),
            temp_storage.create_outbox_item(item2),
        )
        assert results[0].outbox_id == item1.outbox_id
        assert results[1].outbox_id == item2.outbox_id

        # Both items should be readable
        fetched1 = await temp_storage.get_outbox_item(item1.outbox_id)
        fetched2 = await temp_storage.get_outbox_item(item2.outbox_id)
        assert fetched1 is not None
        assert fetched2 is not None

    async def test_write_and_create_outbox_serialised(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A _write call and create_outbox_item running concurrently
        should not interleave on the aiosqlite connection."""
        import asyncio

        item = _make_outbox_item(delivery_plan_id="plan-lock-3")

        # Run a direct _write and a create_outbox_item concurrently
        async def do_write() -> None:
            await temp_storage._write(
                "INSERT INTO delivery_outbox"
                " (outbox_id, event_id, route_id, delivery_plan_id,"
                "  target_adapter, status, created_at, updated_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"obox-direct-{uuid.uuid4()}",
                    "evt-1",
                    "route-1",
                    "plan-lock-direct",
                    "fake_presentation",
                    "pending",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                    "{}",
                ),
            )

        results = await asyncio.gather(
            do_write(),
            temp_storage.create_outbox_item(item),
        )
        # Both should succeed
        fetched = await temp_storage.get_outbox_item(item.outbox_id)
        assert fetched is not None


# ===================================================================
# Group 8: is_claimable property
# ===================================================================


class TestIsClaimable:
    """Verify DeliveryOutboxItem.is_claimable reflects direct claimability
    only, not expired-lease or stale-queued reclaim paths."""

    def test_pending_is_claimable(self) -> None:
        item = _make_outbox_item(status="pending")
        assert item.is_claimable is True

    def test_retry_wait_is_claimable(self) -> None:
        item = _make_outbox_item(status="retry_wait")
        assert item.is_claimable is True

    def test_in_progress_not_directly_claimable(self) -> None:
        item = _make_outbox_item(status="in_progress")
        assert item.is_claimable is False

    def test_queued_not_directly_claimable(self) -> None:
        item = _make_outbox_item(status="queued")
        assert item.is_claimable is False

    def test_sent_not_claimable(self) -> None:
        item = _make_outbox_item(status="sent")
        assert item.is_claimable is False

    def test_dead_lettered_not_claimable(self) -> None:
        item = _make_outbox_item(status="dead_lettered")
        assert item.is_claimable is False
