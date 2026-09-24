"""Boundary validation for malformed delivery-receipt model objects."""

from __future__ import annotations

import pytest
from msgspec.structs import force_setattr

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


class TestUnknownReceiptStatusRejected:
    """append_receipt raises ValueError for unknown receipt statuses
    and does not append a row."""

    async def test_unknown_receipt_status_raises_value_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Passing an unknown status to append_receipt raises ValueError."""
        event = make_storage_event(event_id="evt-unknown-rcpt")
        await temp_storage.append(event)

        with pytest.raises(ValueError, match="Unknown delivery receipt status"):
            DeliveryReceipt(
                receipt_id="rcpt-bad-status",
                event_id="evt-unknown-rcpt",
                delivery_plan_id="plan-bad-status",
                target_adapter="adapter_bad",
                status="not_a_real_status",  # type: ignore[arg-type]
            )

    async def test_unknown_receipt_status_does_not_append_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After a ValueError for unknown receipt status, no row exists."""
        event = make_storage_event(event_id="evt-unknown-row")
        await temp_storage.append(event)

        # Count receipts before.
        rows_before = await temp_storage._read_all(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
            ("evt-unknown-row",),
        )
        count_before = rows_before[0]["cnt"]

        receipt = DeliveryReceipt(
            receipt_id="rcpt-no-row",
            event_id="evt-unknown-row",
            delivery_plan_id="plan-no-row",
            target_adapter="adapter_no",
            status="sent",
        )
        force_setattr(receipt, "status", "totally_invalid")

        with pytest.raises(ValueError, match="Unknown receipt status"):
            await temp_storage.append_receipt(receipt)

        # Count receipts after — must be unchanged.
        rows_after = await temp_storage._read_all(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
            ("evt-unknown-row",),
        )
        count_after = rows_after[0]["cnt"]
        assert count_after == count_before

    async def test_unknown_receipt_kind_does_not_append_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storage revalidates receipt kind even for corrupted model objects."""
        event = make_storage_event(event_id="evt-unknown-kind-row")
        await temp_storage.append(event)
        receipt = DeliveryReceipt(
            receipt_id="rcpt-bad-kind",
            event_id=event.event_id,
            delivery_plan_id="plan-bad-kind",
            target_adapter="adapter_no",
            status="sent",
        )
        force_setattr(receipt, "receipt_kind", "bogus")

        with pytest.raises(ValueError, match="Unknown receipt kind"):
            await temp_storage.append_receipt(receipt)

        rows = await temp_storage._read_all(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
            (event.event_id,),
        )
        assert rows[0]["cnt"] == 0


class TestDeliveryProvenanceValidation:
    """Receipt provenance is canonical at model, storage, and SQLite boundaries."""

    def test_receipt_rejects_unknown_source(self) -> None:
        with pytest.raises(ValueError, match="unknown delivery source"):
            DeliveryReceipt(
                receipt_id="rcpt-unknown-source",
                event_id="evt-unknown-source",
                delivery_plan_id="plan-unknown-source",
                target_adapter="adapter",
                status="sent",
                source="bogus",  # type: ignore[arg-type]
            )

    def test_receipt_normalizes_replay_run_id(self) -> None:
        receipt = DeliveryReceipt(
            receipt_id="rcpt-normalized-run",
            event_id="evt-normalized-run",
            delivery_plan_id="plan-normalized-run",
            target_adapter="adapter",
            status="sent",
            source="replay",
            replay_run_id="  run-42  ",
        )
        assert receipt.replay_run_id == "run-42"

    def test_live_receipt_rejects_replay_origin(self) -> None:
        with pytest.raises(ValueError, match="live delivery cannot carry replay_run_id"):
            DeliveryReceipt(
                receipt_id="rcpt-live-replay-origin",
                event_id="evt-live-replay-origin",
                delivery_plan_id="plan-live-replay-origin",
                target_adapter="adapter",
                status="sent",
                source="live",
                replay_run_id="run-impossible",
            )

    def test_retry_receipt_accepts_replay_origin(self) -> None:
        receipt = DeliveryReceipt(
            receipt_id="rcpt-retry-replay-origin",
            event_id="evt-retry-replay-origin",
            delivery_plan_id="plan-retry-replay-origin",
            target_adapter="adapter",
            status="sent",
            source="retry",
            replay_run_id="run-origin",
        )
        assert receipt.source == "retry"
        assert receipt.replay_run_id == "run-origin"

    async def test_storage_rejects_force_mutated_unknown_source(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-bad-source-row")
        await temp_storage.append(event)
        receipt = DeliveryReceipt(
            receipt_id="rcpt-bad-source",
            event_id=event.event_id,
            delivery_plan_id="plan-bad-source",
            target_adapter="adapter",
            status="sent",
        )
        force_setattr(receipt, "source", "bogus")

        with pytest.raises(ValueError, match="unknown delivery source"):
            await temp_storage.append_receipt(receipt)

    async def test_storage_rejects_force_mutated_live_replay_origin(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-bad-live-origin-row")
        await temp_storage.append(event)
        receipt = DeliveryReceipt(
            receipt_id="rcpt-bad-live-origin",
            event_id=event.event_id,
            delivery_plan_id="plan-bad-live-origin",
            target_adapter="adapter",
            status="sent",
        )
        force_setattr(receipt, "replay_run_id", "run-impossible")

        with pytest.raises(ValueError, match="live delivery cannot carry replay_run_id"):
            await temp_storage.append_receipt(receipt)


    async def test_sqlite_rejects_live_receipt_with_replay_origin(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-sql-live-origin")
        await temp_storage.append(event)

        with pytest.raises(StorageError, match="CHECK constraint failed"):
            await temp_storage._write(
                """
                INSERT INTO delivery_receipts
                    (receipt_id, event_id, delivery_plan_id, target_adapter, status,
                     receipt_kind, source, replay_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rcpt-sql-live-origin",
                    event.event_id,
                    "plan-sql-live-origin",
                    "adapter",
                    "sent",
                    "attempt",
                    "live",
                    "run-impossible",
                    event.timestamp.isoformat(),
                ),
            )

    async def test_sqlite_rejects_noncanonical_replay_run_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-sql-run-whitespace")
        await temp_storage.append(event)

        with pytest.raises(StorageError, match="CHECK constraint failed"):
            await temp_storage._write(
                """
                INSERT INTO delivery_receipts
                    (receipt_id, event_id, delivery_plan_id, target_adapter, status,
                     receipt_kind, source, replay_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rcpt-sql-run-whitespace",
                    event.event_id,
                    "plan-sql-run-whitespace",
                    "adapter",
                    "sent",
                    "attempt",
                    "replay",
                    "  run-not-canonical  ",
                    event.timestamp.isoformat(),
                ),
            )
