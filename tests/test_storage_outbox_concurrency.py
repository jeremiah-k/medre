"""Tests for delivery_outbox concurrency and edge cases: aiosqlite write lock
serialisation, async transaction rollback, stale queued reclaim, and the
is_claimable model property."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS
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
# Async transaction rollback
# ===================================================================


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

    async def test_fresh_queued_not_claimed(self, temp_storage: SQLiteStorage) -> None:
        """A queued row whose updated_at is within the grace period
        should NOT be claimed."""
        now_claim = "2026-01-01T01:00:00"
        grace = STALE_QUEUED_GRACE_SECONDS
        # Set updated_at to exactly now_claim - grace + 10s (still fresh).
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
# Aiosqlite write lock serialisation
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

        await asyncio.gather(
            do_write(),
            temp_storage.create_outbox_item(item),
        )
        # Both should succeed
        fetched = await temp_storage.get_outbox_item(item.outbox_id)
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
