# medre-test-tier: compatibility
"""Retry worker reconciliation and finalization outcome tests.

Split from ``tests/test_retry_worker_outcomes.py`` when that file passed
the 1 500-line ceiling: these module-level tests project lifecycle-
authoritative finalizations onto worker counters and runtime events,
separate from the worker lifecycle behaviors (capacity backoff, crash
outcome, uncancel drain, stop abandonment visibility) that remain there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline.retry_plan import ReconstructedRetryPlan
from medre.core.events.canonical import DeliveryReceipt
from tests.helpers.delivery_evidence import evidence_for_receipt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


async def test_retry_worker_does_not_report_suppressed_receipt_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppressed delivery is terminal but must not increment retry success."""

    import medre.runtime.retry as retry_module
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-suppressed",
        event_id="evt-suppressed",
        route_id="route-suppressed",
        delivery_plan_id="plan-suppressed",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    storage.list_receipts_for_delivery = AsyncMock(return_value=[])
    storage.reserve_outbox_attempt = AsyncMock(return_value=2)
    storage.renew_outbox_lease = AsyncMock(return_value=True)
    storage.mark_outbox_abandoned = AsyncMock(return_value=True)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(
        return_value=evidence_for_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-suppressed",
                event_id=item.event_id,
                delivery_plan_id=item.delivery_plan_id,
                target_adapter=item.target_adapter,
                route_id=item.route_id,
                status="suppressed",
                error="capability_suppressed",
                attempt_number=1,
            )
        )
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
    )
    emit = MagicMock()
    monkeypatch.setattr(worker, "_emit", emit)

    await worker._retry_outbox_item(item)

    storage.delivery_status.assert_awaited_once_with(
        DeliveryIdentity(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel,
        )
    )
    assert worker.state.processed == 1
    assert worker.state.succeeded == 0
    assert worker.state.failed == 1
    storage.mark_outbox_abandoned.assert_awaited_once_with(
        item.outbox_id,
        error_summary="capability_suppressed",
        receipt_id="rcpt-suppressed",
        attempt_number=1,
        expected_worker_id=item.worker_id,
    )
    assert not any(
        call.args and call.args[0] == "retry_succeeded" for call in emit.call_args_list
    )
    failed_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_failed"
    )
    assert failed_event.args[1]["status"] == "suppressed"
    assert failed_event.args[1]["error"] == "capability_suppressed"


async def test_retry_worker_reconciles_returned_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned failed receipt is failure evidence, not a successful retry."""
    import medre.runtime.retry as retry_module
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-failure-return",
        event_id="evt-failure-return",
        route_id="route-failure-return",
        delivery_plan_id="plan-failure-return",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    receipt = DeliveryReceipt(
        receipt_id="rcpt-failure-return",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        route_id=item.route_id,
        status="failed",
        failure_kind="adapter_transient",
        error="remote failure",
        attempt_number=2,
        outbox_id=item.outbox_id,
        source="retry",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(
        return_value=evidence_for_receipt(receipt)
    )
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_attempt_error = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome="retry_wait",
            receipt_id=receipt.receipt_id,
            failure_kind="adapter_transient",
            attempt_number=2,
        )
    )
    lifecycle.finalize_retry_success = AsyncMock()
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
    assert worker.state.failed == 1
    lifecycle.finalize_retry_success.assert_not_awaited()
    lifecycle.finalize_retry_attempt_error.assert_awaited_once()
    assert (
        lifecycle.finalize_retry_attempt_error.call_args.kwargs["attempt_number"] == 2
    )
    assert any(call.args[0] == "retry_failed" for call in emit.call_args_list)


async def test_retry_worker_defers_startup_failed_target_without_consuming_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known startup-unavailable targets are deferred, not dispatched."""

    import medre.runtime.retry as retry_module
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

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
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=30)
    lifecycle.defer_retry_outbox = AsyncMock(return_value=next_attempt)
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
        available_target_adapters={"target_ready"},
    )
    emit = MagicMock()
    monkeypatch.setattr(worker, "_emit", emit)

    await worker._retry_outbox_item(item)

    lifecycle.reconcile_retry_claim.assert_awaited_once()
    lifecycle.defer_retry_outbox.assert_awaited_once()
    defer_call = lifecycle.defer_retry_outbox.call_args
    assert defer_call.args[0] is storage
    assert defer_call.args[1] is item
    assert defer_call.kwargs == {
        "failure_kind": "adapter_transient",
        "attempt_number": 2,
        "min_delay": timedelta(seconds=10),
        "error_summary": (
            "adapter_unavailable_startup: target adapter did not complete "
            "runtime startup"
        ),
    }
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1
    assert worker.state.succeeded == 0
    assert worker.state.failed == 1
    failed_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_failed"
    )
    assert failed_event.args[1]["attempt_number"] == 2
    assert failed_event.args[1]["status"] == "adapter_unavailable_startup"
    assert failed_event.args[1]["failure_kind"] == "adapter_transient"
    assert failed_event.args[1]["next_retry_at"] == next_attempt.isoformat()


