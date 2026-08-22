"""Tests for retry-worker outbox transitions owned by the lifecycle service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from tests.helpers.storage_outbox import create_outbox_item_with_parent

from .conftest import _make_lifecycle, _make_receipt


def _retry_item(
    *,
    outbox_id: str,
    event_id: str,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-retry",
        delivery_plan_id=f"plan-{outbox_id}",
        target_adapter="test_adapter",
        status="in_progress",
        attempt_number=attempt_number,
    )


async def test_abandon_retry_outbox_is_lifecycle_owned(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-abandon", event_id="evt-abandon")
    await create_outbox_item_with_parent(temp_storage, item)

    await lifecycle.abandon_retry_outbox(
        temp_storage,
        item,
        error_summary="Reconstruction failure",
    )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "abandoned"
    assert updated.error_summary == "Reconstruction failure"


async def test_defer_retry_outbox_computes_and_persists_backoff(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-defer", event_id="evt-defer")
    await create_outbox_item_with_parent(temp_storage, item)
    policy = RetryPolicy(
        max_attempts=3,
        backoff_base=2.0,
        max_delay_seconds=30.0,
        jitter=False,
    )
    now = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)

    next_at = await lifecycle.defer_retry_outbox(
        temp_storage,
        item,
        policy,
        failure_kind="capacity_rejection",
        attempt_number=1,
        now=now,
    )

    assert next_at == now + timedelta(seconds=2)
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "retry_wait"
    assert updated.failure_kind == "capacity_rejection"
    assert updated.next_attempt_at == next_at.isoformat()
    assert updated.attempt_number == 1


async def test_finalize_retry_failure_schedules_available_attempt(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-retry", event_id="evt-retry")
    await create_outbox_item_with_parent(temp_storage, item)
    policy = RetryPolicy(max_attempts=3, backoff_base=1.0, jitter=False)

    terminal = await lifecycle.finalize_retry_failure(
        temp_storage,
        item,
        policy,
        failure_kind="adapter_transient",
        attempt_number=2,
        now=datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
    )

    assert terminal is False
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "retry_wait"
    assert updated.attempt_number == 2


async def test_finalize_retry_failure_dead_letters_exhausted_attempt(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-dead", event_id="evt-dead")
    await create_outbox_item_with_parent(temp_storage, item)
    policy = RetryPolicy(max_attempts=3, backoff_base=1.0, jitter=False)

    terminal = await lifecycle.finalize_retry_failure(
        temp_storage,
        item,
        policy,
        failure_kind="adapter_transient",
        attempt_number=4,
    )

    assert terminal is True
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.attempt_number == 4


async def test_finalize_retry_failure_honors_existing_dead_letter_evidence(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-forced", event_id="evt-forced")
    await create_outbox_item_with_parent(temp_storage, item)
    policy = RetryPolicy(max_attempts=5, backoff_base=1.0, jitter=False)

    terminal = await lifecycle.finalize_retry_failure(
        temp_storage,
        item,
        policy,
        failure_kind="retry_exhausted",
        attempt_number=2,
        receipt_id="rcpt-dead",
        force_dead_lettered=True,
    )

    assert terminal is True
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.receipt_id == "rcpt-dead"


async def test_finalize_retry_success_preserves_queued_result(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-queued", event_id="evt-queued")
    await create_outbox_item_with_parent(temp_storage, item)
    receipt = _make_receipt(
        receipt_id="rcpt-queued",
        status="queued",
        attempt_number=1,
        event_id=item.event_id,
        plan_id=item.delivery_plan_id,
    )

    await lifecycle.finalize_retry_success(temp_storage, item, receipt)

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "queued"
    assert updated.receipt_id == receipt.receipt_id


async def test_finalize_retry_success_marks_sent_result_terminal(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-sent", event_id="evt-sent")
    await create_outbox_item_with_parent(temp_storage, item)
    receipt = _make_receipt(
        receipt_id="rcpt-sent",
        status="sent",
        attempt_number=1,
        event_id=item.event_id,
        plan_id=item.delivery_plan_id,
    )

    await lifecycle.finalize_retry_success(temp_storage, item, receipt)

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "sent"
    assert updated.receipt_id == receipt.receipt_id
