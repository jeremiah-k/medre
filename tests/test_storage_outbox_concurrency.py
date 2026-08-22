"""Tests for delivery_outbox concurrency and edge cases."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item

# ===================================================================
# Transaction rollback
# ===================================================================


async def test_rollback_after_mid_transaction_error(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Outbox creation rolls back a failed explicit transaction."""
    real_db = outbox_temp_storage._require_db()

    class _FailingConnection:
        def __init__(self) -> None:
            self.calls = 0
            self.rollback_called = False

        def execute(self, statement: str, params: tuple[object, ...] = ()):
            if statement == "ROLLBACK":
                self.rollback_called = True
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("injected mid-transaction error")
            return real_db.execute(statement, params)

        def commit(self) -> None:
            real_db.commit()

        def rollback(self) -> None:
            self.rollback_called = True
            real_db.rollback()

    failing = _FailingConnection()
    outbox_temp_storage._db = failing  # type: ignore[assignment]
    item = _make_outbox_item(delivery_plan_id="plan-txn-rollback")
    try:
        with pytest.raises(RuntimeError, match="injected mid-transaction"):
            await outbox_temp_storage.create_outbox_item(item)
    finally:
        outbox_temp_storage._db = real_db

    assert failing.rollback_called is True
    recovery = _make_outbox_item(delivery_plan_id="plan-txn-recovery")
    await outbox_temp_storage.create_outbox_item(recovery)
    fetched = await outbox_temp_storage.get_outbox_item(recovery.outbox_id)
    assert fetched is not None


# ===================================================================
# Stale queued reclaim
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
        assert len(claimed) == 1
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
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """A queued row whose updated_at is older than the grace period
        should be reclaimed by claim_due_outbox_items."""
        now_claim = "2026-01-01T01:00:00"
        # Make the queued row appear stale: updated_at is well before
        # now_claim - grace.
        stale_updated = "2026-01-01T00:00:00"  # 1h before now_claim, > grace
        oid = await self._create_and_queue(
            outbox_temp_storage,
            plan_id="plan-stale-q-1",
            updated_at=stale_updated,
        )

        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now=now_claim,
            worker_id="worker-2",
            lease_seconds=30,
            limit=10,
        )
        matched = [c for c in claimed if c.outbox_id == oid]
        assert len(matched) == 1
        assert matched[0].status == "in_progress"
        assert matched[0].worker_id == "worker-2"

    async def test_fresh_queued_not_claimed(self, outbox_temp_storage: SQLiteStorage) -> None:
        """A queued row whose updated_at is within the grace period
        should NOT be claimed."""
        now_claim = "2026-01-01T01:00:00"
        grace = STALE_QUEUED_GRACE_SECONDS
        # Set updated_at to exactly now_claim - grace + 10s (still fresh).
        cutoff = datetime.fromisoformat(now_claim) - timedelta(seconds=grace)
        fresh_updated = (cutoff + timedelta(seconds=10)).isoformat()
        oid = await self._create_and_queue(
            outbox_temp_storage,
            plan_id="plan-fresh-q-1",
            updated_at=fresh_updated,
        )

        claimed = await outbox_temp_storage.claim_due_outbox_items(
            now=now_claim,
            worker_id="worker-2",
            lease_seconds=30,
            limit=10,
        )
        assert not any(c.outbox_id == oid for c in claimed)

        # Row should still be queued
        item = await outbox_temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"


# ===================================================================
# Private-executor write serialisation
# ===================================================================


class TestSerializedWrites:
    """Concurrent async callers serialize through the private executor."""

    async def test_concurrent_writes_are_serialised(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """Two concurrent outbox creates complete without corruption."""
        item1 = _make_outbox_item(delivery_plan_id="plan-lock-1")
        item2 = _make_outbox_item(delivery_plan_id="plan-lock-2")

        # Both calls share the same single-worker SQLite executor.
        results = await asyncio.gather(
            outbox_temp_storage.create_outbox_item(item1),
            outbox_temp_storage.create_outbox_item(item2),
        )
        assert results[0].outbox_id == item1.outbox_id
        assert results[1].outbox_id == item2.outbox_id

        # Both items should be readable
        fetched1 = await outbox_temp_storage.get_outbox_item(item1.outbox_id)
        fetched2 = await outbox_temp_storage.get_outbox_item(item2.outbox_id)
        assert fetched1 is not None
        assert fetched2 is not None

    async def test_write_and_create_outbox_serialised(
        self, outbox_temp_storage: SQLiteStorage
    ) -> None:
        """A direct write and outbox transaction serialize safely."""
        item = _make_outbox_item(delivery_plan_id="plan-lock-3")

        # Run a direct _write and a create_outbox_item concurrently
        async def do_write() -> None:
            await outbox_temp_storage._write(
                "INSERT INTO delivery_outbox"
                " (outbox_id, event_id, route_id, delivery_plan_id,"
                "  target_adapter, status, created_at, updated_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"obox-direct-{uuid.uuid4()}",
                    "__outbox_default__",
                    "route-1",
                    "plan-lock-direct",
                    "fake_presentation",
                    "pending",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                    "{}",
                ),
            )

        await asyncio.gather(
            do_write(),
            outbox_temp_storage.create_outbox_item(item),
        )
        # Both should succeed
        fetched = await outbox_temp_storage.get_outbox_item(item.outbox_id)
        assert fetched is not None


# ===================================================================
# is_claimable property
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
