from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import medre.runtime.retry as retry_module
from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptCommitRejected,
    RetryAttemptFinalization,
)
from medre.core.engine.pipeline.outbox_manager import OutboxContext, OutboxManager
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.retry import RetryWorker


def _item(
    *, status: str = "in_progress", attempt_number: int = 1
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id="obox-race",
        event_id="evt-race",
        route_id="route-race",
        delivery_plan_id="plan-race",
        target_adapter="target_a",
        attempt_number=attempt_number,
        status=status,
        worker_id="retry-worker-race" if status == "in_progress" else None,
    )


def _receipt(*, status: str = "queued", attempt_number: int = 2) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=f"rcpt-race-{attempt_number}",
        event_id="evt-race",
        delivery_plan_id="plan-race",
        target_adapter="target_a",
        route_id="route-race",
        status=status,
        attempt_number=attempt_number,
        outbox_id="obox-race",
        source="retry",
    )


async def test_lifecycle_reconciles_same_attempt_sent_after_queued_cas_loss() -> None:
    lifecycle = DeliveryLifecycleService()
    storage = MagicMock()
    storage.get_outbox_item = AsyncMock(
        return_value=DeliveryOutboxItem(
            outbox_id="obox-race",
            event_id="evt-race",
            route_id="route-race",
            delivery_plan_id="plan-race",
            target_adapter="target_a",
            attempt_number=2,
            status="sent",
            receipt_id="rcpt-terminal-2",
        )
    )

    finalization = await lifecycle.reconcile_retry_success_commit_rejection(
        storage,
        _item(),
        _receipt(),
    )

    assert finalization == RetryAttemptFinalization(
        outcome="accepted",
        receipt_id="rcpt-terminal-2",
        failure_kind=None,
        attempt_number=2,
    )


async def test_lifecycle_reconciles_same_attempt_error_terminal_after_queued_cas_loss() -> (
    None
):
    lifecycle = DeliveryLifecycleService()
    storage = MagicMock()
    storage.get_outbox_item = AsyncMock(
        return_value=DeliveryOutboxItem(
            outbox_id="obox-race",
            event_id="evt-race",
            route_id="route-race",
            delivery_plan_id="plan-race",
            target_adapter="target_a",
            attempt_number=2,
            status="dead_lettered",
            receipt_id="rcpt-terminal-2",
            failure_kind="adapter_permanent",
        )
    )

    finalization = await lifecycle.reconcile_retry_success_commit_rejection(
        storage,
        _item(),
        _receipt(),
    )

    assert finalization == RetryAttemptFinalization(
        outcome="dead_lettered",
        receipt_id="rcpt-terminal-2",
        failure_kind="adapter_permanent",
        attempt_number=2,
    )


async def test_lifecycle_does_not_reconcile_different_attempt_after_cas_loss() -> None:
    lifecycle = DeliveryLifecycleService()
    storage = MagicMock()
    storage.get_outbox_item = AsyncMock(
        return_value=DeliveryOutboxItem(
            outbox_id="obox-race",
            event_id="evt-race",
            route_id="route-race",
            delivery_plan_id="plan-race",
            target_adapter="target_a",
            attempt_number=3,
            status="sent",
            receipt_id="rcpt-terminal-3",
        )
    )

    assert (
        await lifecycle.reconcile_retry_success_commit_rejection(
            storage,
            _item(),
            _receipt(),
        )
        is None
    )


async def test_retry_worker_reports_callback_won_queued_race_as_committed_success(
    monkeypatch,
) -> None:
    item = _item()
    queued = _receipt()
    storage = MagicMock()
    storage.get = AsyncMock(return_value=object())
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_to_target = AsyncMock(return_value=queued)
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_success = AsyncMock(
        side_effect=RetryAttemptCommitRejected("terminal callback committed first")
    )
    lifecycle.reconcile_retry_success_commit_rejection = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome="accepted",
            receipt_id="rcpt-terminal-2",
            failure_kind=None,
            attempt_number=2,
        )
    )
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
    assert worker.state.succeeded == 1
    assert worker.state.failed == 0
    succeeded = [c for c in emit.call_args_list if c.args[0] == "retry_succeeded"]
    assert len(succeeded) == 1
    assert succeeded[0].args[1]["retry_receipt_id"] == "rcpt-terminal-2"
    assert succeeded[0].args[1]["reconciled"] is True


async def test_outbox_manager_passes_pipeline_owner_to_finalization() -> None:
    storage = MagicMock()
    lifecycle = MagicMock()
    lifecycle.finalize_outbox_outcome = AsyncMock()
    manager = OutboxManager(storage, lifecycle)
    ctx = OutboxContext(
        outbox_id="obox-race",
        created=True,
        pipeline_worker="pipeline-owner",
        skip_reason=None,
    )

    await manager.finalize_outcome(
        ctx,
        _receipt(status="sent", attempt_number=1),
        None,
        None,
        None,
    )

    lifecycle.finalize_outbox_outcome.assert_awaited_once()
    call = lifecycle.finalize_outbox_outcome.await_args
    assert call.args == (storage,)
    assert call.kwargs["outbox_id"] == "obox-race"
    assert call.kwargs["outbox_created"] is True
    assert call.kwargs["expected_worker_id"] == "pipeline-owner"
