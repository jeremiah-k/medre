"""Pre-dispatch uncertain-native-handoff evidence recheck wiring.

The retry worker runs a single bounded evidence recheck through
``DeliveryLifecycleService.recheck_last_uncertain_handoff`` immediately
before a reclaimed row dispatches a NEW attempt, gated on the row's last
finalized attempt having been consumed as an explicitly uncertain native
handoff.  These tests pin the worker-side surface only: the four gate
conditions, the delegation arguments, the settle-then-skip-transport path,
the superseded-commit-rejection handling, and the persistence-error
accounting.  The lifecycle authority's verdict logic itself is covered in
``tests/test_uncertain_handoff_resolution.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import medre.runtime.retry as retry_module
from medre.core.engine.pipeline.delivery_lifecycle import (
    RetryAttemptCommitRejected,
    RetryAttemptFinalization,
)
from medre.core.engine.pipeline.retry_plan import ReconstructedRetryPlan
from medre.core.events import DeliveryReceipt
from medre.core.evidence.handoff_resolution import (
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
    UncertainHandoffResolution,
)
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.retry import RetryWorker

# ---------------------------------------------------------------------------
# Fakes (same pattern as tests/test_retry_superseded_transition.py)
# ---------------------------------------------------------------------------

_PLAN_ID = "plan-handoff-recheck"
_WORKER = "retry-worker-recheck"


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
        "failure_kind_detail": UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    }
    fields.update(overrides)
    return DeliveryOutboxItem(**fields)


def _retained_storage(
    *,
    evidence_methods=("get_radio_attempt", "list_delivery_observations_for_attempt"),
):
    """Expose only explicit evidence-read surfaces, not dynamic mock attributes."""
    storage = MagicMock(
        spec=[
            "get_metadata",
            "get",
            "delivery_status",
            "get_outbox_dispatch_context",
            "list_receipts_for_outbox",
            "read_event_for_outbox",
            *evidence_methods,
        ]
    )
    for method in evidence_methods:
        setattr(storage, method, AsyncMock())
    stored = object()
    storage.get_metadata = AsyncMock(return_value=stored)
    storage.get = AsyncMock(return_value=stored)
    storage.delivery_status = AsyncMock(return_value=None)
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="retained", event=stored)
    )
    return storage


def _lifecycle(recheck_result) -> MagicMock:
    """Lifecycle fake; ``reserve_retry_attempt`` returns None so a normal
    dispatch stops at the lost-claim boundary without invoking transport.

    Every lifecycle surface the worker awaits is an ``AsyncMock`` so
    await-expectation assertions are valid on each fake method."""
    lifecycle = MagicMock()
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.reconcile_content_denied_handoff = AsyncMock(return_value=None)
    lifecycle.settle_committed_acceptance = AsyncMock(return_value=None)
    lifecycle.recheck_last_uncertain_handoff = AsyncMock(return_value=recheck_result)
    lifecycle.list_attempt_receipts = AsyncMock(return_value=[])
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=None)
    lifecycle.abandon_retry_outbox = AsyncMock(return_value=None)
    return lifecycle


def _worker(storage, lifecycle):
    pipeline = MagicMock()
    # The transport entry point is awaited by the worker, so the fake must
    # support await-expectation assertions like any lifecycle surface.
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


async def _run_with_emit(worker, item) -> MagicMock:
    emit = MagicMock()
    worker._emit = emit
    await worker._retry_outbox_item(item)
    return emit


# ---------------------------------------------------------------------------
# Gate conditions: each false clause leaves the recheck dormant and the
# normal dispatch path resumes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence_methods",
    [(), ("get_radio_attempt",), ("list_delivery_observations_for_attempt",)],
)
async def test_recheck_gate_resolver_absent_skips_recheck(evidence_methods):
    """Backends without the evidence-store surface leave the gate dormant."""
    storage = _retained_storage(evidence_methods=evidence_methods)
    lifecycle = _lifecycle(recheck_result=None)
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    assert worker._handoff_resolver is None
    emit = await _run_with_emit(worker, item)

    lifecycle.recheck_last_uncertain_handoff.assert_not_called()
    assert lifecycle.reserve_retry_attempt.await_count == 1
    pipeline.deliver_execution_to_target.assert_not_called()
    assert worker.state.processed == 1
    emit.assert_not_called()


async def test_recheck_gate_active_attempt_skips_recheck():
    """A row with a live reserved attempt never rechecks before dispatch."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    worker, _pipeline = _worker(storage, lifecycle)
    item = _gated_item(active_attempt=2)

    emit = await _run_with_emit(worker, item)

    lifecycle.recheck_last_uncertain_handoff.assert_not_called()
    assert lifecycle.reserve_retry_attempt.await_count == 1
    assert worker.state.processed == 1
    emit.assert_not_called()


