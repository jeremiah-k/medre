"""Tests for retry-worker outbox transitions owned by the lifecycle service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptCommitRejected
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from tests.helpers.storage_outbox import create_outbox_item_with_parent

from .conftest import _make_lifecycle, _make_receipt


def _retry_item(
    *,
    outbox_id: str,
    event_id: str,
    attempt_number: int = 1,
    target_channel: str | None = None,
    receipt_id: str | None = None,
    active_attempt: int | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-retry",
        delivery_plan_id=f"plan-{outbox_id}",
        target_adapter="test_adapter",
        target_channel=target_channel,
        status="in_progress",
        attempt_number=attempt_number,
        active_attempt=active_attempt,
        receipt_id=receipt_id,
    )


class _FailingTransitionStorage:
    """Delegate reads while injecting one lifecycle-transition failure."""

    def __init__(self, delegate: StorageBackend, fail_method: str) -> None:
        self._delegate = delegate
        self._fail_method = fail_method

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def mark_outbox_retry_wait(self, *args: Any, **kwargs: Any) -> bool:
        if self._fail_method == "mark_outbox_retry_wait":
            raise RuntimeError("injected retry-wait persistence failure")
        return await self._delegate.mark_outbox_retry_wait(*args, **kwargs)

    async def mark_outbox_dead_lettered(self, *args: Any, **kwargs: Any) -> bool:
        if self._fail_method == "mark_outbox_dead_lettered":
            raise RuntimeError("injected dead-letter persistence failure")
        return await self._delegate.mark_outbox_dead_lettered(*args, **kwargs)

    async def finalize_outbox_terminal(self, *args: Any, **kwargs: Any) -> bool:
        if self._fail_method == "finalize_outbox_terminal":
            raise RuntimeError("injected dead-letter persistence failure")
        return await self._delegate.finalize_outbox_terminal(*args, **kwargs)

    async def mark_outbox_sent(self, *args: Any, **kwargs: Any) -> bool:
        if self._fail_method == "mark_outbox_sent":
            raise RuntimeError("injected sent persistence failure")
        return await self._delegate.mark_outbox_sent(*args, **kwargs)


class _RejectingTransitionStorage:
    """Delegate reads while simulating a guarded transition CAS miss."""

    def __init__(self, delegate: StorageBackend, reject_method: str) -> None:
        self._delegate = delegate
        self._reject_method = reject_method

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def mark_outbox_sent(self, *args: Any, **kwargs: Any) -> bool:
        if self._reject_method == "mark_outbox_sent":
            return False
        return await self._delegate.mark_outbox_sent(*args, **kwargs)


async def test_abandon_retry_outbox_is_lifecycle_owned(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-abandon",
        event_id="evt-abandon",
        attempt_number=1,
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)

    await lifecycle.abandon_retry_outbox(
        temp_storage,
        item,
        error_summary="Reconstruction failure",
    )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "abandoned"
    assert updated.attempt_number == 2
    assert updated.active_attempt is None
    assert updated.error_summary == "Reconstruction failure"


async def test_retry_success_rejects_guarded_storage_noop(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id="obox-stale-success", event_id="evt-stale-success")
    item = DeliveryOutboxItem(**{**item.__dict__, "worker_id": "retry-worker-stale"})
    receipt = _make_receipt(
        receipt_id="rcpt-stale-success",
        event_id=item.event_id,
        status="sent",
        attempt_number=2,
        outbox_id=item.outbox_id,
    )
    storage = _RejectingTransitionStorage(temp_storage, "mark_outbox_sent")

    with pytest.raises(RetryAttemptCommitRejected):
        await lifecycle.finalize_retry_success(storage, item, receipt)


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


async def test_finalize_retry_attempt_error_uses_current_failed_receipt_evidence(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-evidence",
        event_id="evt-evidence",
        attempt_number=1,
        target_channel="room-a",
        receipt_id="rcpt-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    next_at = datetime(2026, 8, 22, 12, 30, tzinfo=UTC)
    current = _make_receipt(
        receipt_id="rcpt-current",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        channel=item.target_channel,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_transient",
        error="ConnectionError: retry failed",
        next_retry_at=next_at,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(current)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
        error=RuntimeError("wrapper should not override receipt classification"),
    )

    assert result.outcome == "retry_wait"
    assert result.receipt_id == current.receipt_id
    assert result.failure_kind == "adapter_transient"
    assert result.attempt_number == 2
    assert result.next_retry_at == next_at
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "retry_wait"
    assert updated.receipt_id == current.receipt_id
    assert updated.failure_kind == "adapter_transient"
    assert updated.attempt_number == 2
    assert updated.next_attempt_at == next_at.isoformat()


async def test_retry_claim_reconciliation_rejects_receipt_without_outbox_id(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-missing-correlation",
        event_id="evt-missing-correlation",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    malformed = _make_receipt(
        receipt_id="rcpt-missing-correlation",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_transient",
        source="retry",
        outbox_id=None,
    )
    await temp_storage.append_receipt(malformed)

    with pytest.raises(ValueError, match="missing required outbox_id"):
        await lifecycle.reconcile_retry_claim(
            temp_storage,
            item,
            RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
        )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "in_progress"


@pytest.mark.parametrize("failure_kind", [None, "not-a-failure-kind"])
async def test_retry_claim_reconciliation_dead_letters_malformed_failure_kind(
    failure_kind: str | None,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-invalid-kind",
        event_id="evt-invalid-kind",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    malformed = _make_receipt(
        receipt_id="rcpt-invalid-kind",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind=failure_kind,
        source="retry",
        outbox_id=item.outbox_id,
    )
    storage = AsyncMock(spec=StorageBackend)
    storage.list_receipts_for_plan.return_value = [malformed]

    result = await lifecycle.reconcile_retry_claim(
        storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )

    assert result is not None
    assert result.outcome == "dead_lettered"
    assert result.failure_kind == "adapter_permanent"
    # Permanent failure terminalizes through the guarded transaction: a NEW
    # same-attempt lifecycle receipt is appended and the outbox pointer is
    # committed with it, rather than re-marking with the failed receipt.
    storage.finalize_outbox_terminal.assert_awaited_once()
    call = storage.finalize_outbox_terminal.await_args
    lifecycle_receipt = call.args[0]
    assert lifecycle_receipt.receipt_kind == "lifecycle"
    assert lifecycle_receipt.status == "dead_lettered"
    assert lifecycle_receipt.attempt_number == 2
    assert lifecycle_receipt.parent_receipt_id == malformed.receipt_id
    assert result.receipt_id == lifecycle_receipt.receipt_id
    assert call.kwargs["terminal_status"] == "dead_lettered"
    assert call.kwargs["attempt_number"] == 2
    assert call.kwargs["expected_worker_id"] is None


async def test_retry_claim_reconciliation_preserves_dead_letter_failure_kind() -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-terminal-kind",
        event_id="evt-terminal-kind",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    failed = _make_receipt(
        receipt_id="rcpt-terminal-failed",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_permanent",
        source="retry",
        outbox_id=item.outbox_id,
    )
    dead = _make_receipt(
        receipt_id="rcpt-terminal-dead",
        status="dead_lettered",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_permanent",
        parent_receipt_id=failed.receipt_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    storage = AsyncMock(spec=StorageBackend)
    storage.list_receipts_for_plan.return_value = [failed, dead]

    result = await lifecycle.reconcile_retry_claim(
        storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )

    assert result is not None
    assert result.outcome == "dead_lettered"
    assert result.receipt_id == dead.receipt_id
    assert result.failure_kind == "adapter_permanent"
    storage.mark_outbox_dead_lettered.assert_awaited_once_with(
        item.outbox_id,
        receipt_id=dead.receipt_id,
        failure_kind="adapter_permanent",
        error_summary=None,
        attempt_number=2,
        expected_worker_id=None,
    )


async def test_finalize_retry_attempt_error_links_existing_dead_letter_evidence(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-existing-dead",
        event_id="evt-existing-dead",
        attempt_number=2,
        target_channel="room-a",
        receipt_id="rcpt-attempt-2",
        active_attempt=3,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    failed = _make_receipt(
        receipt_id="rcpt-attempt-3",
        status="failed",
        attempt_number=3,
        event_id=item.event_id,
        adapter=item.target_adapter,
        channel=item.target_channel,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_transient",
        error="ConnectionError: exhausted",
        source="retry",
        outbox_id=item.outbox_id,
    )
    dead = _make_receipt(
        receipt_id="rcpt-dead-3",
        status="dead_lettered",
        attempt_number=3,
        event_id=item.event_id,
        adapter=item.target_adapter,
        channel=item.target_channel,
        plan_id=item.delivery_plan_id,
        error="ConnectionError: exhausted",
        parent_receipt_id=failed.receipt_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(failed)
    await temp_storage.append_receipt(dead)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=3, backoff_base=1.0, jitter=False),
        error=ConnectionError("exhausted"),
    )

    assert result.outcome == "dead_lettered"
    assert result.receipt_id == "rcpt-dead-3"
    assert result.failure_kind == "retry_exhausted"
    assert result.attempt_number == 3
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.receipt_id == "rcpt-dead-3"
    assert updated.failure_kind == "retry_exhausted"
    assert updated.attempt_number == 3
    assert updated.active_attempt is None


@pytest.mark.parametrize("terminal_status", ["cancelled", "abandoned"])
async def test_reconcile_retry_claim_commits_existing_lifecycle_terminal(
    temp_storage: StorageBackend,
    terminal_status: str,
) -> None:
    """Recovery honours terminal lifecycle evidence persisted before outbox CAS."""
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id=f"obox-existing-{terminal_status}",
        event_id=f"evt-existing-{terminal_status}",
        attempt_number=1,
        receipt_id="rcpt-parent",
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    attempt = _make_receipt(
        receipt_id=f"rcpt-{terminal_status}-attempt",
        status="queued",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    terminal = _make_receipt(
        receipt_id=f"rcpt-{terminal_status}-lifecycle",
        status=terminal_status,
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        error=f"retry {terminal_status}",
        parent_receipt_id=attempt.receipt_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(attempt)
    await temp_storage.append_receipt(terminal)

    result = await lifecycle.reconcile_retry_claim(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )

    assert result is not None
    assert result.outcome == terminal_status
    assert result.receipt_id == terminal.receipt_id
    assert result.attempt_number == 2
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == terminal_status
    assert updated.receipt_id == terminal.receipt_id
    assert updated.attempt_number == 2
    assert updated.active_attempt is None


@pytest.mark.parametrize("terminal_status", ["cancelled", "abandoned"])
async def test_retry_exception_cannot_override_existing_lifecycle_terminal(
    temp_storage: StorageBackend,
    terminal_status: str,
) -> None:
    """An exception cannot reschedule or dead-letter an explicit terminal callback."""
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id=f"obox-exception-{terminal_status}",
        event_id=f"evt-exception-{terminal_status}",
        attempt_number=1,
        receipt_id="rcpt-parent",
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    attempt = _make_receipt(
        receipt_id=f"rcpt-exception-{terminal_status}-attempt",
        status="queued",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    terminal = _make_receipt(
        receipt_id=f"rcpt-exception-{terminal_status}-lifecycle",
        status=terminal_status,
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        parent_receipt_id=attempt.receipt_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(attempt)
    await temp_storage.append_receipt(terminal)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
        error=ConnectionError("must not resurrect a terminal retry"),
    )

    assert result.outcome == terminal_status
    assert result.receipt_id == terminal.receipt_id
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == terminal_status
    assert updated.receipt_id == terminal.receipt_id
    assert updated.next_attempt_at is None





async def test_retry_reconciliation_rejects_unlinked_terminal_lifecycle_evidence(
    temp_storage: StorageBackend,
) -> None:
    """Malformed terminal lifecycle evidence cannot acquire outbox authority."""
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-unlinked-terminal",
        event_id="evt-unlinked-terminal",
        attempt_number=1,
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    terminal = _make_receipt(
        receipt_id="rcpt-unlinked-terminal",
        status="cancelled",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        parent_receipt_id="rcpt-missing-attempt",
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(terminal)

    with pytest.raises(ValueError, match="not linked to same-attempt attempt evidence"):
        await lifecycle.reconcile_retry_claim(
            temp_storage,
            item,
            RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
        )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "in_progress"
    assert updated.attempt_number == 1
    assert updated.active_attempt == 2


async def test_finalize_retry_attempt_error_dead_letters_permanent_failure(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-permanent",
        event_id="evt-permanent",
        attempt_number=1,
        receipt_id="rcpt-parent",
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    current = _make_receipt(
        receipt_id="rcpt-permanent",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_permanent",
        error="ValueError: rejected",
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(current)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=5, backoff_base=1.0, jitter=False),
        error=ConnectionError("receipt classification must win"),
    )

    assert result.outcome == "dead_lettered"
    assert result.failure_kind == "adapter_permanent"
    assert result.receipt_id != current.receipt_id
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.failure_kind == "adapter_permanent"
    assert updated.receipt_id == result.receipt_id
    assert updated.attempt_number == 2
    assert updated.active_attempt is None
    receipts = await temp_storage.list_receipts_for_event(item.event_id)
    lifecycle_rows = [r for r in receipts if r.receipt_kind == "lifecycle"]
    assert len(lifecycle_rows) == 1
    lifecycle_row = lifecycle_rows[0]
    assert lifecycle_row.status == "dead_lettered"
    assert lifecycle_row.attempt_number == 2
    assert lifecycle_row.parent_receipt_id == current.receipt_id


async def test_finalize_retry_attempt_error_preserves_accepted_receipt(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-accepted",
        event_id="evt-accepted",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    accepted = _make_receipt(
        receipt_id="rcpt-sent-after-error",
        status="sent",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(accepted)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
        error=RuntimeError("post-send native-ref persistence failed"),
    )

    assert result.outcome == "accepted"
    assert result.receipt_id == accepted.receipt_id
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "sent"
    assert updated.receipt_id == accepted.receipt_id
    assert updated.attempt_number == accepted.attempt_number


async def test_finalize_retry_attempt_error_ignores_unrelated_dead_letter(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-current",
        event_id="evt-current",
        attempt_number=1,
        target_channel="room-a",
        receipt_id="rcpt-current-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    unrelated_failed = _make_receipt(
        receipt_id="rcpt-other-failed",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        channel=item.target_channel,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_transient",
        outbox_id="obox-other",
    )
    unrelated_dead = _make_receipt(
        receipt_id="rcpt-other-dead",
        status="dead_lettered",
        attempt_number=3,
        event_id=item.event_id,
        adapter=item.target_adapter,
        channel=item.target_channel,
        plan_id=item.delivery_plan_id,
        parent_receipt_id=unrelated_failed.receipt_id,
        source="retry",
        outbox_id="obox-other",
    )
    await temp_storage.append_receipt(unrelated_failed)
    await temp_storage.append_receipt(unrelated_dead)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=5, backoff_base=2.0, jitter=False),
        error=ConnectionError("current attempt failed before receipt persistence"),
        now=now,
    )

    assert result.outcome == "retry_wait"
    assert result.receipt_id is None
    assert result.failure_kind == "adapter_transient"
    assert result.next_retry_at == now + timedelta(seconds=4)
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "retry_wait"
    assert updated.receipt_id == item.receipt_id
    assert updated.failure_kind == "adapter_transient"
    assert (
        updated.error_summary
        == "ConnectionError: current attempt failed before receipt persistence"
    )


async def test_finalize_retry_attempt_error_propagates_retry_wait_write_failure(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-retry-write-fail",
        event_id="evt-retry-write-fail",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    current = _make_receipt(
        receipt_id="rcpt-retry-write-fail",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_transient",
        next_retry_at=datetime(2026, 8, 22, 12, 30, tzinfo=UTC),
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(current)
    failing = _FailingTransitionStorage(temp_storage, "mark_outbox_retry_wait")

    with pytest.raises(RuntimeError, match="retry-wait persistence failure"):
        await lifecycle.finalize_retry_attempt_error(
            failing,  # type: ignore[arg-type]
            item,
            RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
            error=ConnectionError("retry failed"),
        )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "in_progress"

    repaired = await lifecycle.reconcile_retry_claim(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )
    assert repaired is not None
    assert repaired.outcome == "retry_wait"
    assert repaired.receipt_id == current.receipt_id
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "retry_wait"
    assert updated.receipt_id == current.receipt_id
    assert updated.next_attempt_at == current.next_retry_at.isoformat()


async def test_finalize_retry_attempt_error_propagates_dead_letter_write_failure(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-dead-write-fail",
        event_id="evt-dead-write-fail",
        attempt_number=1,
        receipt_id="rcpt-parent",
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    current = _make_receipt(
        receipt_id="rcpt-dead-write-fail",
        status="failed",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        failure_kind="adapter_permanent",
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(current)
    failing = _FailingTransitionStorage(temp_storage, "finalize_outbox_terminal")

    with pytest.raises(RuntimeError, match="dead-letter persistence failure"):
        await lifecycle.finalize_retry_attempt_error(
            failing,  # type: ignore[arg-type]
            item,
            RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
            error=ValueError("permanent failure"),
        )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "in_progress"

    repaired = await lifecycle.reconcile_retry_claim(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )
    assert repaired is not None
    assert repaired.outcome == "dead_lettered"
    assert repaired.receipt_id != current.receipt_id
    assert repaired.failure_kind == "adapter_permanent"
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.receipt_id == repaired.receipt_id


async def test_retry_claim_reconciliation_repairs_sent_transition_failure(
    temp_storage: StorageBackend,
) -> None:
    """Persisted acceptance prevents duplicate transport after write failure."""
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-sent-write-fail",
        event_id="evt-sent-write-fail",
        attempt_number=1,
        receipt_id="rcpt-parent",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    sent = _make_receipt(
        receipt_id="rcpt-sent-write-fail",
        status="sent",
        attempt_number=2,
        event_id=item.event_id,
        adapter=item.target_adapter,
        plan_id=item.delivery_plan_id,
        source="retry",
        outbox_id=item.outbox_id,
    )
    await temp_storage.append_receipt(sent)
    failing = _FailingTransitionStorage(temp_storage, "mark_outbox_sent")

    with pytest.raises(RuntimeError, match="sent persistence failure"):
        await lifecycle.finalize_retry_success(
            failing,  # type: ignore[arg-type]
            item,
            sent,
        )

    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "in_progress"

    repaired = await lifecycle.reconcile_retry_claim(
        temp_storage,
        item,
        RetryPolicy(max_attempts=4, backoff_base=1.0, jitter=False),
    )
    assert repaired is not None
    assert repaired.outcome == "accepted"
    assert repaired.receipt_id == sent.receipt_id
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "sent"
    assert updated.receipt_id == sent.receipt_id
    assert updated.attempt_number == sent.attempt_number


async def test_finalize_retry_attempt_error_dead_letters_unpersisted_permanent_error(
    temp_storage: StorageBackend,
) -> None:
    """Non-retryable exceptions terminate even without receipt evidence."""
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-no-receipt-permanent",
        event_id="evt-no-receipt-permanent",
        attempt_number=1,
    )
    await create_outbox_item_with_parent(temp_storage, item)

    result = await lifecycle.finalize_retry_attempt_error(
        temp_storage,
        item,
        RetryPolicy(max_attempts=5, backoff_base=1.0, jitter=False),
        error=ValueError("invalid target"),
    )

    assert result.outcome == "dead_lettered"
    assert result.failure_kind == "adapter_permanent"
    assert result.attempt_number == 2
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "dead_lettered"
    assert updated.failure_kind == "adapter_permanent"
    assert updated.attempt_number == 2


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


async def test_finalize_retry_success_suppressed_result_is_non_success(
    temp_storage: StorageBackend,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(
        outbox_id="obox-suppressed",
        event_id="evt-suppressed",
        attempt_number=1,
        active_attempt=2,
    )
    await create_outbox_item_with_parent(temp_storage, item)
    receipt = _make_receipt(
        receipt_id="rcpt-suppressed",
        status="suppressed",
        attempt_number=2,
        event_id=item.event_id,
        plan_id=item.delivery_plan_id,
        error="capability_suppressed",
    )

    succeeded = await lifecycle.finalize_retry_success(temp_storage, item, receipt)

    assert succeeded is False
    updated = await temp_storage.get_outbox_item(item.outbox_id)
    assert updated is not None
    assert updated.status == "abandoned"
    assert updated.attempt_number == 2
    assert updated.active_attempt is None
    assert updated.error_summary == "capability_suppressed"


@pytest.mark.parametrize("status", ["failed", "dead_lettered"])
async def test_finalize_retry_success_rejects_failure_receipts(
    temp_storage: StorageBackend,
    status: str,
) -> None:
    lifecycle = _make_lifecycle()
    item = _retry_item(outbox_id=f"obox-{status}", event_id=f"evt-{status}")
    await create_outbox_item_with_parent(temp_storage, item)
    receipt = _make_receipt(
        receipt_id=f"rcpt-{status}",
        status=status,
        attempt_number=1,
        event_id=item.event_id,
        plan_id=item.delivery_plan_id,
    )

    with pytest.raises(ValueError, match="Retry success finalization"):
        await lifecycle.finalize_retry_success(temp_storage, item, receipt)
