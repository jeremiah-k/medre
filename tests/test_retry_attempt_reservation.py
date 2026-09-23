"""Durable retry attempt reservation and its callback-admission contract.

The retry worker reserves the next attempt identity on a claimed outbox row
before invoking the transport.  From that commit onward the reserved number
is the live attempt for every callback validator, and earlier attempts are
stale.  These tests pin the reservation CAS, both sides of the claim window,
lease recovery after crashes at each persistence boundary, and the rule that
a deferral before dispatch consumes no attempt.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import medre.runtime.retry as retry_module
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    OutboundDeliveryObservationRecord,
    OutboundNativeRefRecord,
    QueueTerminalRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
)
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.retry import RetryWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PAST = datetime.now(timezone.utc) - timedelta(seconds=120)
_FUTURE = datetime.now(timezone.utc) + timedelta(seconds=300)
_PLAN_ID = "plan-reservation"
_WORKER = "retry-worker-test"


async def _seed_event(storage, event_id: str) -> CanonicalEvent:
    event = CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="src",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "reservation test"},
        metadata=EventMetadata(),
    )
    await storage.append(event)
    return event


async def _seed_outbox(
    storage,
    *,
    outbox_id: str,
    event_id: str,
    status: str = "in_progress",
    worker_id: str | None = _WORKER,
    attempt_number: int = 1,
    target_channel: str | None = "aa" * 16,
) -> DeliveryOutboxItem:
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-reservation",
        delivery_plan_id=_PLAN_ID,
        target_adapter="lxmf-main",
        target_channel=target_channel,
        attempt_number=attempt_number,
        status="in_progress",
        locked_at=_PAST.isoformat(),
        lease_until=_PAST.isoformat(),
        worker_id=worker_id,
        metadata={
            "capability_level": None,
            "delivery_strategy": "direct",
            "capability_field": None,
            "capability_reason": None,
            "deadline": None,
        },
    )
    await storage.create_outbox_item(item)
    if status == "retry_wait":
        await storage.mark_outbox_retry_wait(
            outbox_id,
            next_attempt_at=_PAST.isoformat(),
            failure_kind="adapter_transient",
            error_summary="seeded failure",
            attempt_number=attempt_number,
        )
    return item


async def _append_receipt(
    storage,
    *,
    outbox_id: str,
    event_id: str,
    status: str,
    attempt_number: int,
) -> DeliveryReceipt:
    receipt = DeliveryReceipt(
        receipt_id=f"rcpt-{outbox_id}-a{attempt_number}-{status}",
        event_id=event_id,
        delivery_plan_id=_PLAN_ID,
        target_adapter="lxmf-main",
        target_channel="aa" * 16,
        route_id="route-reservation",
        status=status,
        failure_kind="adapter_transient" if status == "failed" else None,
        error="seeded attempt failure" if status == "failed" else None,
        attempt_number=attempt_number,
        source="retry",
        outbox_id=outbox_id,
        created_at=datetime.now(timezone.utc),
    )
    await storage.append_receipt(receipt)
    return receipt


def _observation_record(
    *,
    event_id: str,
    outbox_id: str,
    attempt_number: int,
    state: str = "delivered",
) -> OutboundDeliveryObservationRecord:
    return OutboundDeliveryObservationRecord(
        event_id=event_id,
        adapter="lxmf-main",
        state=state,  # type: ignore[arg-type]
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        delivery_plan_id=_PLAN_ID,
        native_channel_id="aa" * 16,
        native_message_id=f"{outbox_id}-a{attempt_number}",
        metadata={},
    )


async def _claim_due(storage, worker_id: str = _WORKER):
    return await storage.claim_due_outbox_items(
        now=datetime.now(timezone.utc).isoformat(),
        worker_id=worker_id,
    )


# ---------------------------------------------------------------------------
# Reservation CAS semantics
# ---------------------------------------------------------------------------


async def test_reservation_cas_guards_claim_ownership(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-cas")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-cas", event_id=event.event_id
    )
    lifecycle = DeliveryLifecycleService()

    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ]
    assert claimed, "seeded row must be claimable"
    claimed_item = claimed[0]

    reserved = await lifecycle.reserve_retry_attempt(temp_storage, claimed_item)
    assert reserved == 2

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.active_attempt == 2
    assert row.attempt_number == 1
    assert row.status == "in_progress"

    # A second reservation on the same row cannot advance further.
    assert await lifecycle.reserve_retry_attempt(temp_storage, claimed_item) is None

    # Stale snapshots (wrong base attempt or wrong worker) cannot reserve.
    stale_snapshot = DeliveryOutboxItem(
        **{**claimed_item.__dict__, "attempt_number": 5}
    )
    assert await lifecycle.reserve_retry_attempt(temp_storage, stale_snapshot) is None
    thief_snapshot = DeliveryOutboxItem(
        **{**claimed_item.__dict__, "worker_id": "other-worker"}
    )
    assert await lifecycle.reserve_retry_attempt(temp_storage, thief_snapshot) is None


async def test_reservation_requires_in_progress_row(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-not-in-progress")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-not-in-progress", event_id=event.event_id
    )
    lifecycle = DeliveryLifecycleService()

    # The row is in_progress with an expired lease but not yet re-claimed;
    # a retry_wait row cannot be reserved either.
    await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        next_attempt_at=_PAST.isoformat(),
        failure_kind="adapter_transient",
    )
    snapshot = await temp_storage.get_outbox_item(item.outbox_id)
    assert snapshot is not None
    assert await lifecycle.reserve_retry_attempt(temp_storage, snapshot) is None

    # Exercise the SQLite status predicate directly.  A matching worker ID is
    # not sufficient when the row is not in_progress.
    pending = DeliveryOutboxItem(
        outbox_id="obox-pending-reservation-guard",
        event_id=event.event_id,
        route_id="route-reservation",
        delivery_plan_id=_PLAN_ID,
        target_adapter="lxmf-main",
        target_channel="aa" * 16,
        attempt_number=1,
        status="pending",
        worker_id=_WORKER,
    )
    await temp_storage.create_outbox_item(pending)
    assert (
        await temp_storage.reserve_outbox_attempt(pending.outbox_id, _WORKER, 1) is None
    )


# ---------------------------------------------------------------------------
# Claim window: callback admission on both sides of the reservation
# ---------------------------------------------------------------------------


async def test_reserved_attempt_is_admissible_throughout_handoff(temp_storage) -> None:
    """The fix: the live next-attempt callback is recorded before finalization."""
    event = await _seed_event(temp_storage, "evt-window")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-window",
        event_id=event.event_id,
        status="retry_wait",
    )
    # Attempt 1 failed durably and the row waits for retry.
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    lifecycle = DeliveryLifecycleService()

    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ][0]
    assert claimed.attempt_number == 1

    # Before dispatch begins, the prior attempt is still the live identity.
    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _observation_record(
            event_id=event.event_id, outbox_id=item.outbox_id, attempt_number=1
        ),
        datetime.now(timezone.utc),
    )

    reserved = await lifecycle.reserve_retry_attempt(temp_storage, claimed)
    assert reserved == 2

    now = datetime.now(timezone.utc)
    # The reserved attempt's callback is admitted mid-handoff, even though
    # the row still stores attempt 1 and no finalization has committed.
    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _observation_record(
            event_id=event.event_id, outbox_id=item.outbox_id, attempt_number=2
        ),
        now,
    )
    # The superseded attempt's callback is rejected from reservation onward.
    assert not await lifecycle.record_delivery_observation(
        temp_storage,
        _observation_record(
            event_id=event.event_id,
            outbox_id=item.outbox_id,
            attempt_number=1,
            state="failed",
        ),
        now,
    )

    observations = await temp_storage.list_delivery_observations_for_outbox(
        item.outbox_id
    )
    assert [obs.attempt_number for obs in observations] == [1, 2]


async def test_completion_rejects_old_attempts_and_keeps_live_one(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-complete")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-complete", event_id=event.event_id
    )
    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    await temp_storage.mark_outbox_sent(
        item.outbox_id, receipt_id="rcpt-final", attempt_number=2
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.attempt_number == 2
    assert row.active_attempt is None

    lifecycle = DeliveryLifecycleService()
    now = datetime.now(timezone.utc)
    assert not await lifecycle.record_delivery_observation(
        temp_storage,
        _observation_record(
            event_id=event.event_id, outbox_id=item.outbox_id, attempt_number=1
        ),
        now,
    )
    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _observation_record(
            event_id=event.event_id, outbox_id=item.outbox_id, attempt_number=2
        ),
        now,
    )


# ---------------------------------------------------------------------------
# Queue callbacks under a live reservation
# ---------------------------------------------------------------------------


async def test_queue_terminal_commits_reserved_attempt(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-terminal")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-terminal", event_id=event.event_id
    )
    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    receipts_before = len(await temp_storage.list_receipts_for_event(event.event_id))
    await manager.record_terminal(
        QueueTerminalRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            outcome="permanent_failed",
            outbox_id=item.outbox_id,
            delivery_plan_id=_PLAN_ID,
            attempt_number=1,
            native_channel_id="aa" * 16,
        )
    )
    live = await temp_storage.get_outbox_item(item.outbox_id)
    assert live is not None
    assert (live.status, live.attempt_number, live.active_attempt) == (
        "in_progress",
        1,
        2,
    )
    assert len(await temp_storage.list_receipts_for_event(event.event_id)) == (
        receipts_before
    )

    committed = await manager.record_terminal(
        QueueTerminalRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            outcome="permanent_failed",
            outbox_id=item.outbox_id,
            delivery_plan_id=_PLAN_ID,
            attempt_number=2,
            native_channel_id="aa" * 16,
            error="send rejected",
        )
    )
    assert committed is None  # record_terminal returns None; state speaks below

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.attempt_number == 2
    assert row.active_attempt is None

    # A late terminal callback for the superseded attempt commits nothing.
    stale_committed = await manager.record_terminal(
        QueueTerminalRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            outcome="cancelled",
            outbox_id=item.outbox_id,
            delivery_plan_id=_PLAN_ID,
            attempt_number=1,
            native_channel_id="aa" * 16,
        )
    )
    assert stale_committed is None
    row_after = await temp_storage.get_outbox_item(item.outbox_id)
    assert row_after is not None
    assert row_after.status == "dead_lettered"


async def test_stale_finalize_cannot_consume_newer_reservation(temp_storage) -> None:
    """A worker returning after lease theft must not regress the live attempt.

    Worker A's attempt 2 has already been finalized to retry_wait, and worker C
    claims and reserves attempt 3.  If an older worker later tries to finalize
    attempt 2, the explicit-attempt fence must reject the write: C's
    reservation stays live and the row's attempt identity never regresses.
    """
    event = await _seed_event(temp_storage, "evt-stale-finalize")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-stale-finalize",
        event_id=event.event_id,
        status="retry_wait",
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    # Cycle B: claim, reserve 2, transient failure finalizes retry_wait at 2.
    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=2,
    )
    await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        next_attempt_at=_PAST.isoformat(),
        failure_kind="adapter_transient",
        receipt_id="rcpt-b",
        attempt_number=2,
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert (row.attempt_number, row.active_attempt) == (2, None)

    # Cycle C: claim the due row and reserve attempt 3.
    claimed = [
        row
        for row in await _claim_due(temp_storage, worker_id="retry-worker-c")
        if row.outbox_id == item.outbox_id
    ]
    assert claimed
    assert (
        await temp_storage.reserve_outbox_attempt(item.outbox_id, "retry-worker-c", 2)
        == 3
    )

    # Zombie worker A returns with its attempt-2 success.
    assert not await temp_storage.mark_outbox_sent(
        item.outbox_id, receipt_id="rcpt-a-late", attempt_number=2
    )
    after_stale = await temp_storage.get_outbox_item(item.outbox_id)
    assert after_stale is not None
    assert after_stale.status == "in_progress"
    assert after_stale.attempt_number == 2
    assert after_stale.active_attempt == 3

    # The live holder of attempt 3 still finalizes normally.
    assert await temp_storage.mark_outbox_sent(
        item.outbox_id, receipt_id="rcpt-c", attempt_number=3
    )
    after_live = await temp_storage.get_outbox_item(item.outbox_id)
    assert after_live is not None
    assert after_live.status == "sent"
    assert after_live.attempt_number == 3
    assert after_live.active_attempt is None


async def test_stale_finalize_cannot_regress_finalized_attempt(temp_storage) -> None:
    """An older explicit attempt stays stale after the reservation is gone."""
    event = await _seed_event(temp_storage, "evt-stale-finalized")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-stale-finalized",
        event_id=event.event_id,
        status="retry_wait",
    )

    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2
    assert await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        next_attempt_at=_PAST.isoformat(),
        failure_kind="adapter_transient",
        attempt_number=2,
        expected_worker_id=_WORKER,
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert (row.status, row.attempt_number, row.active_attempt) == (
        "retry_wait",
        2,
        None,
    )

    # Zombie attempt 1 must not terminalize the row or regress lineage just
    # because the newer reservation has already been consumed.
    assert not await temp_storage.mark_outbox_dead_lettered(
        item.outbox_id,
        failure_kind="retry_exhausted",
        attempt_number=1,
    )
    after_stale = await temp_storage.get_outbox_item(item.outbox_id)
    assert after_stale is not None
    assert (after_stale.status, after_stale.attempt_number) == ("retry_wait", 2)

    # The finalized attempt itself remains an admissible unreserved commit.
    assert await temp_storage.mark_outbox_dead_lettered(
        item.outbox_id,
        failure_kind="retry_exhausted",
        attempt_number=2,
    )
    after_live = await temp_storage.get_outbox_item(item.outbox_id)
    assert after_live is not None
    assert (after_live.status, after_live.attempt_number) == ("dead_lettered", 2)


async def test_retry_transition_requires_current_claim_owner(temp_storage) -> None:
    """Pre-dispatch stale workers cannot release another worker's claim."""
    event = await _seed_event(temp_storage, "evt-owner-fence")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-owner-fence",
        event_id=event.event_id,
        status="retry_wait",
    )
    claimed = [
        row
        for row in await _claim_due(temp_storage, worker_id="retry-worker-new")
        if row.outbox_id == item.outbox_id
    ][0]
    assert claimed.worker_id == "retry-worker-new"

    assert not await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        next_attempt_at=_PAST.isoformat(),
        failure_kind="capacity_rejection",
        attempt_number=claimed.attempt_number,
        expected_worker_id="retry-worker-stale",
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "in_progress"
    assert row.worker_id == "retry-worker-new"