async def test_recheck_gate_unstarted_attempt_skips_recheck():
    """Attempt zero has no finalized attempt to recheck evidence for."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    lifecycle.renew_retry_lease = AsyncMock(return_value=False)
    worker, _pipeline = _worker(storage, lifecycle)
    item = _gated_item(attempt_number=0)

    await _run_with_emit(worker, item)

    lifecycle.recheck_last_uncertain_handoff.assert_not_called()
    assert lifecycle.renew_retry_lease.await_count == 1
    assert worker.state.processed == 1


async def test_recheck_gate_other_failure_detail_skips_recheck():
    """Only the explicit uncertain-native-handoff detail arms the gate."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    worker, _pipeline = _worker(storage, lifecycle)
    item = _gated_item(failure_kind_detail="adapter_transient")

    await _run_with_emit(worker, item)

    lifecycle.recheck_last_uncertain_handoff.assert_not_called()
    assert lifecycle.reserve_retry_attempt.await_count == 1
    assert worker.state.processed == 1


# ---------------------------------------------------------------------------
# Delegation: every gate clause true delegates exactly once with the worker's
# resolver, then unresolved evidence resumes normal dispatch.
# ---------------------------------------------------------------------------


async def test_recheck_delegates_once_when_gated_in():
    """The recheck receives the lifecycle storage, row, policy, and resolver."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    worker, _pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    assert isinstance(worker._handoff_resolver, EvidenceBackedHandoffResolver)
    await _run_with_emit(worker, item)

    assert lifecycle.recheck_last_uncertain_handoff.await_count == 1
    call = lifecycle.recheck_last_uncertain_handoff.await_args
    assert call.args[:2] == (storage, item)
    assert isinstance(call.args[2], RetryPolicy)
    assert call.args[2].max_attempts == 3
    assert call.kwargs == {"handoff_resolver": worker._handoff_resolver}
    # Unresolved evidence: the normal dispatch resumed (reserve reached).
    assert lifecycle.reserve_retry_attempt.await_count == 1
    assert worker.state.processed == 1


# ---------------------------------------------------------------------------
# Settlement: a resolved recheck finalizes without any transport work.
# ---------------------------------------------------------------------------


async def test_recheck_settlement_skips_transport():
    """Newly readable evidence settles the obligation as sent."""
    storage = _retained_storage()
    lifecycle = _lifecycle(
        recheck_result=RetryAttemptFinalization(
            outcome="accepted",
            receipt_id="rcpt-recheck-settled",
            failure_kind=None,
            attempt_number=1,
        )
    )
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    emit = await _run_with_emit(worker, item)

    assert lifecycle.recheck_last_uncertain_handoff.await_count == 1
    lifecycle.reserve_retry_attempt.assert_not_called()
    pipeline.deliver_execution_to_target.assert_not_called()
    # Missing dispatch context consults the content authority even on the
    # settlement path; a retained verdict lets the recheck settle undisturbed.
    storage.read_event_for_outbox.assert_awaited_once_with(
        "evt-handoff-recheck", "obox-handoff-recheck"
    )
    assert worker.state.processed == 1
    assert worker.state.succeeded == 1
    assert worker.state.failed == 0
    emit.assert_called_once()
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert set(emitted) == {"retry_succeeded"}
    assert emitted["retry_succeeded"]["receipt_id"] == "rcpt-recheck-settled"
    assert emitted["retry_succeeded"]["attempt_number"] == 1
    assert emitted["retry_succeeded"]["reconciled"] is True


async def test_recheck_not_submitted_settlement_returns_to_retry_wait():
    """Definitively-not-submitted evidence finalizes without dispatching."""
    storage = _retained_storage()
    lifecycle = _lifecycle(
        recheck_result=RetryAttemptFinalization(
            outcome="retry_wait",
            receipt_id=None,
            failure_kind="ambiguous_dispatch",
            attempt_number=1,
            next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
    )
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    emit = await _run_with_emit(worker, item)

    lifecycle.reserve_retry_attempt.assert_not_called()
    pipeline.deliver_execution_to_target.assert_not_called()
    assert worker.state.processed == 1
    assert worker.state.failed == 1
    assert worker.state.succeeded == 0
    emit.assert_called_once()
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert set(emitted) == {"retry_failed"}
    assert emitted["retry_failed"]["status"] == "retry_wait"
    assert emitted["retry_failed"]["failure_kind"] == "ambiguous_dispatch"


# ---------------------------------------------------------------------------
# Guarded recheck failures: a lost claim is stale work, and a storage failure
# is process-local observability only — neither may touch the transport.
# ---------------------------------------------------------------------------


async def test_recheck_superseded_commit_rejection_accounts_stale_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CAS rejection during the recheck is stale work, not a delivery."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    lifecycle.recheck_last_uncertain_handoff = AsyncMock(
        side_effect=RetryAttemptCommitRejected("claim moved to a newer worker")
    )
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    with caplog.at_level(logging.WARNING):
        emit = await _run_with_emit(worker, item)

    assert lifecycle.recheck_last_uncertain_handoff.await_count == 1
    lifecycle.reserve_retry_attempt.assert_not_called()
    pipeline.deliver_execution_to_target.assert_not_called()
    assert worker.state.processed == 1
    assert worker.state.succeeded == 0
    assert worker.state.failed == 0
    emit.assert_not_called()
    assert "superseded transition ignored" in caplog.text


async def test_recheck_persistence_error_records_local_observability():
    """A recheck storage failure is accounted without inventing durable state."""
    storage = _retained_storage()
    lifecycle = _lifecycle(recheck_result=None)
    lifecycle.recheck_last_uncertain_handoff = AsyncMock(
        side_effect=RuntimeError("evidence recheck storage unavailable")
    )
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()

    emit = await _run_with_emit(worker, item)

    lifecycle.reserve_retry_attempt.assert_not_called()
    pipeline.deliver_execution_to_target.assert_not_called()
    assert worker.state.processed == 1
    assert worker.state.failed == 1
    assert worker.state.succeeded == 0
    emit.assert_called_once()
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert set(emitted) == {"retry_failed"}
    assert emitted["retry_failed"]["status"] == "lifecycle_persistence_error"
    assert emitted["retry_failed"]["error"] == (
        "RuntimeError: evidence recheck storage unavailable"
    )
    assert emitted["retry_failed"]["attempt_number"] == item.attempt_number


@pytest.mark.parametrize("count", [0, 2])
async def test_recheck_passes_observation_count_to_reservation(count):
    storage = _retained_storage()
    lifecycle = _lifecycle(
        UncertainHandoffResolution(
            "unresolved",
            1,
            True,
            ("unresolved",),
            False,
            observation_count=count,
        )
    )
    worker, pipeline = _worker(storage, lifecycle)
    item = _gated_item()
    emit = await _run_with_emit(worker, item)
    lifecycle.reserve_retry_attempt.assert_awaited_once_with(
        storage,
        item,
        expected_observation_count=count,
        expected_receipt_count=0,
        expected_checkpoint_revision=None,
        expected_observation_scope_count=None,
    )
    pipeline.deliver_execution_to_target.assert_not_called()
    emit.assert_not_called()


async def test_unreadable_recheck_never_reserves_or_abandons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed evidence read does not silently remove the preflight fence."""
    storage = _retained_storage()
    lifecycle = _lifecycle(
        UncertainHandoffResolution(
            "unresolved",
            1,
            True,
            ("resolution_evidence_unreadable",),
            False,
            observation_count=None,
        )
    )
    worker, pipeline = _worker(storage, lifecycle)
    with caplog.at_level(logging.WARNING):
        await _run_with_emit(worker, _gated_item())
    lifecycle.reserve_retry_attempt.assert_not_awaited()
    lifecycle.abandon_retry_outbox.assert_not_awaited()
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert "deferring dispatch until a fresh claim" in caplog.text


