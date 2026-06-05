"""Tests covering uncovered branches in storage/sqlite/_outbox.py.

Lines 72-73: ValueError on disallowed initial status.
Lines 135-137: aiosqlite branch - existing reclaimable row found.
Lines 170-174: aiosqlite branch - non-reclaimable row returned + IntegrityError.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import NoReturn

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
# Lines 135-137: aiosqlite existing reclaimable row
# ===================================================================


class TestAiosqliteExistingReclaimableRow:
    """In the aiosqlite branch, finding an existing reclaimable row (pending
    or retry_wait) triggers reclaim rather than returning unchanged."""

    async def test_existing_pending_row_reclaimed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Second create with status=in_progress reclaims a pending row."""
        if not temp_storage._use_aiosqlite:
            pytest.skip("aiosqlite not available")

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
        if not temp_storage._use_aiosqlite:
            pytest.skip("aiosqlite not available")

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
# Lines 170-171: aiosqlite non-reclaimable row returned unchanged
# ===================================================================


class TestAiosqliteExistingNonReclaimableRow:
    """In the aiosqlite branch, an existing non-reclaimable row (terminal or
    active) is returned unchanged via the COMMIT + return path."""

    async def test_terminal_row_returned_unchanged(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Sent (terminal) row is returned unchanged on re-create."""
        if not temp_storage._use_aiosqlite:
            pytest.skip("aiosqlite not available")

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
        if not temp_storage._use_aiosqlite:
            pytest.skip("aiosqlite not available")

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
# Lines 172-174: aiosqlite INSERT + IntegrityError handler
# ===================================================================


class TestAiosqliteIntegrityErrorHandler:
    """Exercise the aiosqlite IntegrityError handler (UNIQUE race)."""

    async def test_integrity_error_returns_existing_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When INSERT fails with IntegrityError, the winning row is returned."""
        if not temp_storage._use_aiosqlite:
            pytest.skip("aiosqlite not available")

        # Create the winning row.
        item = _make_outbox_item(
            delivery_plan_id="plan-cov-ie",
            target_channel="ch-cov-ie",
        )
        created = await temp_storage.create_outbox_item(item)

        # Build a second item with same key but different outbox_id.
        item2 = DeliveryOutboxItem(
            outbox_id=f"obox-{uuid.uuid4()}",
            event_id=item.event_id,
            route_id=item.route_id,
            delivery_plan_id="plan-cov-ie",
            target_adapter="fake_presentation",
            target_channel="ch-cov-ie",
            attempt_number=1,
        )

        db = temp_storage._require_db()
        real_execute = db.execute
        _insert_attempted = [False]

        class _NoRowCursor:
            """Mock aiosqlite cursor: fetchone returns None, supports async with."""

            async def fetchone(self) -> None:
                return None

            async def __aenter__(self) -> _NoRowCursor:
                return self

            async def __aexit__(self, *a: object) -> None:
                pass

            def __await__(self):  # type: ignore[override]
                return self.__aenter__().__await__()

        class _InsertErrorCursor:
            """Mock aiosqlite cursor that raises IntegrityError when awaited."""

            def __await__(self):  # type: ignore[override]
                return self._raise().__await__()

            async def _raise(self) -> NoReturn:
                raise sqlite3.IntegrityError("UNIQUE constraint violation")

        def _patched_execute(sql, params=None):
            # aiosqlite db.execute() returns a cursor synchronously.
            # INSERT → raise IntegrityError to simulate UNIQUE race.
            if sql.strip().startswith("INSERT INTO delivery_outbox"):
                _insert_attempted[0] = True
                return _InsertErrorCursor()
            # In-transaction SELECT → pretend no row exists so we reach INSERT.
            if "SELECT outbox_id, status FROM delivery_outbox" in sql:
                if not _insert_attempted[0]:
                    return _NoRowCursor()
            # Everything else (BEGIN, COMMIT, ROLLBACK, re-read SELECT) → real.
            return real_execute(sql, params)

        db.execute = _patched_execute
        try:
            result = await temp_storage.create_outbox_item(item2)
        finally:
            db.execute = real_execute

        assert _insert_attempted[0], "INSERT was never attempted"
        assert result.outbox_id == created.outbox_id