async def test_terminal_without_attempt_consumes_reservation(temp_storage) -> None:
    """Abandonment/cancellation of a reserved dispatch records that attempt."""
    event = await _seed_event(temp_storage, "evt-terminal-reserved")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-terminal-reserved", event_id=event.event_id
    )
    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    # finalize_retry_success maps a suppressed receipt to abandonment
    # without passing an attempt number.
    await temp_storage.mark_outbox_abandoned(
        item.outbox_id, error_summary="capability_suppressed"
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "abandoned"
    assert row.attempt_number == 2
    assert row.active_attempt is None


async def test_queued_to_sent_commits_reserved_attempt(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-queued")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-queued", event_id=event.event_id
    )
    await _claim_due(temp_storage)
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    # The reserved dispatch produced a queued receipt before the crash.
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="queued",
        attempt_number=2,
    )

    lifecycle = DeliveryLifecycleService()
    receipts_before = len(await temp_storage.list_receipts_for_event(event.event_id))
    await lifecycle.finalize_queued_delivery(
        temp_storage,
        OutboundNativeRefRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            native_channel_id="aa" * 16,
            native_message_id="native-live-stale-1",
            delivery_plan_id=_PLAN_ID,
            outbox_id=item.outbox_id,
            attempt_number=1,
        ),
        datetime.now(timezone.utc),
    )
    live = await temp_storage.get_outbox_item(item.outbox_id)
    assert live is not None
    assert (live.status, live.attempt_number, live.active_attempt) == (
        "in_progress",
        1,
        2,
    )
    assert len(await temp_storage.list_receipts_for_event(event.event_id)) == (
        receipts_before
    )

    await lifecycle.finalize_queued_delivery(
        temp_storage,
        OutboundNativeRefRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            native_channel_id="aa" * 16,
            native_message_id="native-queued-2",
            delivery_plan_id=_PLAN_ID,
            outbox_id=item.outbox_id,
            attempt_number=2,
            confirmation_level="local_transport",
        ),
        datetime.now(timezone.utc),
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.attempt_number == 2
    assert row.active_attempt is None

    # A stale queued→sent callback for attempt 1 is rejected outright.
    receipts_before = len(await temp_storage.list_receipts_for_event(event.event_id))
    await lifecycle.finalize_queued_delivery(
        temp_storage,
        OutboundNativeRefRecord(
            event_id=event.event_id,
            adapter="lxmf-main",
            native_channel_id="aa" * 16,
            native_message_id="native-queued-1",
            delivery_plan_id=_PLAN_ID,
            outbox_id=item.outbox_id,
            attempt_number=1,
        ),
        datetime.now(timezone.utc),
    )
    receipts_after = len(await temp_storage.list_receipts_for_event(event.event_id))
    assert receipts_before == receipts_after