async def test_retry_worker_reconciles_persisted_attempt_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reclaimed rows repair persisted evidence without duplicate delivery."""

    import medre.runtime.retry as retry_module
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-reconcile",
        event_id="evt-reconcile",
        route_id="route-reconcile",
        delivery_plan_id="plan-reconcile",
        target_adapter="target_a",
        attempt_number=1,
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
    lifecycle.reconcile_retry_claim = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome="accepted",
            receipt_id="rcpt-sent-2",
            failure_kind=None,
            attempt_number=2,
        )
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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

    lifecycle.reconcile_retry_claim.assert_awaited_once()
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1
    assert worker.state.succeeded == 1
    assert worker.state.failed == 0
    assert not any(
        call.args and call.args[0] == "retry_attempted" for call in emit.call_args_list
    )
    success_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_succeeded"
    )
    assert success_event.args[1]["reconciled"] is True
    assert success_event.args[1]["retry_receipt_id"] == "rcpt-sent-2"


@pytest.mark.parametrize(
    ("outcome", "expected_event", "expected_failed", "expected_dead_lettered"),
    [
        ("retry_wait", "retry_failed", 1, 0),
        ("dead_lettered", "retry_dead_lettered", 1, 1),
        ("suppressed", "retry_failed", 1, 0),
    ],
)
async def test_retry_worker_reports_reconciled_terminal_and_wait_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_event: str,
    expected_failed: int,
    expected_dead_lettered: int,
) -> None:
    """Claim reconciliation updates counters/events without redispatch."""

    import medre.runtime.retry as retry_module
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id=f"obox-reconcile-{outcome}",
        event_id=f"evt-reconcile-{outcome}",
        route_id="route-reconcile",
        delivery_plan_id=f"plan-reconcile-{outcome}",
        target_adapter="target_a",
        attempt_number=1,
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
    lifecycle.reconcile_retry_claim = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome=outcome,  # type: ignore[arg-type]
            receipt_id=f"rcpt-{outcome}-2",
            failure_kind="adapter_transient",
            attempt_number=2,
        )
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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

    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1
    assert worker.state.failed == expected_failed
    assert worker.state.dead_lettered == expected_dead_lettered
    event = next(call for call in emit.call_args_list if call.args[0] == expected_event)
    assert event.args[1]["reconciled"] is True
    assert event.args[1]["retry_receipt_id"] == f"rcpt-{outcome}-2"
    if outcome in {"retry_wait", "suppressed"}:
        assert event.args[1]["status"] == outcome


async def test_retry_worker_retry_wait_event_includes_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty exception messages still produce a useful retry failure error."""

    import medre.runtime.retry as retry_module
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-empty-error",
        event_id="evt-empty-error",
        route_id="route-empty-error",
        delivery_plan_id="plan-empty-error",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(side_effect=ConnectionError())
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_attempt_error = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome="retry_wait",
            receipt_id=None,
            failure_kind="adapter_transient",
            attempt_number=2,
        )
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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

    failed_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_failed"
    )
    assert failed_event.args[1]["error"] == "ConnectionError: "


async def test_retry_worker_reports_retry_finalization_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle write errors report their own error, not the delivery error."""

    import medre.runtime.retry as retry_module
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-finalize-fail",
        event_id="evt-finalize-fail",
        route_id="route-finalize-fail",
        delivery_plan_id="plan-finalize-fail",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(
        side_effect=ConnectionError("transport down")
    )
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_attempt_error = AsyncMock(
        side_effect=RuntimeError("injected outbox write failure")
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
    assert worker.state.failed == 1
    failed_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_failed"
    )
    detail = failed_event.args[1]
    assert detail["status"] == "lifecycle_persistence_error"
    assert detail["error"] == "RuntimeError: injected outbox write failure"
    assert "transport down" not in detail["error"]


async def test_retry_worker_reports_success_transition_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-send outbox write failure is visible and not counted successful."""

    import medre.runtime.retry as retry_module
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-success-write-fail",
        event_id="evt-success-write-fail",
        route_id="route-success-write-fail",
        delivery_plan_id="plan-success-write-fail",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    sent = DeliveryReceipt(
        receipt_id="rcpt-success-2",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        route_id=item.route_id,
        status="sent",
        attempt_number=2,
        outbox_id=item.outbox_id,
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=object())
    )
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock(
        return_value=evidence_for_receipt(sent)
    )
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_success = AsyncMock(
        side_effect=RuntimeError("injected sent transition failure")
    )
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
    assert worker.state.failed == 1
    failed_event = next(
        call for call in emit.call_args_list if call.args[0] == "retry_failed"
    )
    assert failed_event.args[1]["status"] == "lifecycle_persistence_error"
    assert failed_event.args[1]["error"] == (
        "RuntimeError: injected sent transition failure"
    )


