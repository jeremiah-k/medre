"""Edge coverage for terminal lifecycle authority decisions.

These tests pin fail-closed and fallback branches of the lifecycle-owned
terminalization path: typed execution evidence winning failure
classification, rejection of unsupported lifecycle authority statuses,
terminal-attempt derivation when no attempt evidence exists, and the
append variant of terminal lifecycle evidence construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.core.engine.pipeline.delivery_evidence import (
    DeliveryExecutionEvidence,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
)
from medre.core.storage.backend import DeliveryOutboxItem

from .conftest import _make_lifecycle, _make_receipt


class _EvidenceError(Exception):
    """Retry-path exception carrying typed execution evidence."""

    def __init__(self, evidence: DeliveryExecutionEvidence) -> None:
        self.evidence = evidence
        super().__init__("typed evidence failure")


def test_execution_evidence_failure_kind_wins_in_retry_classification() -> None:
    """A typed DeliveryExecutionEvidence on the exception classifies it."""
    lifecycle = _make_lifecycle()
    attempt = _make_receipt(
        receipt_id="rcpt-evidence-classify",
        status="failed",
        attempt_number=2,
        failure_kind="adapter_permanent",
    )
    evidence = DeliveryExecutionEvidence(
        attempt_receipt=attempt,
        failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
    )

    kind = lifecycle._classify_retry_exception(_EvidenceError(evidence))

    assert kind is DeliveryFailureKind.RENDERER_FAILURE


async def test_finalize_outbox_outcome_rejects_unsupported_authority_status(
    temp_storage,
) -> None:
    """A lifecycle authority status outside the terminal set fails closed."""
    lifecycle = _make_lifecycle()
    await temp_storage.append(_minimal_event("evt-unsupported-authority"))
    item = DeliveryOutboxItem(
        outbox_id="obox-unsupported-authority",
        event_id="evt-unsupported-authority",
        route_id="route-1",
        delivery_plan_id="plan-unsupported",
        target_adapter="test_adapter",
        status="in_progress",
        attempt_number=1,
        active_attempt=2,
        worker_id="worker-a",
    )
    await temp_storage.create_outbox_item(item)
    attempt = _make_receipt(
        receipt_id="rcpt-unsupported-attempt",
        status="failed",
        attempt_number=2,
        event_id="evt-unsupported-authority",
        plan_id="plan-unsupported",
        failure_kind="adapter_permanent",
        outbox_id="obox-unsupported-authority",
    )
    await temp_storage.append_receipt(attempt)
    suppressed = _make_receipt(
        receipt_id="rcpt-unsupported-authority",
        status="suppressed",
        attempt_number=2,
        event_id="evt-unsupported-authority",
        plan_id="plan-unsupported",
        parent_receipt_id=attempt.receipt_id,
        outbox_id="obox-unsupported-authority",
    )
    await temp_storage.append_receipt(suppressed)
    evidence = DeliveryExecutionEvidence(
        attempt_receipt=attempt,
        authority_receipt=suppressed,
        failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
    )

    # Fail-closed: the unsupported authority status is rejected internally,
    # the finalizer reports no commit, and the row is left untouched.
    committed = await lifecycle.finalize_outbox_outcome(
        temp_storage,
        outbox_id="obox-unsupported-authority",
        outbox_created=True,
        evidence=evidence,
        retry_policy=None,
        expected_worker_id="worker-a",
    )

    assert committed is False
    row = await temp_storage.get_outbox_item("obox-unsupported-authority")
    assert row is not None
    assert row.status == "in_progress"
    assert row.active_attempt == 2


async def test_terminal_attempt_falls_back_to_the_outbox_row(temp_storage) -> None:
    """Without attempt evidence or a reservation, terminalization derives the
    attempt from the outbox row's effective generation instead of guessing."""
    lifecycle = _make_lifecycle()
    await temp_storage.append(_minimal_event("evt-fallback-attempt"))
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="obox-fallback-attempt",
            event_id="evt-fallback-attempt",
            route_id="route-1",
            delivery_plan_id="plan-fallback",
            target_adapter="test_adapter",
            status="in_progress",
            attempt_number=3,
            worker_id="worker-b",
        )
    )
    evidence = DeliveryExecutionEvidence(
        failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
        error="planner refused before any dispatch evidence persisted",
    )

    committed = await lifecycle.finalize_outbox_outcome(
        temp_storage,
        outbox_id="obox-fallback-attempt",
        outbox_created=True,
        evidence=evidence,
        retry_policy=None,
        expected_worker_id="worker-b",
    )

    assert committed is True
    row = await temp_storage.get_outbox_item("obox-fallback-attempt")
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.attempt_number == 3


async def test_terminal_attempt_prefers_the_reserved_generation(temp_storage) -> None:
    """A supplied reservation wins over re-reading the outbox row."""
    lifecycle = _make_lifecycle()
    await temp_storage.append(_minimal_event("evt-reserved-attempt"))
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="obox-reserved-attempt",
            event_id="evt-reserved-attempt",
            route_id="route-1",
            delivery_plan_id="plan-reserved",
            target_adapter="test_adapter",
            status="in_progress",
            attempt_number=2,
            active_attempt=3,
            worker_id="worker-c",
        )
    )
    original_get = temp_storage.get_outbox_item
    spy = AsyncMock(wraps=original_get)
    temp_storage.get_outbox_item = spy  # type: ignore[method-assign]
    evidence = DeliveryExecutionEvidence(
        failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
        error="no dispatch evidence survived",
    )

    try:
        committed = await lifecycle.finalize_outbox_outcome(
            temp_storage,
            outbox_id="obox-reserved-attempt",
            outbox_created=True,
            evidence=evidence,
            retry_policy=None,
            reserved_attempt_number=3,
            expected_worker_id="worker-c",
        )
        assert committed is True
        spy.assert_not_awaited()
    finally:
        temp_storage.get_outbox_item = original_get  # type: ignore[method-assign]

    row = await temp_storage.get_outbox_item("obox-reserved-attempt")
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.attempt_number == 3


