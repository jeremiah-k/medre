"""Outbox race model tests: in_progress lease protection, expired lease
reclaim, slow-adapter simulation, and live pipeline lease verification.

These tests verify that the claim_due_outbox_items query correctly
distinguishes between:
- in_progress items with unexpired leases (not claimable)
- in_progress items with expired leases (claimable)
- live pipeline items created with valid leases
"""

from __future__ import annotations

import uuid
from datetime import datetime

from medre.core.storage import DeliveryOutboxItem, SQLiteStorage


def _make_outbox_item(
    delivery_plan_id: str = "plan-race-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    status: str = "pending",
    next_attempt_at: str | None = None,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Build a minimal outbox item for race model tests."""
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id="evt-race-1",
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


def _now() -> str:
    """Return a fixed reference timestamp for tests."""
    return "2026-06-01T12:00:00+00:00"


class TestInProgressLeaseProtection:
    """in_progress items with unexpired leases must not be claimable."""

    async def test_in_progress_with_unexpired_lease_not_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An in_progress item with lease_until in the future should NOT be
        claimed by claim_due_outbox_items."""
        now = _now()

        # Create a pending item then claim it to set up in_progress + lease
        item = _make_outbox_item(delivery_plan_id="plan-race-unexpired")
        await temp_storage.create_outbox_item(item)

        # Claim with a 300s lease (well into the future from 'now')
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=300, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        # Verify item is in_progress with lease
        item_after = await temp_storage.get_outbox_item(oid)
        assert item_after is not None
        assert item_after.status == "in_progress"
        assert item_after.lease_until is not None

        # Another worker tries to claim at the same time — should get nothing
        claimed2 = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-2", lease_seconds=30, limit=10
        )
        assert not any(c.outbox_id == oid for c in claimed2)

    async def test_in_progress_with_expired_lease_is_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An in_progress item with lease_until in the past SHOULD be
        reclaimed by claim_due_outbox_items."""
        now = _now()

        # Create and claim
        item = _make_outbox_item(delivery_plan_id="plan-race-expired")
        await temp_storage.create_outbox_item(item)

        # Claim with short lease
        claimed1 = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=10, limit=10
        )
        assert len(claimed1) == 1
        oid = claimed1[0].outbox_id

        # Simulate time passing — lease has expired
        later = "2026-06-01T12:01:00+00:00"  # 60 seconds later
        claimed2 = await temp_storage.claim_due_outbox_items(
            now=later, worker_id="worker-2", lease_seconds=30, limit=10
        )
        matching = [c for c in claimed2 if c.outbox_id == oid]
        assert len(matching) == 1
        assert matching[0].worker_id == "worker-2"

    async def test_slow_adapter_in_progress_protected_from_claim(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Simulates a slow adapter: an in_progress item with a valid lease
        must not be claimed by another worker.

        This models the race where the live pipeline is still delivering
        while the retry worker runs a claim cycle.  The key invariant is
        that unexpired in_progress rows are invisible to claim.
        """
        now = _now()

        # Create a pending item and claim it (simulating the live pipeline
        # grabbing it for delivery)
        item = _make_outbox_item(delivery_plan_id="plan-race-slow")
        await temp_storage.create_outbox_item(item)

        live_claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="live-pipeline-1", lease_seconds=300, limit=10
        )
        assert len(live_claimed) == 1
        oid = live_claimed[0].outbox_id

        # Verify it's in_progress with a future lease
        item_check = await temp_storage.get_outbox_item(oid)
        assert item_check is not None
        assert item_check.status == "in_progress"
        assert item_check.lease_until is not None

        # The retry worker tries to claim — MUST NOT get this item
        retry_claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="retry-worker-1", lease_seconds=30, limit=10
        )
        assert not any(c.outbox_id == oid for c in retry_claimed)

        # Now the live pipeline finishes delivery — marks as sent
        await temp_storage.mark_outbox_sent(oid, receipt_id="rcpt-slow-done")

        # Final state is sent
        final = await temp_storage.get_outbox_item(oid)
        assert final is not None
        assert final.status == "sent"
        assert final.locked_at is None

    async def test_live_pipeline_creates_in_progress_with_lease(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Verify that claim_due_outbox_items creates in_progress items
        with non-null locked_at, lease_until, and worker_id."""
        now = _now()

        item = _make_outbox_item(delivery_plan_id="plan-race-lease-fields")
        await temp_storage.create_outbox_item(item)

        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-lease-test", lease_seconds=60, limit=10
        )
        assert len(claimed) == 1
        c = claimed[0]
        assert c.status == "in_progress"
        assert c.locked_at is not None
        assert c.lease_until is not None
        assert c.worker_id == "worker-lease-test"

        # Verify lease_until is in the future relative to 'now'
        assert c.lease_until is not None
        assert datetime.fromisoformat(c.lease_until) > datetime.fromisoformat(now)

    async def test_renewed_lease_remains_unclaimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After renew_outbox_lease extends the lease, the item must still
        not be claimable at a time past the *original* lease but within
        the renewed lease window.

        This models the slow live delivery scenario: the pipeline owns the
        item, the original short lease (60s) would expire, but the renewal
        task extends it (to 1800s).  The retry worker must not reclaim it.
        """
        from datetime import timedelta

        now = _now()
        now_dt = datetime.fromisoformat(now)

        # Create and claim with a short 60s lease.
        item = _make_outbox_item(delivery_plan_id="plan-race-renewed")
        await temp_storage.create_outbox_item(item)

        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="live-pipeline-1", lease_seconds=60, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        # Renew the lease to 1800s from now (simulating the renewal task).
        renewed_lease = (now_dt + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            oid, "live-pipeline-1", renewed_lease
        )
        assert result is True

        # At T+120s (past the original 60s lease, within the renewed 1800s
        # lease), the retry worker MUST NOT be able to claim this item.
        after_original_lease = (now_dt + timedelta(seconds=120)).isoformat()
        retry_claimed = await temp_storage.claim_due_outbox_items(
            now=after_original_lease,
            worker_id="retry-worker-1",
            lease_seconds=30,
            limit=10,
        )
        assert not any(c.outbox_id == oid for c in retry_claimed)
