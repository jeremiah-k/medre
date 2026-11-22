"""The operator ruling's terminal settlement deletes the frozen snapshot.

``get_outbox_dispatch_context`` returns ``None`` for a terminal row even
when a snapshot row remains in SQLite, so the retirement duty is checked
directly against ``delivery_outbox_snapshots`` around a ``failed`` ruling.
"""

from __future__ import annotations

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite._operator_decision import (
    CONFIRMATION_PENDING_STATUS,
    ConfirmationPendingResolution,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_event,
    append_receipt_with_parent,
    make_outbox_item,
)

_EVENT = "evt-ruling-snapshot"
_PLAN = "plan-ruling-snapshot"
_ADAPTER = "matrix"
_CHANNEL = "!ruling-snapshot"


def _receipt(stored, status: str) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id="rcpt-queued-snapshot",
        event_id=stored.event_id,
        delivery_plan_id=stored.delivery_plan_id,
        target_adapter=stored.target_adapter,
        target_channel=stored.target_channel,
        outbox_id=stored.outbox_id,
        attempt_number=stored.attempt_number,
        source="live",
        status=status,
    )


def _failed_decision(stored, row) -> ConfirmationPendingResolution:
    updated_at = (
        row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else str(row.updated_at)
    )
    return ConfirmationPendingResolution(
        decision_id="decision-snapshot-retired",
        actor_id="local-operator",
        identity=DeliveryIdentity(
            stored.event_id,
            stored.delivery_plan_id,
            stored.target_adapter,
            stored.target_channel,
        ),
        outbox_id=stored.outbox_id,
        expected_generation=row.attempt_number,
        expected_status=CONFIRMATION_PENDING_STATUS,
        expected_updated_at=updated_at,
        resolution="failed",
    )


async def test_failed_ruling_deletes_the_dispatch_snapshot(
    temp_storage: SQLiteStorage,
) -> None:
    """A committed ruling removes the snapshot row in the same settlement."""
    await admit_event(temp_storage, _EVENT)
    stored = await temp_storage.create_outbox_item(
        make_outbox_item(
            event_id=_EVENT,
            delivery_plan_id=_PLAN,
            target_adapter=_ADAPTER,
            target_channel=_CHANNEL,
        )
    )
    claimed = await temp_storage.claim_due_outbox_items(stored.updated_at, "claimer")
    assert [c.outbox_id for c in claimed] == [stored.outbox_id]
    receipt = _receipt(stored, "queued")
    await append_receipt_with_parent(temp_storage, receipt)
    assert await temp_storage.mark_outbox_queued(
        stored.outbox_id, receipt_id=receipt.receipt_id
    )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == CONFIRMATION_PENDING_STATUS

    await temp_storage._write(
        "INSERT INTO delivery_outbox_snapshots "
        "(outbox_id, attempt_number, snapshot_version, snapshot_kind, "
        "context_json, created_at) VALUES (?, ?, 1, 'direct', '{}', ?)",
        (stored.outbox_id, stored.attempt_number, "2026-01-01T00:00:00+00:00"),
    )
    before = await temp_storage._read_one(
        "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id = ?",
        (stored.outbox_id,),
    )
    assert before is not None

    committed = await temp_storage.resolve_confirmation_pending(
        _failed_decision(stored, row)
    )
    assert committed is True

    remaining = await temp_storage._read_one(
        "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id = ?",
        (stored.outbox_id,),
    )
    assert remaining is None