# ---------------------------------------------------------------------------
# Claim identity: every poll cycle owns its claims under a fresh,
# untruncated retry-worker UUID so claim provenance stays attributable.
# ---------------------------------------------------------------------------


async def test_claim_owner_identifier_is_full_uuid_hex(monkeypatch):
    """The poll claims due rows as retry-worker-<full uuid4 hex>."""
    storage = _retained_storage()
    claim = AsyncMock(return_value=[])
    storage.claim_due_outbox_items = claim
    owner = uuid.UUID("12345678123456781234567812345678")
    monkeypatch.setattr(retry_module.uuid, "uuid4", lambda: owner)
    worker, _pipeline = _worker(storage, _lifecycle(None))

    await worker._process_due(datetime.now(timezone.utc))

    assert claim.await_args.kwargs["worker_id"] == f"retry-worker-{owner.hex}"


async def test_committed_acceptance_records_before_visible_receipt_guard():
    """A committed acceptance is recorded before the receipt early return."""
    item = _gated_item(metadata={})
    storage = _retained_storage()
    storage.read_event_for_outbox.return_value = SimpleNamespace(status="expired")
    lifecycle = _lifecycle(None)
    lifecycle.reconcile_retry_claim = AsyncMock(
        return_value=RetryAttemptFinalization(
            outcome="accepted",
            receipt_id="rcpt-visible-acceptance",
            failure_kind=None,
            attempt_number=1,
        )
    )
    lifecycle.list_attempt_receipts = AsyncMock(
        return_value=[
            DeliveryReceipt(
                receipt_id="rcpt-visible-acceptance",
                event_id=item.event_id,
                delivery_plan_id=item.delivery_plan_id,
                target_adapter=item.target_adapter,
                outbox_id=item.outbox_id,
                attempt_number=1,
                status="sent",
                source="retry",
            )
        ]
    )
    finalization = RetryAttemptFinalization(
        outcome="accepted",
        receipt_id="rcpt-visible-acceptance",
        failure_kind=None,
        attempt_number=1,
    )
    lifecycle.settle_committed_acceptance = AsyncMock(return_value=finalization)
    worker, pipeline = _worker(storage, lifecycle)

    emit = await _run_with_emit(worker, item)

    lifecycle.settle_committed_acceptance.assert_awaited_once()
    lifecycle.abandon_retry_outbox.assert_not_awaited()
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1
    assert worker.state.succeeded == 1
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert emitted["retry_succeeded"]["receipt_id"] == "rcpt-visible-acceptance"
    assert emitted["retry_succeeded"]["reconciled"] is True


