"""Retry/recovery outbox integration tests: due outbox retry, retry exhaustion,
restart visibility, and Meshtastic ambiguous items.
"""

from __future__ import annotations

import uuid

from medre.core.storage import DeliveryOutboxItem, SQLiteStorage


def _make_outbox_item(
    delivery_plan_id: str = "plan-retry-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    status: str = "retry_wait",
    next_attempt_at: str | None = None,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Build a minimal outbox item for retry tests."""
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id="evt-retry-1",
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


class TestDueOutboxRetry:
    """Due outbox items with status retry_wait should be claimable."""

    async def test_due_retry_wait_item_is_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item past its next_attempt_at should be claimable."""
        item = _make_outbox_item(
            delivery_plan_id="plan-due-1",
            status="retry_wait",
            next_attempt_at="2025-01-01T00:00:00",
        )
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        assert claimed[0].outbox_id == item.outbox_id
        assert claimed[0].status == "in_progress"

    async def test_not_due_retry_wait_not_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item NOT past its next_attempt_at should NOT be claimable."""
        future = "2099-01-01T00:00:00"
        item = _make_outbox_item(
            delivery_plan_id="plan-not-due-1",
            status="retry_wait",
            next_attempt_at=future,
        )
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 0

    async def test_mark_retry_wait_with_next_attempt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Claim an item, then mark it retry_wait with a next_attempt_at."""
        item = _make_outbox_item(
            delivery_plan_id="plan-mark-retry-1",
            status="pending",
        )
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        next_at = "2026-01-01T01:00:00"
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at=next_at,
            failure_kind="adapter_transient",
            error_summary="Connection timeout",
        )

        item_after = await temp_storage.get_outbox_item(oid)
        assert item_after is not None
        assert item_after.status == "retry_wait"
        assert item_after.next_attempt_at == next_at
        assert item_after.failure_kind == "adapter_transient"


class TestRetryExhaustion:
    """When retries are exhausted, the outbox should be dead_lettered."""

    async def test_dead_lettered_from_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item can be marked dead_lettered."""
        item = _make_outbox_item(
            delivery_plan_id="plan-exhaust-1",
            status="retry_wait",
            next_attempt_at="2025-01-01T00:00:00",
            attempt_number=3,
        )
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        await temp_storage.mark_outbox_dead_lettered(
            oid,
            failure_kind="adapter_permanent",
            error_summary="All 3 retry attempts exhausted",
        )

        dl = await temp_storage.get_outbox_item(oid)
        assert dl is not None
        assert dl.status == "dead_lettered"
        assert dl.locked_at is None  # terminal clears lease


class TestRestartVisibility:
    """After restart (close/reopen), outbox items remain visible."""

    async def test_pending_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        item = _make_outbox_item(
            delivery_plan_id="plan-restart-pending",
            status="pending",
        )
        await storage.create_outbox_item(item)
        await storage.close()

        # Re-open
        storage2 = SQLiteStorage(db_path=db_path)
        await storage2.initialize()

        items = await storage2.list_outbox_items(status_filter=["pending"])
        matching = [i for i in items if i.delivery_plan_id == "plan-restart-pending"]
        assert len(matching) == 1
        assert matching[0].status == "pending"
        await storage2.close()

        os.unlink(db_path)

    async def test_due_retry_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        item = _make_outbox_item(
            delivery_plan_id="plan-restart-due",
            status="retry_wait",
            next_attempt_at="2025-01-01T00:00:00",
        )
        await storage.create_outbox_item(item)
        await storage.close()

        # Re-open
        storage2 = SQLiteStorage(db_path=db_path)
        await storage2.initialize()

        # Should still be visible and claimable.
        now = "2026-01-01T00:00:00"
        claimed = await storage2.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) >= 1
        matching = [c for c in claimed if c.delivery_plan_id == "plan-restart-due"]
        assert len(matching) == 1
        await storage2.close()

        os.unlink(db_path)

    async def test_dead_lettered_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        item = _make_outbox_item(
            delivery_plan_id="plan-restart-dl",
            status="dead_lettered",
        )
        await storage.create_outbox_item(item)
        await storage.close()

        # Re-open
        storage2 = SQLiteStorage(db_path=db_path)
        await storage2.initialize()

        items = await storage2.list_outbox_items(status_filter=["dead_lettered"])
        matching = [i for i in items if i.delivery_plan_id == "plan-restart-dl"]
        assert len(matching) == 1
        assert matching[0].status == "dead_lettered"
        await storage2.close()

        os.unlink(db_path)

    async def test_ambiguous_meshtastic_after_restart(self) -> None:
        """A Meshtastic-queued item after restart should remain visible
        as 'queued' (the pipeline recorded queue acceptance).  Recovery
        must decide whether to re-send or abandon."""
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        item = _make_outbox_item(
            delivery_plan_id="plan-ambiguous-msh",
            status="queued",
            target_adapter="meshtastic",
        )
        await storage.create_outbox_item(item)
        await storage.close()

        # Re-open: queued item is visible.
        storage2 = SQLiteStorage(db_path=db_path)
        await storage2.initialize()

        items = await storage2.list_outbox_items(status_filter=["queued"])
        matching = [i for i in items if i.delivery_plan_id == "plan-ambiguous-msh"]
        assert len(matching) == 1
        assert matching[0].status == "queued"
        # Not claimable (status != pending/retry_wait).
        now = "2026-01-01T00:00:00"
        claimed = await storage2.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert not any(c.delivery_plan_id == "plan-ambiguous-msh" for c in claimed)
        await storage2.close()

        os.unlink(db_path)
