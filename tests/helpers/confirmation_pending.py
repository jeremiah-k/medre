"""Shared seeding for the confirmation-pending recovery test modules.

Both recovery modules exercise the same durable ``queued`` disposition —
the confirmation-pending outbox row — from different surfaces: the
storage authority directly, and the POSIX-gated CLI mirror. This helper
owns the delivery-identity constants, the receipt builder, and the
queued-row seeding that both modules use; everything else stays private
to each test module.
"""

from __future__ import annotations

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite._operator_decision import CONFIRMATION_PENDING_STATUS
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_event,
    append_receipt_with_parent,
    make_outbox_item,
)

PLAN_ID = "plan-confirmation-pending"
TARGET_ADAPTER = "matrix"
TARGET_CHANNEL = "!pending"
EVENT_ID = "evt-confirmation-pending"


def build_receipt(
    stored: DeliveryOutboxItem,
    status: str,
    *,
    receipt_id: str = "queued-acceptance",
    target_channel: str | None = None,
) -> DeliveryReceipt:
    """Build the delivery receipt for a seeded confirmation-pending row."""
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=stored.event_id,
        delivery_plan_id=stored.delivery_plan_id,
        target_adapter=stored.target_adapter,
        target_channel=target_channel or stored.target_channel,
        outbox_id=stored.outbox_id,
        attempt_number=stored.attempt_number,
        source="live",
        status=status,
    )


async def seed_queued_row(
    storage: SQLiteStorage,
    *,
    event_id: str = EVENT_ID,
    plan_id: str = PLAN_ID,
) -> tuple[DeliveryOutboxItem, DeliveryOutboxItem]:
    """Create, claim, and queue one row; append its queued receipt."""
    await admit_event(storage, event_id)
    stored = await storage.create_outbox_item(
        make_outbox_item(
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter=TARGET_ADAPTER,
            target_channel=TARGET_CHANNEL,
        )
    )
    claimed = await storage.claim_due_outbox_items(stored.updated_at, "first-worker")
    assert [c.outbox_id for c in claimed] == [stored.outbox_id]
    receipt = build_receipt(stored, "queued")
    await append_receipt_with_parent(storage, receipt)
    assert await storage.mark_outbox_queued(
        stored.outbox_id, receipt_id=receipt.receipt_id
    )
    row = await storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == CONFIRMATION_PENDING_STATUS
    return stored, row