# ---------------------------------------------------------------------------
# Lease recovery: crashes at each persistence boundary
# ---------------------------------------------------------------------------


async def test_reconcile_commits_evidence_after_receipt_persisted_crash(
    temp_storage,
) -> None:
    """Crash between receipt persistence and outbox finalization."""
    event = await _seed_event(temp_storage, "evt-crash-evidence")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-crash-evidence", event_id=event.event_id
    )
    # Worker 1 claimed, reserved attempt 2, dispatched, persisted the failed
    # receipt, then died before finalizing.  The lease has expired.
    await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1)
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=2,
    )

    reclaimed = [
        row
        for row in await _claim_due(temp_storage, worker_id="retry-worker-two")
        if row.outbox_id == item.outbox_id
    ]
    assert reclaimed, "expired-lease row must be reclaimable"
    snapshot = reclaimed[0]
    assert snapshot.active_attempt == 2

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage, snapshot, RetryPolicy(max_attempts=5)
    )
    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    assert finalization.attempt_number == 2

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "retry_wait"
    assert row.attempt_number == 2
    assert row.active_attempt is None


async def test_reconcile_consumes_evidence_less_reserved_attempt(
    temp_storage,
) -> None:
    """A reclaimed evidence-less reservation consumes its attempt identity."""
    event = await _seed_event(temp_storage, "evt-crash-reserve")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-crash-reserve", event_id=event.event_id
    )
    await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1)

    reclaimed = [
        row
        for row in await _claim_due(temp_storage, worker_id="retry-worker-two")
        if row.outbox_id == item.outbox_id
    ]
    assert reclaimed
    snapshot = reclaimed[0]
    assert snapshot.active_attempt == 2

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        snapshot,
        RetryPolicy(max_attempts=5, jitter=False),
        now=_PAST,
    )
    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    assert finalization.attempt_number == 2
    assert finalization.receipt_id is None

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.active_attempt is None
    assert row.attempt_number == 2
    assert row.status == "retry_wait"

    reclaimed_again = [
        candidate
        for candidate in await _claim_due(temp_storage, worker_id="retry-worker-three")
        if candidate.outbox_id == item.outbox_id
    ]
    assert reclaimed_again
    assert (
        await temp_storage.reserve_outbox_attempt(
            item.outbox_id,
            "retry-worker-three",
            2,
        )
        == 3
    )


