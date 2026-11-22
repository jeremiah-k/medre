"""Settlement argument identity and clock-controlled backoff for retry recovery.

The erased-route reconciliation must settle exactly the claimed item's
committed acceptance (never a neighboring attempt or receipt set), a
startup deferral must not reserve a dispatch identity, and the persisted
retry_wait backoff must be measured on the reconciliation clock, not the
test process's wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import medre.core.engine.pipeline.delivery_lifecycle as lifecycle_module
import medre.runtime.retry as retry_module
from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
from medre.core.engine.pipeline.retry_plan import ReconstructedRetryPlan
from medre.core.events import DeliveryReceipt
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem, OutboxRouteRetryBoundsWrite
from medre.runtime.retry import RetryWorker
from tests.helpers.storage_outbox import admit_event

_WORKER = "worker-settle-args"
_PLAN_ID = "plan-handoff-recheck"
_UNCERTAIN_DETAIL = "uncertain_native_handoff"


def _gated_item(**overrides) -> DeliveryOutboxItem:
    """An outbox row whose last finalized attempt was an uncertain handoff."""
    fields: dict = {
        "outbox_id": "obox-handoff-recheck",
        "event_id": "evt-handoff-recheck",
        "route_id": "route-handoff-recheck",
        "delivery_plan_id": _PLAN_ID,
        "target_adapter": "target_a",
        "attempt_number": 1,
        "status": "in_progress",
        "worker_id": _WORKER,
        "failure_kind_detail": _UNCERTAIN_DETAIL,
    }
    fields.update(overrides)
    return DeliveryOutboxItem(**fields)


def _retained_storage() -> MagicMock:
    """Expose only explicit evidence-read surfaces, not dynamic attributes."""
    storage = MagicMock(
        spec=[
            "get_metadata",
            "get",
            "delivery_status",
            "get_outbox_dispatch_context",
            "get_outbox_route_retry_bounds",
            "list_receipts_for_outbox",
            "read_event_for_outbox",
            "get_radio_attempt",
            "read_handoff_observation_snapshot",
        ]
    )
    stored = object()
    storage.get_metadata = AsyncMock(return_value=stored)
    storage.get = AsyncMock(return_value=stored)
    storage.delivery_status = AsyncMock(return_value=None)
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_outbox_route_retry_bounds = AsyncMock(return_value=None)
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=stored)
    )
    for method in ("get_radio_attempt", "read_handoff_observation_snapshot"):
        setattr(storage, method, AsyncMock(return_value=None))
    return storage


def _lifecycle(recheck_result=None) -> MagicMock:
    lifecycle = MagicMock()
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.reconcile_content_denied_handoff = AsyncMock(return_value=None)
    lifecycle.settle_committed_acceptance = AsyncMock(return_value=None)
    lifecycle.recheck_last_uncertain_handoff = AsyncMock(return_value=recheck_result)
    lifecycle.list_attempt_receipts = AsyncMock(return_value=[])
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=None)
    lifecycle.abandon_retry_outbox = AsyncMock(return_value=None)
    return lifecycle


def _worker(storage, lifecycle) -> tuple[RetryWorker, MagicMock]:
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(return_value=None)
    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
        lifecycle=lifecycle,
    )
    return worker, pipeline


@pytest.fixture(autouse=True)
def _frozen_retry_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=3),
        ),
    )


async def _run_with_emit(worker: RetryWorker, item: DeliveryOutboxItem) -> MagicMock:
    emit = MagicMock()
    worker._emit = emit
    await worker._retry_outbox_item(item)
    return emit


async def test_settle_receives_only_the_claimed_item_exact_acceptance() -> None:
    """The lifecycle settles this item, this attempt, these exact receipts."""
    item = _gated_item(metadata={})
    storage = _retained_storage()
    storage.read_event_for_outbox.return_value = SimpleNamespace(
        status="expired", event=object()
    )
    lifecycle = _lifecycle()
    receipt = DeliveryReceipt(
        receipt_id="sent-current-attempt",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        outbox_id=item.outbox_id,
        attempt_number=1,
        status="sent",
        source="retry",
    )
    lifecycle.list_attempt_receipts = AsyncMock(return_value=[receipt])
    finalization = RetryAttemptFinalization(
        outcome="accepted",
        receipt_id=receipt.receipt_id,
        failure_kind=None,
        attempt_number=1,
    )

    async def _settle(settle_storage, settle_item, *, attempt_number, receipts):
        assert settle_storage is storage
        assert settle_item is item
        assert settle_item.outbox_id == item.outbox_id
        assert attempt_number == item.attempt_number == 1
        assert receipts == [receipt]
        assert receipts[0].receipt_id == "sent-current-attempt"
        return finalization

    lifecycle.settle_committed_acceptance = AsyncMock(side_effect=_settle)
    worker, pipeline = _worker(storage, lifecycle)

    emit = await _run_with_emit(worker, item)

    lifecycle.settle_committed_acceptance.assert_awaited_once()
    lifecycle.abandon_retry_outbox.assert_not_awaited()
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1 and worker.state.succeeded == 1
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert emitted["retry_succeeded"]["receipt_id"] == "sent-current-attempt"


async def test_startup_deferral_does_not_reserve_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup-unavailable targets defer without reserving a dispatch identity."""
    item = DeliveryOutboxItem(
        outbox_id="obox-unavailable",
        event_id="evt-unavailable",
        route_id="route-unavailable",
        delivery_plan_id="plan-unavailable",
        target_adapter="target_down",
        attempt_number=2,
        status="in_progress",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock()
    lifecycle = MagicMock()
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.defer_retry_outbox = AsyncMock(
        return_value=datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    lifecycle.reserve_retry_attempt = AsyncMock()
    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
        lifecycle=lifecycle,
        available_target_adapters={"target_ready"},
    )
    emit = MagicMock()
    monkeypatch.setattr(worker, "_emit", emit)

    await worker._retry_outbox_item(item)

    lifecycle.reserve_retry_attempt.assert_not_awaited()
    lifecycle.defer_retry_outbox.assert_awaited_once()
    defer_call = lifecycle.defer_retry_outbox.call_args
    assert defer_call.args[1] is item
    assert defer_call.kwargs["attempt_number"] == 2
    pipeline.deliver_execution_to_target.assert_not_awaited()


class _FrozenClock(datetime):
    """A datetime whose now() is pinned for the reconciliation module."""

    pinned: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.pinned


async def test_erased_route_backoff_is_measured_on_the_reconciliation_clock(
    temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """next_attempt_at equals the reconciliation clock plus the bounds backoff."""
    from medre.core.storage.sqlite.storage import SQLiteStorage

    assert isinstance(temp_storage, SQLiteStorage)
    outbox_id = "obox-clock-bounds"
    await admit_event(temp_storage, "evt-clock-bounds")
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id="evt-clock-bounds",
            route_id="route-clock-bounds",
            delivery_plan_id="plan-clock-bounds",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=0,
            status="in_progress",
            locked_at=datetime.now(timezone.utc).isoformat(),
            lease_until=(
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat(),
            worker_id=_WORKER,
        ),
        route_retry_bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=5,
            backoff_base=2.0,
            max_delay_seconds=90.0,
            jitter=False,
        ),
    )
    reserved = await temp_storage.reserve_outbox_attempt(outbox_id, _WORKER, 0)
    assert reserved == 1
    temp_storage.read_event_for_outbox = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(status="denied", event=None)
    )
    worker = RetryWorker(
        storage=temp_storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=1,
    )
    worker._emit = MagicMock()

    pinned = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    _FrozenClock.pinned = pinned
    monkeypatch.setattr(lifecycle_module, "datetime", _FrozenClock)
    await worker._retry_outbox_item(await temp_storage.get_outbox_item(outbox_id))

    row = await temp_storage.get_outbox_item(outbox_id)
    assert row is not None and row.status == "retry_wait"
    assert row.next_attempt_at is not None
    assert datetime.fromisoformat(row.next_attempt_at) == pinned + timedelta(seconds=2)
