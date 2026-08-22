"""Focused branch coverage for storage/sqlite/_outbox.py."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item as _make_outbox_item

# ===================================================================
# Lines 72-73: ValueError on disallowed initial status
# ===================================================================


class TestCreateOutboxRejectsDisallowedStatus:
    """create_outbox_item raises ValueError for statuses other than
    pending / in_progress."""

    @pytest.mark.parametrize(
        "status",
        ["queued", "sent", "retry_wait", "dead_lettered", "cancelled", "abandoned"],
    )
    async def test_disallowed_status_raises_value_error(
        self, temp_storage: SQLiteStorage, status: str
    ) -> None:
        item = _make_outbox_item(status=status)
        with pytest.raises(ValueError, match="does not permit initial status"):
            await temp_storage.create_outbox_item(item)


# ===================================================================
# Existing reclaimable rows
# ===================================================================


class TestExistingReclaimableRow:
    """Existing pending/retry_wait rows are reclaimed in place."""

    async def test_existing_pending_row_reclaimed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Second create with status=in_progress reclaims a pending row."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-cov-135",
            target_channel="ch-cov-135",
        )
        await temp_storage.create_outbox_item(item1)

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-cov-135",
            target_adapter="fake_presentation",
            target_channel="ch-cov-135",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:cov135",
            locked_at="2026-01-01T00:00:00",
            lease_until="2026-01-01T00:01:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == item1.outbox_id
        assert created2.status == "in_progress"
        assert created2.worker_id == "pipeline:cov135"

    async def test_existing_retry_wait_row_reclaimed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Second create reclaims a retry_wait row (claimable status)."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-cov-135rw",
            target_channel="ch-cov-135rw",
        )
        await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="w1",
            lease_seconds=30,
            limit=10,
        )
        assert len(claimed) == 1
        await temp_storage.mark_outbox_retry_wait(
            claimed[0].outbox_id,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
        )

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-cov-135rw",
            target_adapter="fake_presentation",
            target_channel="ch-cov-135rw",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:cov135rw",
            locked_at="2026-01-01T00:00:00",
            lease_until="2026-01-01T00:01:00",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == claimed[0].outbox_id
        assert created2.status == "in_progress"
        assert created2.worker_id == "pipeline:cov135rw"
        assert created2.next_attempt_at is None


# ===================================================================
# Existing active/terminal rows
# ===================================================================


class TestExistingNonReclaimableRow:
    """Existing active or terminal rows are returned unchanged."""

    async def test_terminal_row_returned_unchanged(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Sent (terminal) row is returned unchanged on re-create."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-cov-170",
            target_channel="ch-cov-170",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="w1",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) == 1
        await temp_storage.mark_outbox_sent(created1.outbox_id, receipt_id="rcpt-170")

        item2 = _make_outbox_item(
            delivery_plan_id="plan-cov-170",
            target_channel="ch-cov-170",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.outbox_id == created1.outbox_id
        assert created2.status == "sent"

    async def test_in_progress_row_returned_unchanged(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Active in_progress row is not stolen on re-create."""
        item1 = _make_outbox_item(
            delivery_plan_id="plan-cov-170ip",
            target_channel="ch-cov-170ip",
        )
        await temp_storage.create_outbox_item(item1)
        claimed = await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00",
            worker_id="worker-original",
            lease_seconds=300,
            limit=10,
        )
        assert len(claimed) == 1

        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item1.event_id,
            route_id=item1.route_id,
            delivery_plan_id="plan-cov-170ip",
            target_adapter="fake_presentation",
            target_channel="ch-cov-170ip",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:new",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        assert created2.worker_id == "worker-original"


# ===================================================================
# UNIQUE race fallback
# ===================================================================


class TestIntegrityErrorHandler:
    """A UNIQUE race re-reads and returns the winning row."""

    async def test_integrity_error_returns_existing_row(
        self, temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = _make_outbox_item(
            delivery_plan_id="plan-cov-ie",
            target_channel="ch-cov-ie",
        )
        created = await temp_storage.create_outbox_item(item)
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item.event_id,
            route_id=item.route_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
            attempt_number=1,
        )

        def _raise_unique(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.IntegrityError("UNIQUE constraint failed")

        monkeypatch.setattr(temp_storage, "_sync_atomic_create_outbox", _raise_unique)
        result = await temp_storage.create_outbox_item(item2)

        assert result.outbox_id == created.outbox_id
