"""The durable confirmation-pending disposition for queued-but-unconfirmed work.

A ``queued`` receipt records only adapter-local queue acceptance, so the
``queued`` outbox row is a durable, content-free confirmation-pending
disposition: reconciliation commits it without promoting completion, the
row is never claimable or re-dispatched, the acceptance fence still refuses
abandoning it, the deadline sweep never resolves it, and a queued retry
receipt finalizes as the third observability outcome — never delivery
success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptFinalization,
)
from medre.core.events import DeliveryReceipt
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.handoff_resolution import _seed_reserved_claim
from tests.helpers.storage_outbox import append_receipt_with_parent, make_outbox_item

_SERVICE = DeliveryLifecycleService()

_EVENT = "evt-confirmation-pending"


def _queued_receipt(
    provenance, *, receipt_id: str = "queued-acceptance"
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        source=provenance.source,
        status="queued",
    )


async def _seed_pending_row(storage: SQLiteStorage, provenance) -> None:
    """Append the queued acceptance receipt to a seeded reserved claim."""
    await append_receipt_with_parent(
        storage, _queued_receipt(provenance, receipt_id="queued-acceptance")
    )


async def test_reconciled_queued_receipt_commits_the_pending_disposition(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    await _seed_pending_row(temp_storage, provenance)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None

    finalization = await _SERVICE.settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[_queued_receipt(provenance)]
    )

    assert finalization.outcome == "queued"
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None
    # The disposition is durable, content-free, and not a completion: the
    # row rests in ``queued`` with the acceptance receipt pointer, no
    # failure fact, and no claim ownership.
    assert after.status == "queued"
    assert after.receipt_id == "queued-acceptance"
    assert after.failure_kind is None
    assert after.active_attempt is None
    assert after.worker_id is None and after.lease_until is None


async def test_confirmation_pending_row_is_never_claimable(
    temp_storage: SQLiteStorage,
) -> None:
    """A stale confirmation-pending row is never claimed or re-dispatched."""
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    await _seed_pending_row(temp_storage, provenance)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    await _SERVICE.settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[_queued_receipt(provenance)]
    )
    # Age the row far past any staleness threshold.
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await temp_storage._write(
        "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
        (stale, stored.outbox_id),
    )

    claimed = await temp_storage.claim_due_outbox_items(
        now=datetime.now(timezone.utc).isoformat(),
        worker_id="later-worker",
        lease_seconds=30,
        limit=10,
    )

    assert not any(c.outbox_id == stored.outbox_id for c in claimed)
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "queued"
    assert after.worker_id is None and after.attempt_number == 1


async def test_maintenance_wake_does_not_dispatch_the_pending_row(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A retry scan over a stale confirmation-pending row dispatches nothing."""
    from medre.core.engine.pipeline.runner import PipelineRunner

    target = "fake_presentation"
    worker = RetryWorker(
        outbox_temp_storage,
        cast(PipelineRunner, object()),
        None,
        enabled=True,
        initial_dispatch=False,
        available_target_adapters=[target],
    )
    stored = await outbox_temp_storage.create_outbox_item(
        make_outbox_item(target_adapter=target)
    )
    assert stored.updated_at is not None
    claimed = await outbox_temp_storage.claim_due_outbox_items(
        stored.updated_at, "first-worker"
    )
    assert [c.outbox_id for c in claimed] == [stored.outbox_id]
    assert await outbox_temp_storage.mark_outbox_queued(stored.outbox_id)
    # Age the row past any staleness threshold: the wake must still skip it.
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await outbox_temp_storage._write(
        "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
        (stale, stored.outbox_id),
    )
    receipts_before = await outbox_temp_storage.list_receipts_for_outbox(
        stored.outbox_id
    )

    await worker._process_due(datetime.now(timezone.utc))

    after = await outbox_temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None
    assert after.status == "queued"
    assert after.attempt_number == 1 and after.active_attempt is None
    assert after.worker_id is None and after.lease_until is None
    receipts_after = await outbox_temp_storage.list_receipts_for_outbox(
        stored.outbox_id
    )
    assert receipts_after == receipts_before


async def test_acceptance_fence_still_refuses_abandoning_the_pending_row(
    temp_storage: SQLiteStorage,
) -> None:
    """A queued admission on the effective attempt refuses fresh abandonment."""
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    await _seed_pending_row(temp_storage, provenance)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    await _SERVICE.settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[_queued_receipt(provenance)]
    )

    assert not await temp_storage.mark_outbox_abandoned(stored.outbox_id)

    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "queued"


async def test_deadline_sweep_never_resolves_the_pending_row(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    await _seed_pending_row(temp_storage, provenance)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    await _SERVICE.settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[_queued_receipt(provenance)]
    )

    retired = await temp_storage.retire_expired_unstarted_outbox(
        now=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    assert retired == []
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "queued"


async def test_queued_retry_evidence_yields_the_third_outcome(
    temp_storage: SQLiteStorage,
) -> None:
    """A queued receipt finalizes as ``queued``, never delivery success."""
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    await _seed_pending_row(temp_storage, provenance)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None

    finalization = await _SERVICE._finalize_retry_evidence(
        temp_storage,
        item,
        RetryPolicy(max_attempts=3),
        evidence=_queued_receipt(provenance),
        unpersisted_failure_kind=None,
        unpersisted_error_summary=None,
        attempt_number=1,
    )

    assert isinstance(finalization, RetryAttemptFinalization)
    assert finalization.outcome == "queued"
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "queued"


async def test_sent_retry_evidence_still_yields_acceptance(
    temp_storage: SQLiteStorage,
) -> None:
    """A provenance-matched ``sent`` receipt still finalizes as accepted."""
    stored, provenance = await _seed_reserved_claim(temp_storage, event_id=_EVENT)
    receipt = DeliveryReceipt(
        receipt_id="sent-proof",
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        source=provenance.source,
        status="sent",
    )
    await append_receipt_with_parent(temp_storage, receipt)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None

    finalization = await _SERVICE._finalize_retry_evidence(
        temp_storage,
        item,
        RetryPolicy(max_attempts=3),
        evidence=receipt,
        unpersisted_failure_kind=None,
        unpersisted_error_summary=None,
        attempt_number=1,
    )

    assert finalization.outcome == "accepted"
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "sent"


@pytest.mark.parametrize(
    ("source_status", "target"),
    [("queued", "sent"), ("queued", "cancelled"), ("queued", "abandoned")],
)
async def test_pending_row_exits_only_through_legal_transitions(
    source_status: str,
    target: str,
) -> None:
    from medre.core.engine.pipeline.delivery_state import (
        OUTBOX_TRANSITIONS,
        is_claimable_outbox_status,
    )

    assert not is_claimable_outbox_status(source_status)
    allowed = OUTBOX_TRANSITIONS[source_status]
    assert "in_progress" not in allowed
    assert target in allowed