async def test_expired_dispatch_context_fences_existing_failed_receipt():
    item = _gated_item(failure_kind_detail=None)
    storage = _retained_storage()
    storage.read_event_for_outbox.return_value = SimpleNamespace(status="expired")
    lifecycle = _lifecycle(recheck_result=None)
    lifecycle.list_attempt_receipts = AsyncMock(
        return_value=[
            DeliveryReceipt(
                receipt_id="failed-before-expiry",
                event_id=item.event_id,
                delivery_plan_id=item.delivery_plan_id,
                target_adapter=item.target_adapter,
                outbox_id=item.outbox_id,
                attempt_number=item.attempt_number,
                status="failed",
            )
        ]
    )
    worker, pipeline = _worker(storage, lifecycle)

    await worker._retry_outbox_item(item)

    lifecycle.abandon_retry_outbox.assert_awaited_once_with(
        storage,
        item,
        error_summary="content_authority_denied",
        expected_observation_count=None,
        expected_checkpoint_revision=None,
        expected_receipt_count=1,
        expected_observation_scope_count=None,
    )
    pipeline.deliver_execution_to_target.assert_not_awaited()


async def test_content_denied_settles_committed_receipt_before_abandon():
    """A persisted sent receipt is terminal even when content is denied."""
    item = _gated_item(metadata={})
    storage = _retained_storage()
    storage.read_event_for_outbox.return_value = SimpleNamespace(status="expired")
    lifecycle = _lifecycle(None)
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
        receipt_id="sent-current-attempt",
        failure_kind=None,
        attempt_number=1,
    )
    lifecycle.settle_committed_acceptance = AsyncMock(return_value=finalization)
    worker, pipeline = _worker(storage, lifecycle)

    emit = await _run_with_emit(worker, item)

    lifecycle.settle_committed_acceptance.assert_awaited_once()
    lifecycle.abandon_retry_outbox.assert_not_awaited()
    pipeline.deliver_execution_to_target.assert_not_awaited()
    assert worker.state.processed == 1
    assert worker.state.succeeded == 1
    emitted = {call.args[0]: call.args[1] for call in emit.call_args_list}
    assert emitted["retry_succeeded"]["receipt_id"] == "sent-current-attempt"
