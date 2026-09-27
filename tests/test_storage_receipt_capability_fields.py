"""Tests for SQLiteStorage: structured capability decision fields on
delivery receipts and their persistence round-trip through delivery_status.

Split from test_storage_receipts.py, which sits at the test-suite line
ceiling; new receipt-field coverage lands here.
"""

from __future__ import annotations

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_capability_decision_fields_round_trip(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_storage_event(event_id="evt-capability-fields")
    await temp_storage.append(event)
    receipt = DeliveryReceipt(
        receipt_id="rcpt-capability-fields",
        event_id=event.event_id,
        delivery_plan_id="plan-capability-fields",
        target_adapter="matrix",
        status="sent",
        capability_level="fallback",
        capability_field="replies",
        capability_reason="native reply unavailable",
        delivery_strategy="fallback_text",
    )
    await temp_storage.append_receipt(receipt)

    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert len(receipts) == 1
    stored = receipts[0]
    assert stored.capability_level == "fallback"
    assert stored.capability_field == "replies"
    assert stored.capability_reason == "native reply unavailable"
    assert stored.delivery_strategy == "fallback_text"

    status = await temp_storage.delivery_status(
        DeliveryIdentity(event.event_id, "plan-capability-fields", "matrix", None)
    )
    assert status is not None
    assert status.capability_level == "fallback"
    assert status.capability_field == "replies"
    assert status.capability_reason == "native reply unavailable"
    assert status.delivery_strategy == "fallback_text"
