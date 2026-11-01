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
from medre.core.engine.pipeline.retry_plan import ReconstructedRetryPlan
from medre.core.events.canonical import (
    DeliveryReceipt,
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
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    stored = object()
    storage.get_metadata = AsyncMock(return_value=stored)
    storage.get = AsyncMock(return_value=stored)
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=stored)
    )
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
    assert worker.state.failed == 0
    event_types = [call.args[0] for call in emit.call_args_list]
    assert "retry_attempted" in event_types
    assert "retry_succeeded" not in event_types
    assert "retry_failed" not in event_types