async def test_reconcile_dead_letters_exhausted_ambiguous_reservation(
    temp_storage,
) -> None:
    event = await _seed_event(temp_storage, "evt-reclaim-exhausted")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-reclaim-exhausted",
        event_id=event.event_id,
    )
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    reclaimed = [
        row
        for row in await _claim_due(temp_storage, worker_id="retry-worker-exhausted")
        if row.outbox_id == item.outbox_id
    ]
    assert reclaimed

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        reclaimed[0],
        RetryPolicy(max_attempts=2, jitter=False),
        now=_PAST,
    )

    assert finalization is not None
    assert finalization.outcome == "dead_lettered"
    assert finalization.attempt_number == 2
    assert finalization.receipt_id is None
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.attempt_number == 2
    assert row.active_attempt is None
    assert row.failure_kind == "retry_exhausted"
    assert row.failure_kind_detail == "dispatch_outcome_unknown"


async def test_reconcile_still_repairs_unreserved_next_attempt_evidence(
    temp_storage,
) -> None:
    """Defensive path: evidence one attempt ahead of an unreserved row."""
    event = await _seed_event(temp_storage, "evt-legacy")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-legacy", event_id=event.event_id
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=2,
    )

    snapshot = await temp_storage.get_outbox_item(item.outbox_id)
    assert snapshot is not None
    assert snapshot.active_attempt is None

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage, snapshot, RetryPolicy(max_attempts=5)
    )
    assert finalization is not None
    assert finalization.attempt_number == 2

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "retry_wait"
    assert row.attempt_number == 2
    assert row.active_attempt is None


