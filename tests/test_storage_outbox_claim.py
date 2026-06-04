"""Tests for delivery_outbox claim operations: claim due items, release claim,
and claim clearing next_attempt_at."""

from __future__ import annotations

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item

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

    async def test_release_non_in_progress_is_noop(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """release_outbox_claim only releases in_progress rows.

        A pending item (never claimed) should remain pending after a
        release attempt with a matching worker_id.
        """
        item = _make_outbox_item(delivery_plan_id="plan-release-nonip")
        created = await temp_storage.create_outbox_item(item)
        assert created.status == "pending"

        # Try to release a pending item — no worker was ever assigned,
        # but even if we pass the outbox_id, the in_progress guard
        # must prevent the update.
        await temp_storage.release_outbox_claim(
            created.outbox_id, "worker-1", release_status="pending"
        )
        after = await temp_storage.get_outbox_item(created.outbox_id)
        assert after is not None
        assert after.status == "pending"  # unchanged

    async def test_release_retry_wait_item_is_noop(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An item transitioned to retry_wait cannot be released back to
        pending — release_outbox_claim only works on in_progress rows."""
        item = _make_outbox_item(delivery_plan_id="plan-release-rw-noop")
        await temp_storage.create_outbox_item(item)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        # Move to retry_wait
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )
        item_rw = await temp_storage.get_outbox_item(oid)
        assert item_rw is not None
        assert item_rw.status == "retry_wait"

        # Attempt to release — should be a no-op
        await temp_storage.release_outbox_claim(oid, "worker-1")
        after = await temp_storage.get_outbox_item(oid)
        assert after is not None
        assert after.status == "retry_wait"  # unchanged


# ===================================================================
# Claim clears next_attempt_at
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