async def test_retry_worker_abandons_outbox_when_content_authority_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied owner-bound read abandons the attempt without a native call."""
    import medre.runtime.retry as retry_module
    from medre.core.engine.pipeline.delivery_lifecycle import (
        DeliveryLifecycleService,
        RetryAttemptCommitRejected,
    )
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-denied",
        event_id="evt-denied",
        route_id="route-denied",
        delivery_plan_id="plan-denied",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-denied",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_metadata = AsyncMock(return_value=object())
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="denied", event=None)
    )
    storage.delivery_status = AsyncMock(return_value=None)
    storage.list_receipts_for_delivery = AsyncMock(return_value=[])
    storage.list_receipts_for_outbox = AsyncMock(return_value=[])
    storage.reserve_outbox_attempt = AsyncMock(return_value=2)
    storage.renew_outbox_lease = AsyncMock(return_value=True)
    storage.mark_outbox_abandoned = AsyncMock(return_value=True)
    pipeline = MagicMock()
    pipeline.deliver_execution_to_target = AsyncMock()
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: ReconstructedRetryPlan(
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
    )
    assert isinstance(worker._lifecycle, DeliveryLifecycleService)
    # This test exercises a backend without native-evidence reads; generic
    # MagicMock attributes must not simulate an installed resolver.
    worker._handoff_resolver = None
    emit = MagicMock()
    monkeypatch.setattr(worker, "_emit", emit)

    await worker._retry_outbox_item(item)

    pipeline.deliver_execution_to_target.assert_not_awaited()
    storage.mark_outbox_abandoned.assert_awaited_once()
    abandon_call = storage.mark_outbox_abandoned.await_args
    assert abandon_call.args == (item.outbox_id,)
    assert abandon_call.kwargs["error_summary"] == "content_authority_denied"
    assert abandon_call.kwargs["expected_worker_id"] == item.worker_id
    assert worker.state.processed == 1
    assert worker.state.succeeded == 0
    assert worker.state.failed == 1

    storage.mark_outbox_abandoned.reset_mock()
    abandon = AsyncMock(
        side_effect=RetryAttemptCommitRejected("claim moved to a newer worker")
    )
    monkeypatch.setattr(worker._lifecycle, "abandon_retry_outbox", abandon)
    await worker._retry_outbox_item(item)
    abandon.assert_awaited_once()
    assert worker.state.processed == 2
    assert worker.state.succeeded == 0
    # A rejected terminal CAS is not a second committed failure.
    assert worker.state.failed == 1


async def test_reconciled_queued_outcome_reaches_the_event_buffer() -> None:
    """A reconciled queue admission emits ``retry_queued`` on a real buffer.

    Queue admission is neither delivery success nor failure: the counters
    must not move, and the event must carry the admitted receipt identity.
    """

    from medre.core.engine.pipeline.delivery_lifecycle import (
        RetryAttemptFinalization,
    )
    from medre.core.storage.backend import DeliveryOutboxItem
    from medre.runtime.events import EventBuffer, RuntimeEventType
    from medre.runtime.retry import RetryWorker

    item = DeliveryOutboxItem(
        outbox_id="obox-queued-reconciled",
        event_id="evt-queued-reconciled",
        route_id="route-queued-reconciled",
        delivery_plan_id="plan-queued-reconciled",
        target_adapter="target_a",
        attempt_number=1,
        status="in_progress",
        worker_id="retry-worker-test",
    )
    storage = MagicMock()
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.count_outbox_by_status = AsyncMock(return_value={})
    pipeline = MagicMock()
    event_buffer = EventBuffer(maxlen=64)

    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
        interval_seconds=300,
        event_buffer=event_buffer,
        stop_timeout_seconds=2.0,
    )

    worker._record_retry_finalization(
        item,
        RetryAttemptFinalization(
            outcome="queued",
            receipt_id="rcpt-queued-reconciled",
            failure_kind=None,
            attempt_number=1,
        ),
        reconciled=True,
    )

    events = list(event_buffer)
    queued = [
        event for event in events if event.event_type == RuntimeEventType.RETRY_QUEUED
    ]
    assert len(queued) == 1
    assert queued[0].detail["event_id"] == item.event_id
    assert queued[0].detail["receipt_id"] == "rcpt-queued-reconciled"
    assert queued[0].detail["attempt_number"] == 1
    assert queued[0].detail["reconciled"] is True
    # Queue admission is distinct from delivery success and failure.
    assert worker.state.succeeded == 0
    assert worker.state.failed == 0
    assert worker.state.dead_lettered == 0