async def test_deferral_without_dispatch_consumes_no_attempt(temp_storage) -> None:
    """The unavailable-adapter path defers on the stored attempt, no reservation."""
    event = await _seed_event(temp_storage, "evt-defer")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-defer",
        event_id=event.event_id,
    )
    snapshot = await temp_storage.get_outbox_item(item.outbox_id)
    assert snapshot is not None

    lifecycle = DeliveryLifecycleService()
    await lifecycle.defer_retry_outbox(
        temp_storage,
        snapshot,
        RetryPolicy(max_attempts=5),
        failure_kind="adapter_transient",
        attempt_number=snapshot.attempt_number,
        error_summary="adapter_unavailable_startup: target adapter did not complete runtime startup",
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "retry_wait"
    assert row.attempt_number == 1
    assert row.active_attempt is None


# ---------------------------------------------------------------------------
# Worker-level dispatch flow
# ---------------------------------------------------------------------------


def _worker_with_stub_pipeline(storage, deliver) -> RetryWorker:
    pipeline = MagicMock()
    pipeline.deliver_to_target = deliver
    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
    )
    worker._emit = MagicMock()  # type: ignore[method-assign]
    return worker


async def test_worker_dispatches_under_reserved_attempt(
    temp_storage, monkeypatch
) -> None:
    event = await _seed_event(temp_storage, "evt-worker")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-worker",
        event_id=event.event_id,
        status="retry_wait",
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ][0]

    deliver = AsyncMock(side_effect=ConnectionError("transport down"))
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=5),
        ),
    )
    worker = _worker_with_stub_pipeline(temp_storage, deliver)

    await worker._retry_outbox_item(claimed)

    deliver.assert_awaited_once()
    assert deliver.call_args.kwargs["reserved_attempt_number"] == 2

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "retry_wait"
    assert row.attempt_number == 2
    assert row.active_attempt is None


