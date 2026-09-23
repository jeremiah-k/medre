from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import medre.runtime.retry as retry_module
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
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
from tests.helpers.storage_outbox import (
    append_receipt_with_parent,
    create_outbox_item_with_parent,
)


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
    storage.list_receipts_for_plan = AsyncMock(
        return_value=[
            DeliveryReceipt(
                receipt_id="rcpt-terminal-2",
                event_id="evt-race",
                delivery_plan_id="plan-race",
                target_adapter="target_a",
                route_id="route-race",
                status="sent",
                attempt_number=2,
                outbox_id="obox-race",
                source="retry",
            )
        ]
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
    storage.list_receipts_for_plan = AsyncMock(
        return_value=[
            DeliveryReceipt(
                receipt_id="rcpt-terminal-2",
                event_id="evt-race",
                delivery_plan_id="plan-race",
                target_adapter="target_a",
                route_id="route-race",
                status="failed",
                failure_kind="adapter_permanent",
                attempt_number=2,
                outbox_id="obox-race",
                source="retry",
            )
        ]
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


async def test_lifecycle_does_not_reconcile_terminal_without_committed_receipt() -> (
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
            failure_kind="retry_exhausted",
            receipt_id=None,
        )
    )
    storage.list_receipts_for_plan = AsyncMock()

    assert (
        await lifecycle.reconcile_retry_success_commit_rejection(
            storage,
            _item(),
            _receipt(),
        )
        is None
    )
    storage.list_receipts_for_plan.assert_not_awaited()


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


async def test_late_rejected_receipt_remains_history_not_current(temp_storage) -> None:
    item = _item()
    await create_outbox_item_with_parent(temp_storage, item)

    committed_receipt = _receipt(status="sent", attempt_number=1)
    await append_receipt_with_parent(temp_storage, committed_receipt)
    assert await temp_storage.mark_outbox_sent(
        item.outbox_id,
        receipt_id=committed_receipt.receipt_id,
        attempt_number=1,
        expected_worker_id=item.worker_id,
    )

    # Simulate a stale pipeline/worker returning after the authoritative outbox
    # transition already committed.  Receipt history is immutable, so the late
    # row remains durable, but it must not become current status or retry input.
    late_receipt = DeliveryReceipt(
        receipt_id="rcpt-race-late-stale",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        route_id=item.route_id,
        status="failed",
        failure_kind="adapter_transient",
        error="late stale failure",
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        attempt_number=1,
        outbox_id=item.outbox_id,
        source="live",
    )
    await append_receipt_with_parent(temp_storage, late_receipt)

    history = await temp_storage.list_receipts_for_event(item.event_id)
    assert [receipt.receipt_id for receipt in history] == [
        committed_receipt.receipt_id,
        late_receipt.receipt_id,
    ]
    current = await temp_storage.delivery_status(
        item.delivery_plan_id, item.target_adapter, item.target_channel, event_id=item.event_id
    )
    assert current is not None
    assert current.receipt_id == committed_receipt.receipt_id

    due = await temp_storage.list_due_retry_receipts(datetime.now(timezone.utc))
    assert late_receipt.receipt_id not in {receipt.receipt_id for receipt in due}
    unresolved = await temp_storage.query_unresolved_deliveries()
    assert not unresolved.items


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
        DeliveryExecutionEvidence(
            attempt_receipt=_receipt(status="sent", attempt_number=1)
        ),
        None,
    )

    lifecycle.finalize_outbox_outcome.assert_awaited_once()
    call = lifecycle.finalize_outbox_outcome.await_args
    assert call.args == (storage,)
    assert call.kwargs["outbox_id"] == "obox-race"
    assert call.kwargs["outbox_created"] is True
    assert call.kwargs["expected_worker_id"] == "pipeline-owner"


async def test_retry_suppression_commits_receipt_pointer() -> None:
    lifecycle = DeliveryLifecycleService()
    storage = MagicMock()
    storage.mark_outbox_abandoned = AsyncMock(return_value=True)
    receipt = _receipt(status="suppressed", attempt_number=2)

    succeeded = await lifecycle.finalize_retry_success(storage, _item(), receipt)

    assert succeeded is False
    storage.mark_outbox_abandoned.assert_awaited_once_with(
        "obox-race",
        error_summary=receipt.error,
        receipt_id=receipt.receipt_id,
        expected_worker_id="retry-worker-race",
    )
