"""Tests for delivery_outbox status transitions: state machine guards, valid
transitions, queued lease semantics, and terminal state protection."""

from __future__ import annotations

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item


async def _create_in_progress(storage: SQLiteStorage, plan_id: str) -> str:
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


# ===================================================================
# Status transitions
# ===================================================================


class TestStatusTransitions:
    """Outbox status methods correctly transition items."""

    async def _create_and_claim(
        self, storage: SQLiteStorage, plan_id: str, *, attempt_number: int = 1
    ) -> str:
        item = _make_outbox_item(
            delivery_plan_id=plan_id, attempt_number=attempt_number
        )
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

    async def test_mark_dead_lettered_with_attempt_number(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """mark_outbox_dead_lettered persists attempt_number when provided."""
        oid = await self._create_and_claim(temp_storage, "plan-ts-dl-attnum")
        await temp_storage.mark_outbox_dead_lettered(
            oid,
            failure_kind="retry_exhausted",
            error_summary="Max retries reached",
            attempt_number=5,
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "dead_lettered"
        assert item.attempt_number == 5

    async def test_mark_dead_lettered_without_attempt_number(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """mark_outbox_dead_lettered without attempt_number preserves the
        original value (backwards compatible)."""
        oid = await self._create_and_claim(
            temp_storage, "plan-ts-dl-noattnum", attempt_number=3
        )
        # Original attempt_number is 3 (explicitly passed)
        await temp_storage.mark_outbox_dead_lettered(
            oid,
            failure_kind="adapter_permanent",
        )
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "dead_lettered"
        assert item.attempt_number == 3  # unchanged from creation

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
# Status transition guards
# ===================================================================


class TestStatusTransitionGuards:
    """Verify _update_outbox_status allowed_from guards block invalid
    transitions and leave the row unchanged."""

    async def _create_pending(self, storage: SQLiteStorage, plan_id: str) -> str:
        """Create a pending outbox item and return its outbox_id."""
        item = _make_outbox_item(delivery_plan_id=plan_id)
        created = await storage.create_outbox_item(item)
        return created.outbox_id

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
        oid = await _create_in_progress(temp_storage, "plan-guard-rw2q")
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
        oid = await _create_in_progress(temp_storage, "plan-guard-q2s")
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
        oid = await _create_in_progress(temp_storage, "plan-guard-ip2q")
        await temp_storage.mark_outbox_queued(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "queued"

    async def test_in_progress_can_transition_to_sent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await _create_in_progress(temp_storage, "plan-guard-ip2s")
        await temp_storage.mark_outbox_sent(oid)
        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"

    async def test_in_progress_can_transition_to_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        oid = await _create_in_progress(temp_storage, "plan-guard-ip2rw")
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
        oid = await _create_in_progress(temp_storage, "plan-guard-ip2dl")
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
            oid = await _create_in_progress(
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
# Queued lease semantics
# ===================================================================


class TestQueuedLeaseSemantics:
    """Verify that marking queued clears lease fields and that queued items
    are not claimable."""

    async def test_mark_queued_clears_lease_fields(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Transitioning in_progress -> queued clears all lease fields."""
        oid = await _create_in_progress(temp_storage, "plan-lease-clear")

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
        oid = await _create_in_progress(temp_storage, "plan-lease-noclaim")
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
        oid = await _create_in_progress(temp_storage, "plan-lease-q2s")
        await temp_storage.mark_outbox_queued(oid)
        await temp_storage.mark_outbox_sent(oid, receipt_id="rcpt-final")

        item = await temp_storage.get_outbox_item(oid)
        assert item is not None
        assert item.status == "sent"
        assert item.locked_at is None
        assert item.lease_until is None
        assert item.worker_id is None
        assert item.receipt_id == "rcpt-final"