async def test_worker_never_invokes_transport_on_lost_claim(
    temp_storage, monkeypatch
) -> None:
    event = await _seed_event(temp_storage, "evt-lost")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-lost",
        event_id=event.event_id,
        status="retry_wait",
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    stale_snapshot = [
        row
        for row in await _claim_due(temp_storage, worker_id="worker-a")
        if row.outbox_id == item.outbox_id
    ][0]

    # The lease expires and a competing worker steals the row before
    # dispatch begins.
    await temp_storage.claim_due_outbox_items(
        now=(datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
        worker_id="worker-b",
    )

    deliver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=5),
        ),
    )
    worker = _worker_with_stub_pipeline(temp_storage, deliver)

    await worker._retry_outbox_item(stale_snapshot)

    deliver.assert_not_awaited()
    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert len(receipts) == 1  # only the seeded attempt-1 failure


async def test_worker_skips_transport_when_initial_lease_renewal_fails(
    temp_storage, monkeypatch
) -> None:
    """The awaited pre-transport renewal gates dispatch on a live claim.

    A dispatch that reserves near the end of its original lease must not
    enter the transport before the lease is extended; when the synchronous
    renewal reports the claim lost, the transport is never invoked.
    """
    event = await _seed_event(temp_storage, "evt-renew-lost")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-renew-lost",
        event_id=event.event_id,
        status="retry_wait",
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ][0]

    deliver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=5),
        ),
    )
    worker = _worker_with_stub_pipeline(temp_storage, deliver)

    async def _losing_renewal(storage_arg, item_arg, *, lease_seconds):
        return False

    monkeypatch.setattr(worker._lifecycle, "renew_retry_lease", _losing_renewal)

    await worker._retry_outbox_item(claimed)

    deliver.assert_not_awaited()
    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert len(receipts) == 1  # only the seeded attempt-1 failure


