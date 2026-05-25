"""Tests for delivery_outbox storage operations: create, list, get, update,
claim, lease, idempotent create, status transitions, count, and persistence.
"""

from __future__ import annotations

import uuid

from medre.core.storage import DeliveryOutboxItem, SQLiteStorage


def _make_outbox_item(
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    attempt_number: int = 1,
    status: str = "pending",
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

    async def test_different_attempt_allows_separate(
        self, temp_storage: SQLiteStorage
    ) -> None:
        item1 = _make_outbox_item(delivery_plan_id="plan-attempt", attempt_number=1)
        item2 = _make_outbox_item(delivery_plan_id="plan-attempt", attempt_number=2)
        created1 = await temp_storage.create_outbox_item(item1)
        created2 = await temp_storage.create_outbox_item(item2)
        assert created1.outbox_id != created2.outbox_id


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

    async def test_lease_expiry_does_not_auto_reclaim(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After lease expiry, items stay ``in_progress`` and are not
        auto-reclaimed.  This prevents duplicate processing.  Operators
        must resolve stale in_progress items manually."""
        item = _make_outbox_item(delivery_plan_id="plan-lease-expire")
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed1 = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed1) == 1

        # After lease expiry, item is still in_progress — not reclaimable.
        later = "2026-02-01T00:00:00"  # well past lease expiry
        claimed2 = await temp_storage.claim_due_outbox_items(
            now=later, worker_id="worker-2", lease_seconds=30, limit=10
        )
        assert len(claimed2) == 0

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
    """release_outbox_claim() clears lease fields when worker matches."""

    async def test_release_claim(self, temp_storage: SQLiteStorage) -> None:
        item = _make_outbox_item(delivery_plan_id="plan-release")
        await temp_storage.create_outbox_item(item)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        await temp_storage.release_outbox_claim(oid, "worker-1")
        released = await temp_storage.get_outbox_item(oid)
        assert released is not None
        assert released.locked_at is None
        assert released.lease_until is None
        assert released.worker_id is None

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

        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        item = _make_outbox_item(delivery_plan_id="plan-persist")
        created = await storage.create_outbox_item(item)
        await storage.close()

        # Re-open
        storage2 = SQLiteStorage(db_path=db_path)
        await storage2.initialize()
        retrieved = await storage2.get_outbox_item(created.outbox_id)
        assert retrieved is not None
        assert retrieved.status == "pending"
        assert retrieved.delivery_plan_id == "plan-persist"
        await storage2.close()

        os.unlink(db_path)
