"""Queued rows sit outside claim and direct terminal-mark authority.

``queued`` is the durable confirmation-pending disposition. The claim
predicates never admit it even beside an expired-lease ``in_progress``
row that does reclaim; the direct dead-letter mark method refuses it (the
operator confirmation-pending finalizer is the permitted route); and the
abandonment-receipt exception is scoped to an explicit ``abandoned``
receipt — a ``sent`` receipt pointer can never drive the transition past
a live acceptance.
"""

from __future__ import annotations

from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_default_event,
    append_receipt_with_parent,
    make_outbox_item,
)

_STALE_NOW = "2026-01-01T00:00:00+00:00"
_LATER_NOW = "2026-01-01T01:00:00+00:00"


async def test_expired_in_progress_reclaims_while_queued_row_stays(
    temp_storage: SQLiteStorage,
) -> None:
    """One claim call reclaims the expired lease and leaves queued untouched."""
    await admit_default_event(temp_storage)
    expired = make_outbox_item(
        delivery_plan_id="plan-queued-guards-expired", status="pending"
    )
    await temp_storage.create_outbox_item(expired)
    claimed = await temp_storage.claim_due_outbox_items(_STALE_NOW, "worker-a")
    assert [c.outbox_id for c in claimed] == [expired.outbox_id]

    queued = make_outbox_item(
        delivery_plan_id="plan-queued-guards-queued", status="pending"
    )
    await temp_storage.create_outbox_item(queued)
    second = await temp_storage.claim_due_outbox_items(_STALE_NOW, "worker-a")
    assert [c.outbox_id for c in second] == [queued.outbox_id]
    assert await temp_storage.mark_outbox_queued(queued.outbox_id)

    reclaimed = await temp_storage.claim_due_outbox_items(_LATER_NOW, "worker-b")
    assert [c.outbox_id for c in reclaimed] == [expired.outbox_id]
    row = await temp_storage.get_outbox_item(queued.outbox_id)
    assert row is not None and row.status == "queued"
    assert row.worker_id is None and row.lease_until is None


async def test_dead_letter_mark_refuses_queued_row(
    temp_storage: SQLiteStorage,
) -> None:
    """mark_outbox_dead_lettered cannot transition queued work directly."""
    await admit_default_event(temp_storage)
    item = make_outbox_item(
        delivery_plan_id="plan-queued-guards-dead", status="pending"
    )
    await temp_storage.create_outbox_item(item)
    await temp_storage.claim_due_outbox_items(_STALE_NOW, "worker-a")
    assert await temp_storage.mark_outbox_queued(item.outbox_id)

    committed = await temp_storage.mark_outbox_dead_lettered(
        item.outbox_id,
        failure_kind="retry_exhausted",
        error_summary="direct mark must not rule queued work",
    )
    assert committed is False
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None and row.status == "queued"


async def test_abandonment_refuses_sent_receipt_pointer(
    temp_storage: SQLiteStorage,
) -> None:
    """A sent receipt pointer is not abandonment evidence for its attempt."""
    await admit_default_event(temp_storage)
    item = make_outbox_item(
        delivery_plan_id="plan-queued-guards-abandon", status="pending"
    )
    await temp_storage.create_outbox_item(item)
    claimed = await temp_storage.claim_due_outbox_items(_STALE_NOW, "worker-a")
    assert [c.outbox_id for c in claimed] == [item.outbox_id]

    sent = DeliveryReceipt(
        receipt_id="rcpt-sent-pointer",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        source="retry",
        status="sent",
    )
    await append_receipt_with_parent(temp_storage, sent)

    committed = await temp_storage.mark_outbox_abandoned(
        item.outbox_id,
        receipt_id="rcpt-sent-pointer",
        attempt_number=item.attempt_number,
        expected_worker_id="worker-a",
    )
    assert committed is False
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None and row.status == "in_progress"
    assert row.receipt_id is None