async def test_dispatch_lease_is_renewed_while_transport_runs(temp_storage) -> None:
    """A live worker's dispatch must not outlive its claim lease."""
    from tests.helpers.async_utils import wait_until

    event = await _seed_event(temp_storage, "evt-lease-renew")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-lease-renew", event_id=event.event_id
    )
    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ][0]
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    worker = RetryWorker(
        storage=temp_storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        interval_seconds=0.2,
    )
    worker._emit = MagicMock()  # type: ignore[method-assign]

    initial_row = await temp_storage.get_outbox_item(item.outbox_id)
    assert initial_row is not None
    initial_lease = initial_row.lease_until
    assert initial_lease is not None

    async def _lease_advanced() -> bool:
        row = await temp_storage.get_outbox_item(item.outbox_id)
        return row is not None and (row.lease_until or "") > initial_lease

    renewal = asyncio.create_task(worker._renew_dispatch_lease(claimed))
    try:
        assert await wait_until(_lease_advanced, timeout=2.0)
    finally:
        renewal.cancel()
        try:
            await renewal
        except asyncio.CancelledError:
            pass


async def test_worker_cancels_dispatch_lease_renewal_on_completion(
    temp_storage, monkeypatch
) -> None:
    """The renewal task never outlives the dispatch it protects."""
    event = await _seed_event(temp_storage, "evt-renew-cancel")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-renew-cancel",
        event_id=event.event_id,
        status="retry_wait",
    )
    await _append_receipt(
        temp_storage,
        outbox_id=item.outbox_id,
        event_id=event.event_id,
        status="failed",
        attempt_number=1,
    )
    claimed = [
        row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id
    ][0]

    deliver = AsyncMock(side_effect=ConnectionError("transport down"))
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=5),
        ),
    )
    worker = _worker_with_stub_pipeline(temp_storage, deliver)

    recorded: dict[str, object] = {}

    async def _recording_renew(item_arg):
        recorded["task"] = asyncio.current_task()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_renew_dispatch_lease", _recording_renew)
    await worker._retry_outbox_item(claimed)

    renewal_task = recorded.get("task")
    assert isinstance(renewal_task, asyncio.Task), "dispatch must start renewal"
    assert renewal_task.done() and renewal_task.cancelled()


# ---------------------------------------------------------------------------
# Dispatch stamping: the reserved identity reaches the adapter
# ---------------------------------------------------------------------------


