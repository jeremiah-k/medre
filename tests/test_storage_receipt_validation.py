"""Boundary validation for malformed delivery-receipt model objects."""

from __future__ import annotations

import pytest
from msgspec.structs import force_setattr

from medre.core.events import DeliveryReceipt
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