async def test_build_and_persist_terminal_lifecycle_receipt_appends_linked_evidence(
    temp_storage,
) -> None:
    """The append variant persists the same-attempt linked lifecycle receipt."""
    lifecycle = _make_lifecycle()
    await temp_storage.append(_minimal_event("evt-append-terminal"))
    attempt = _make_receipt(
        receipt_id="rcpt-append-terminal-attempt",
        status="failed",
        attempt_number=4,
        event_id="evt-append-terminal",
        failure_kind="adapter_permanent",
    )
    await temp_storage.append_receipt(attempt)

    receipt = await lifecycle.build_and_persist_terminal_receipt(
        temp_storage,
        attempt,
        status="dead_lettered",
        error="budget exhausted",
        failure_kind="adapter_permanent",
    )

    assert receipt.receipt_kind == "lifecycle"
    assert receipt.status == "dead_lettered"
    assert receipt.attempt_number == attempt.attempt_number
    assert receipt.parent_receipt_id == attempt.receipt_id
    persisted = await temp_storage.list_receipts_for_event("evt-append-terminal")
    assert receipt.receipt_id in {r.receipt_id for r in persisted}


def _minimal_event(event_id: str):
    from medre.core.events.canonical import CanonicalEvent, EventMetadata

    return CanonicalEvent(
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
        payload={"text": "terminal authority edges"},
        metadata=EventMetadata(),
    )


async def test_success_commit_rejection_reconciliation_guards_stale_shapes(
    temp_storage,
) -> None:
    """Only a same-attempt committed terminal row reconciles a lost CAS."""
    lifecycle = _make_lifecycle()
    await temp_storage.append(_minimal_event("evt-reconcile-guards"))
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="obox-reconcile-guards",
            event_id="evt-reconcile-guards",
            route_id="route-1",
            delivery_plan_id="plan-reconcile",
            target_adapter="test_adapter",
            status="in_progress",
            attempt_number=1,
            worker_id="worker-d",
        )
    )
    failed_receipt = _make_receipt(
        receipt_id="rcpt-reconcile-failed",
        status="failed",
        attempt_number=2,
        event_id="evt-reconcile-guards",
        plan_id="plan-reconcile",
        outbox_id="obox-reconcile-guards",
    )

    # A non queued/sent receipt never reconciles.
    assert (
        await lifecycle.reconcile_retry_success_commit_rejection(
            temp_storage,
            DeliveryOutboxItem(
                outbox_id="obox-reconcile-guards",
                event_id="evt-reconcile-guards",
                route_id="route-1",
                delivery_plan_id="plan-reconcile",
                target_adapter="test_adapter",
                attempt_number=1,
                status="in_progress",
                worker_id="worker-d",
            ),
            failed_receipt,
        )
        is None
    )

    sent_receipt = _make_receipt(
        receipt_id="rcpt-reconcile-sent",
        status="sent",
        attempt_number=2,
        event_id="evt-reconcile-guards",
        plan_id="plan-reconcile",
        outbox_id="obox-reconcile-guards",
    )
    item = DeliveryOutboxItem(
        outbox_id="obox-reconcile-guards",
        event_id="evt-reconcile-guards",
        route_id="route-1",
        delivery_plan_id="plan-reconcile",
        target_adapter="test_adapter",
        attempt_number=1,
        status="in_progress",
        worker_id="worker-d",
    )

    # The row still sits at attempt 1 while the receipt claims attempt 2:
    # different dispatch generations never reconcile.
    assert (
        await lifecycle.reconcile_retry_success_commit_rejection(
            temp_storage, item, sent_receipt
        )
        is None
    )

    # A missing row never reconciles.
    ghost_item = DeliveryOutboxItem(
        outbox_id="obox-does-not-exist",
        event_id="evt-reconcile-guards",
        route_id="route-1",
        delivery_plan_id="plan-reconcile",
        target_adapter="test_adapter",
        attempt_number=2,
        status="in_progress",
    )
    assert (
        await lifecycle.reconcile_retry_success_commit_rejection(
            temp_storage, ghost_item, sent_receipt
        )
        is None
    )

    # A committed same-attempt terminal row reconciles to its outcome.
    await temp_storage.append_receipt(sent_receipt)
    committed = await temp_storage.mark_outbox_sent(
        "obox-reconcile-guards",
        receipt_id=sent_receipt.receipt_id,
        attempt_number=2,
        expected_worker_id="worker-d",
    )
    assert committed is True

    finalization = await lifecycle.reconcile_retry_success_commit_rejection(
        temp_storage, item, sent_receipt
    )
    assert finalization is not None
    assert finalization.outcome == "accepted"
    assert finalization.receipt_id == sent_receipt.receipt_id
    assert finalization.attempt_number == 2