async def test_reserved_attempt_overrides_lineage_stamp() -> None:
    from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
    from medre.core.observability.metrics import Diagnostician
    from medre.core.planning.delivery_plan import (
        DeliveryPlan,
        DeliveryStrategy,
    )
    from medre.core.rendering.renderer import RenderingResult
    from medre.core.routing.models import (
        Route,
        RouteSource,
        RouteTarget,
    )

    class _CapturingAdapter:
        adapter_id = "test_adapter"
        platform = "test_platform"

        def __init__(self) -> None:
            self.stamped: RenderingResult | None = None

        async def deliver(self, rendering_result):
            self.stamped = rendering_result
            return AdapterDeliveryResult(
                native_message_id="native-reserved",
                native_channel_id=None,
            )

    class _ReceiptListStorage:
        def __init__(self) -> None:
            self.receipts: list[DeliveryReceipt] = []

        async def append_receipt(self, receipt: DeliveryReceipt) -> None:
            self.receipts.append(receipt)

        async def store_native_ref(self, ref) -> None:
            return None

    class _StaticRenderingPipeline:
        async def render(self, event, target_adapter, target_channel=None, **_):
            return RenderingResult(
                event_id=event.event_id,
                target_adapter=target_adapter,
                target_channel=target_channel,
                payload={"text": "reserved stamp"},
            )

    adapter = _CapturingAdapter()
    storage = _ReceiptListStorage()
    service = TargetDeliveryService(
        adapters={"test_adapter": adapter},
        rendering_pipeline=_StaticRenderingPipeline(),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        diagnostician=Diagnostician(),
        lifecycle=DeliveryLifecycleService(),
        logger=logging.getLogger("test.reservation.stamp"),
    )
    target = RouteTarget(adapter="test_adapter", channel=None)
    route = Route(
        id="route-001",
        source=RouteSource(
            adapter="src_adapter", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-001",
        event_id="evt-stamp-001",
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    event = CanonicalEvent(
        event_id="evt-stamp-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="src_adapter",
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "reserved stamp"},
        metadata=EventMetadata(),
    )

    previous = DeliveryReceipt(
        receipt_id="rcpt-previous",
        event_id=event.event_id,
        delivery_plan_id=plan.plan_id,
        target_adapter="test_adapter",
        route_id="route-001",
        status="failed",
        attempt_number=4,
        source="retry",
        created_at=datetime.now(timezone.utc),
    )

    receipt = await service.deliver_to_target(
        event,
        route,
        plan,
        previous_receipt=previous,
        source="retry",
        outbox_id="obox-stamp",
        reserved_attempt_number=6,
    )

    assert receipt.attempt_number == 6
    assert receipt.parent_receipt_id == "rcpt-previous"
    assert adapter.stamped is not None
    assert adapter.stamped.attempt_number == 6
    assert adapter.stamped.outbox_id == "obox-stamp"


async def test_retry_worker_does_not_report_superseded_success_transition(
    monkeypatch,
) -> None:
    """A guarded CAS miss is stale work, not a durable success or failure."""
    from medre.core.engine.pipeline.delivery_lifecycle import (
        RetryAttemptCommitRejected,
    )

    item = DeliveryOutboxItem(
        outbox_id="obox-success-superseded",
        event_id="evt-success-superseded",
        route_id="route-success-superseded",
        delivery_plan_id="plan-success-superseded",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-stale",
    )
    sent = DeliveryReceipt(
        receipt_id="rcpt-success-superseded-2",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        route_id=item.route_id,
        status="sent",
        attempt_number=2,
        outbox_id=item.outbox_id,
    )
    storage = MagicMock()
    storage.get = AsyncMock(return_value=object())
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_to_target = AsyncMock(return_value=sent)
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_success = AsyncMock(
        side_effect=RetryAttemptCommitRejected("claim moved to a newer worker")
    )
    lifecycle.reconcile_retry_success_commit_rejection = AsyncMock(return_value=None)
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=3),
        ),
    )
    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
        lifecycle=lifecycle,
    )
    emit = MagicMock()
    monkeypatch.setattr(worker, "_emit", emit)

    await worker._retry_outbox_item(item)

    assert worker.state.processed == 1
    assert worker.state.succeeded == 0
    assert worker.state.failed == 0
    event_types = [call.args[0] for call in emit.call_args_list]
    assert "retry_attempted" in event_types
    assert "retry_succeeded" not in event_types
    assert "retry_failed" not in event_types


async def test_unstamped_queued_transition_consumes_live_reservation(temp_storage) -> None:
    """Leaving in_progress without an explicit attempt finalizes the reservation."""
    event = await _seed_event(temp_storage, "evt-reserved-queued-implicit")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-reserved-queued-implicit",
        event_id=event.event_id,
    )
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    assert await temp_storage.mark_outbox_queued(
        item.outbox_id,
        receipt_id="rcpt-reserved-queued-implicit",
        expected_worker_id=_WORKER,
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert (row.status, row.attempt_number, row.active_attempt) == ("queued", 2, None)


async def test_unstamped_retry_wait_transition_consumes_live_reservation(
    temp_storage,
) -> None:
    """Retry deferral without an explicit attempt cannot leave a ghost reservation."""
    event = await _seed_event(temp_storage, "evt-reserved-retry-wait-implicit")
    item = await _seed_outbox(
        temp_storage,
        outbox_id="obox-reserved-retry-wait-implicit",
        event_id=event.event_id,
    )
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2

    assert await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        next_attempt_at=_FUTURE.isoformat(),
        failure_kind="adapter_transient",
        expected_worker_id=_WORKER,
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert (row.status, row.attempt_number, row.active_attempt) == (
        "retry_wait",
        2,
        None,
    )
