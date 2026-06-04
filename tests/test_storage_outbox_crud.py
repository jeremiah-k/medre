"""Tests for delivery_outbox CRUD operations: create, get, list, count, and
persistence across restarts.

Includes idempotent create semantics (duplicate key returns existing).
"""

from __future__ import annotations

import os
import tempfile
import uuid

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item

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

    async def test_recreate_after_terminal_returns_existing_terminal_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After an item reaches a terminal status, creating a new item
        with the same key tuple returns the existing terminal row
        unchanged.  Terminal rows are immutable for lifecycle purposes;
        a new delivery after terminal state must use a new attempt
        identity (new ``delivery_plan_id`` and/or ``attempt_number``)
        so it does not collide with the terminal row's key."""
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

        # Re-create with same key tuple.  The terminal row is returned
        # unchanged; no new row is inserted.
        item2 = _make_outbox_item(
            delivery_plan_id="plan-recreate",
            target_channel="ch-r1",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == created1.outbox_id
        assert created2.status == "dead_lettered"

    async def test_recreate_after_sent_returns_existing_terminal_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After an item is marked sent (terminal), re-creation with the
        same key tuple returns the existing terminal row unchanged."""
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
        assert created2.outbox_id == created1.outbox_id
        assert created2.status == "sent"


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
        matching = [
            i for i in items if i.delivery_plan_id in {"plan-list-1", "plan-list-2"}
        ]
        assert len(matching) == 2

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
