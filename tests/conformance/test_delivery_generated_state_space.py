"""Generated delivery lifecycle state-space and terminalization conformance.

The fixed adversarial scenarios in ``test_delivery_state_machine_model`` remain
useful named regressions.  This module complements them by generating every
operation edge reachable from the core in-progress execution state and by
exercising the complete guarded terminal-finalization truth table against both
the in-memory conformance backend and SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Literal

import pytest

from medre.core.engine.pipeline.delivery_state import OUTBOX_STATUSES
from medre.core.events import DeliveryReceipt, NativeMessageRef
from medre.core.storage.backend import (
    DeferredHandoffFinalization,
    DeliveryOutboxItem,
    TerminalOutboxFinalization,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.conformance.test_delivery_lifecycle_conformance import _MemoryStorage
from tests.helpers.storage_outbox import admit_event

Operation = Literal[
    "reserve",
    "queue",
    "retry",
    "send",
    "send_finalized",
    "dead_letter",
    "cancel",
    "abandon",
    "cancel_finalized",
    "wrong_owner_send",
]

_OPERATIONS: tuple[Operation, ...] = (
    "reserve",
    "queue",
    "retry",
    "send",
    "send_finalized",
    "dead_letter",
    "cancel",
    "abandon",
    "cancel_finalized",
    "wrong_owner_send",
)
_TERMINAL_STATUSES = ("dead_lettered", "cancelled", "abandoned")


@dataclass(frozen=True)
class _ModelState:
    status: str = "in_progress"
    attempt_number: int = 1
    active_attempt: int | None = None
    receipt_id: str | None = None
    worker_id: str | None = "worker-a"
    receipt_ids: tuple[str, ...] = ()

    @property
    def effective_attempt(self) -> int:
        return self.active_attempt or self.attempt_number


@dataclass(frozen=True)
class _Transition:
    state: _ModelState
    result: bool | int | None


def _transition(
    state: _ModelState,
    operation: Operation,
    *,
    receipt_id: str,
) -> _Transition:
    if operation == "reserve":
        if (
            state.status == "in_progress"
            and state.worker_id == "worker-a"
            and state.active_attempt is None
        ):
            reserved = state.attempt_number + 1
            return _Transition(replace(state, active_attempt=reserved), reserved)
        return _Transition(state, None)

    candidate = state.effective_attempt
    expected_worker = state.worker_id
    allowed: tuple[str, ...]
    new_status: str
    terminal = False

    if operation == "queue":
        allowed = ("in_progress",)
        new_status = "queued"
    elif operation == "retry":
        allowed = ("in_progress",)
        new_status = "retry_wait"
    elif operation == "send":
        allowed = ("in_progress", "queued")
        new_status = "sent"
    elif operation == "send_finalized":
        allowed = ("in_progress", "queued")
        new_status = "sent"
        candidate = state.attempt_number
    elif operation in {"dead_letter", "cancel", "abandon", "cancel_finalized"}:
        allowed = ("in_progress", "queued")
        terminal = True
        new_status = {
            "dead_letter": "dead_lettered",
            "cancel": "cancelled",
            "abandon": "abandoned",
            "cancel_finalized": "cancelled",
        }[operation]
        if operation == "cancel_finalized":
            candidate = state.attempt_number
    elif operation == "wrong_owner_send":
        allowed = ("in_progress", "queued")
        new_status = "sent"
        expected_worker = "worker-b"
    else:  # pragma: no cover - Literal exhaustiveness guard
        raise AssertionError(operation)

    owner_matches = expected_worker is None or state.worker_id == expected_worker
    attempt_matches = (
        state.active_attempt == candidate
        if state.active_attempt is not None
        else state.attempt_number <= candidate
    )
    if terminal:
        attempt_matches = state.effective_attempt == candidate
    committed = (
        state.status in allowed
        and owner_matches
        and attempt_matches
        and state.status not in {"sent", "dead_lettered", "cancelled", "abandoned"}
    )
    if not committed:
        return _Transition(state, False)

    next_state = replace(
        state,
        status=new_status,
        attempt_number=candidate,
        active_attempt=None,
        receipt_id=receipt_id,
        worker_id=None,
        receipt_ids=(
            state.receipt_ids + (receipt_id,) if terminal else state.receipt_ids
        ),
    )
    return _Transition(next_state, True)


def _generated_edges() -> tuple[tuple[tuple[Operation, ...], Operation], ...]:
    """Generate every operation edge from every reachable abstract state."""
    representative: dict[_ModelState, tuple[Operation, ...]] = {_ModelState(): ()}
    frontier = [_ModelState()]
    while frontier:
        state = frontier.pop(0)
        path = representative[state]
        for operation in _OPERATIONS:
            transition = _transition(
                state,
                operation,
                receipt_id=f"model-{len(path)}-{operation}",
            )
            next_state = transition.state
            # Receipt IDs are observational evidence, not control state.  Strip
            # them while discovering the finite lifecycle control graph.
            control_state = replace(next_state, receipt_id=None, receipt_ids=())
            if control_state not in representative:
                representative[control_state] = path + (operation,)
                frontier.append(control_state)
    return tuple(
        (path, operation)
        for _, path in sorted(
            representative.items(),
            key=lambda item: (len(item[1]), item[1]),
        )
        for operation in _OPERATIONS
    )


_GENERATED_EDGES = _generated_edges()


@pytest.fixture(params=("memory", "sqlite"))
async def generated_storage(
    request: pytest.FixtureRequest,
    temp_storage: SQLiteStorage,
):
    if request.param == "memory":
        return _MemoryStorage()
    return temp_storage


def _new_item(
    *,
    outbox_id: str,
    event_id: str,
    status: str = "in_progress",
    worker_id: str | None = "worker-a",
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-generated",
        delivery_plan_id="plan-generated",
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=1,
        status=status,
        worker_id=worker_id,
    )


async def _admit(storage: object, item: DeliveryOutboxItem) -> None:
    if isinstance(storage, SQLiteStorage):
        await admit_event(storage, item.event_id)
    await storage.create_outbox_item(item)  # type: ignore[attr-defined]


async def _storage_state(storage: object, outbox_id: str) -> _ModelState:
    item = await storage.get_outbox_item(outbox_id)  # type: ignore[attr-defined]
    assert item is not None
    receipts = await storage.list_receipts_for_event(  # type: ignore[attr-defined]
        item.event_id
    )
    return _ModelState(
        status=item.status,
        attempt_number=item.attempt_number,
        active_attempt=item.active_attempt,
        receipt_id=item.receipt_id,
        worker_id=item.worker_id,
        receipt_ids=tuple(receipt.receipt_id for receipt in receipts),
    )


async def _apply_operation(
    storage: object,
    operation: Operation,
    *,
    item: DeliveryOutboxItem,
    model: _ModelState,
    receipt_id: str,
) -> bool | int | None:
    candidate = model.effective_attempt
    if operation == "reserve":
        return await storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
            item.outbox_id,
            "worker-a",
            model.attempt_number,
        )
    if operation == "queue":
        return await storage.mark_outbox_queued(  # type: ignore[attr-defined]
            item.outbox_id,
            receipt_id=receipt_id,
            attempt_number=candidate,
            expected_worker_id=model.worker_id,
        )
    if operation == "retry":
        return await storage.mark_outbox_retry_wait(  # type: ignore[attr-defined]
            item.outbox_id,
            "2099-01-01T00:00:00+00:00",
            receipt_id=receipt_id,
            failure_kind="adapter_transient",
            attempt_number=candidate,
            expected_worker_id=model.worker_id,
        )
    if operation in {"send", "send_finalized", "wrong_owner_send"}:
        if operation == "send_finalized":
            candidate = model.attempt_number
        expected_worker = (
            "worker-b" if operation == "wrong_owner_send" else model.worker_id
        )
        return await storage.mark_outbox_sent(  # type: ignore[attr-defined]
            item.outbox_id,
            receipt_id=receipt_id,
            attempt_number=candidate,
            expected_worker_id=expected_worker,
        )

    terminal_status = {
        "dead_letter": "dead_lettered",
        "cancel": "cancelled",
        "abandon": "abandoned",
        "cancel_finalized": "cancelled",
    }[operation]
    if operation == "cancel_finalized":
        candidate = model.attempt_number
    receipt = DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status=terminal_status,  # type: ignore[arg-type]
        receipt_kind="lifecycle",
        outbox_id=item.outbox_id,
        attempt_number=candidate,
    )
    return await storage.finalize_outbox_terminal(  # type: ignore[attr-defined]
        TerminalOutboxFinalization(
            lifecycle_receipt=receipt,
            expected_worker_id=model.worker_id,
        )
    )


async def test_generated_reachable_state_space_matches_model(
    generated_storage: object,
) -> None:
    assert len(_GENERATED_EDGES) >= 50

    for case_index, (path, edge) in enumerate(_GENERATED_EDGES):
        case_id = f"generated-{case_index}"
        item = _new_item(
            outbox_id=f"obox-{case_id}",
            event_id=f"evt-{case_id}",
        )
        await _admit(generated_storage, item)
        model = _ModelState()

        for step, operation in enumerate((*path, edge), start=1):
            receipt_id = f"rcpt-{case_id}-{step}-{operation}"
            expected = _transition(model, operation, receipt_id=receipt_id)
            actual_result = await _apply_operation(
                generated_storage,
                operation,
                item=item,
                model=model,
                receipt_id=receipt_id,
            )
            assert actual_result == expected.result, (path, edge, step, operation)
            model = expected.state
            assert await _storage_state(generated_storage, item.outbox_id) == model, (
                path,
                edge,
                step,
                operation,
            )


async def _seed_source_status(
    storage: object,
    *,
    case_id: str,
    source_status: str,
) -> DeliveryOutboxItem:
    initial_status = "pending" if source_status == "pending" else "in_progress"
    initial_worker = None if initial_status == "pending" else "worker-a"
    item = _new_item(
        outbox_id=f"obox-truth-{case_id}",
        event_id=f"evt-truth-{case_id}",
        status=initial_status,
        worker_id=initial_worker,
    )
    await _admit(storage, item)
    if source_status == "pending" or source_status == "in_progress":
        return item
    if source_status == "in_progress_reserved":
        assert (
            await storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
                item.outbox_id, "worker-a", 1
            )
            == 2
        )
        return item

    transition = {
        "queued": lambda: storage.mark_outbox_queued(  # type: ignore[attr-defined]
            item.outbox_id, attempt_number=1, expected_worker_id="worker-a"
        ),
        "sent": lambda: storage.mark_outbox_sent(  # type: ignore[attr-defined]
            item.outbox_id, attempt_number=1, expected_worker_id="worker-a"
        ),
        "retry_wait": lambda: storage.mark_outbox_retry_wait(  # type: ignore[attr-defined]
            item.outbox_id,
            "2099-01-01T00:00:00+00:00",
            attempt_number=1,
            expected_worker_id="worker-a",
        ),
        "dead_lettered": lambda: storage.mark_outbox_dead_lettered(  # type: ignore[attr-defined]
            item.outbox_id, attempt_number=1, expected_worker_id="worker-a"
        ),
        "cancelled": lambda: storage.mark_outbox_cancelled(  # type: ignore[attr-defined]
            item.outbox_id, attempt_number=1, expected_worker_id="worker-a"
        ),
        "abandoned": lambda: storage.mark_outbox_abandoned(  # type: ignore[attr-defined]
            item.outbox_id, attempt_number=1, expected_worker_id="worker-a"
        ),
    }[source_status]
    assert await transition()
    return item


async def test_terminalization_guard_truth_table_is_exhaustive(
    generated_storage: object,
) -> None:
    source_statuses = tuple(sorted(OUTBOX_STATUSES)) + ("in_progress_reserved",)
    worker_fences: tuple[str | None, ...] = (None, "worker-a", "worker-b")
    attempt_modes = ("effective", "finalized", "future")
    cases = tuple(
        product(source_statuses, _TERMINAL_STATUSES, attempt_modes, worker_fences)
    )
    assert len(cases) == len(source_statuses) * 3 * 3 * 3

    for case_index, (source, terminal_status, attempt_mode, worker_fence) in enumerate(
        cases
    ):
        case_id = f"{case_index}-{source}-{terminal_status}-{attempt_mode}"
        item = await _seed_source_status(
            generated_storage,
            case_id=case_id,
            source_status=source,
        )
        current = await generated_storage.get_outbox_item(  # type: ignore[attr-defined]
            item.outbox_id
        )
        assert current is not None
        current = replace(current)
        effective_attempt = current.active_attempt or current.attempt_number
        attempt_number = {
            "effective": effective_attempt,
            "finalized": current.attempt_number,
            "future": effective_attempt + 1,
        }[attempt_mode]
        receipt_id = f"rcpt-truth-{case_index}"
        stored_before = await generated_storage.list_receipts_for_event(  # type: ignore[attr-defined]
            item.event_id
        )
        before_receipts = tuple(r.receipt_id for r in stored_before)
        receipt = DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
            route_id=item.route_id,
            status=terminal_status,  # type: ignore[arg-type]
            receipt_kind="lifecycle",
            outbox_id=item.outbox_id,
            attempt_number=attempt_number,
        )

        expected = (
            current.status in {"in_progress", "queued"}
            and effective_attempt == attempt_number
            and (worker_fence is None or current.worker_id == worker_fence)
        )
        committed = await generated_storage.finalize_outbox_terminal(  # type: ignore[attr-defined]
            TerminalOutboxFinalization(
                lifecycle_receipt=receipt,
                expected_worker_id=worker_fence,
            )
        )
        assert committed is expected, (
            source,
            terminal_status,
            attempt_mode,
            worker_fence,
        )

        after = await generated_storage.get_outbox_item(  # type: ignore[attr-defined]
            item.outbox_id
        )
        assert after is not None
        stored_after = await generated_storage.list_receipts_for_event(  # type: ignore[attr-defined]
            item.event_id
        )
        after_receipts = tuple(r.receipt_id for r in stored_after)
        if expected:
            assert after.status == terminal_status
            assert after.attempt_number == attempt_number
            assert after.active_attempt is None
            assert after.receipt_id == receipt_id
            assert after_receipts == before_receipts + (receipt_id,)
        else:
            assert after == current
            assert after_receipts == before_receipts


@pytest.mark.parametrize(
    "mismatch",
    ("outbox", "event", "plan", "adapter", "channel", "attempt"),
)
async def test_queued_sent_finalization_fences_full_delivery_identity(
    generated_storage: object,
    mismatch: str,
) -> None:
    """Both backends reject coherent sent evidence for a sibling identity."""
    item = _new_item(
        outbox_id=f"obox-queued-fence-{mismatch}",
        event_id=f"evt-queued-fence-{mismatch}",
    )
    await _admit(generated_storage, item)
    assert await generated_storage.mark_outbox_queued(  # type: ignore[attr-defined]
        item.outbox_id,
        attempt_number=1,
        expected_worker_id="worker-a",
    )

    event_id = item.event_id
    plan_id = item.delivery_plan_id
    adapter = item.target_adapter
    channel = item.target_channel
    outbox_id = item.outbox_id
    attempt_number = 1
    if mismatch == "outbox":
        outbox_id = f"{item.outbox_id}-other"
    elif mismatch == "event":
        event_id = f"{item.event_id}-other"
    elif mismatch == "plan":
        plan_id = f"{item.delivery_plan_id}-other"
    elif mismatch == "adapter":
        adapter = f"{item.target_adapter}-other"
    elif mismatch == "channel":
        channel = f"{item.target_channel}-other"
    elif mismatch == "attempt":
        attempt_number = 2

    native_message_id = f"native-queued-fence-{mismatch}"
    native_ref = NativeMessageRef(
        id=f"nref-queued-fence-{mismatch}",
        event_id=event_id,
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=native_message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
    )
    receipt = DeliveryReceipt(
        receipt_id=f"rcpt-queued-fence-{mismatch}",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=item.route_id,
        status="sent",
        receipt_kind="attempt",
        adapter_message_id=native_message_id,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
    )
    before = await generated_storage.get_outbox_item(  # type: ignore[attr-defined]
        item.outbox_id
    )
    assert before is not None
    before = replace(before)
    list_receipts = generated_storage.list_receipts_for_event  # type: ignore[attr-defined]
    receipts_before = await list_receipts(item.event_id)

    finalize_queued = generated_storage.finalize_deferred_handoff  # type: ignore[attr-defined]
    committed = await finalize_queued(
        DeferredHandoffFinalization(native_ref=native_ref, receipt=receipt)
    )

    assert committed is False
    after = await generated_storage.get_outbox_item(  # type: ignore[attr-defined]
        item.outbox_id
    )
    assert after == before
    receipts_after = await list_receipts(item.event_id)
    assert receipts_after == receipts_before
