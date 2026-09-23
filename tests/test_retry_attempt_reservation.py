"""Durable retry attempt reservation and its callback-admission contract.

The retry worker reserves the next attempt identity on a claimed outbox row
before invoking the transport.  From that commit onward the reserved number
is the live attempt for every callback validator, and earlier attempts are
stale.  These tests pin the reservation CAS, both sides of the claim window,
lease recovery after crashes at each persistence boundary, and the rule that
a deferral before dispatch consumes no attempt.
"""

from __future__ import annotations

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


async def test_clear_reservation_releases_exact_number_only(temp_storage) -> None:
    event = await _seed_event(temp_storage, "evt-clear")
    item = await _seed_outbox(
        temp_storage, outbox_id="obox-clear", event_id=event.event_id
    )
    [row for row in await _claim_due(temp_storage) if row.outbox_id == item.outbox_id][
        0
    ]

    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2
    assert not await temp_storage.clear_outbox_attempt_reservation(
        item.outbox_id, _WORKER, 7
    )
    assert await temp_storage.clear_outbox_attempt_reservation(
        item.outbox_id, _WORKER, 2
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.active_attempt is None
    # The released number is immediately reservable again by the same claim.
    assert await temp_storage.reserve_outbox_attempt(item.outbox_id, _WORKER, 1) == 2


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


async def test_reconcile_clears_reservation_after_pre_dispatch_crash(
    temp_storage,
) -> None:
    """Crash after reservation but before any evidence persisted."""
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
    assert (
        await lifecycle.reconcile_retry_claim(
            temp_storage, snapshot, RetryPolicy(max_attempts=5)
        )
        is None
    )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.active_attempt is None
    assert row.attempt_number == 1
    assert row.status == "in_progress"
    # The released identity is reservable again by the recovering worker.
    assert (
        await temp_storage.reserve_outbox_attempt(item.outbox_id, "retry-worker-two", 1)
        == 2
    )


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
        status="retry_wait",
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


# ---------------------------------------------------------------------------
# Dispatch stamping: the reserved identity reaches the adapter
# ---------------------------------------------------------------------------


async def test_reserved_attempt_overrides_lineage_stamp() -> None:
    from medre.core.rendering.renderer import RenderingResult
    from tests.test_target_delivery_outcomes import (
        _make_event,
        _make_route_and_plan,
        _make_service,
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

    adapter = _CapturingAdapter()
    service, storage = _make_service(adapters={"test_adapter": adapter})
    route, plan = _make_route_and_plan()
    event = _make_event()

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
