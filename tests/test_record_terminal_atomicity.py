"""Atomicity regression tests for OutboxManager.record_terminal.

Pins the all-or-nothing contract between the terminal receipt and the
outbox transition at the real SQLite + OutboxManager seam:

* a valid terminal callback commits the failed receipt and the outbox
  terminal transition together — including from the production ``queued``
  hand-off state, with the outbox row linked back to its receipt and
  queued-receipt lineage preserved;
* a stale callback that loses to a competing attempt/state change
  commits neither a receipt nor an outbox mutation;
* a mid-transaction write failure rolls the guarded transition back —
  no receipt, no terminal outbox;
* duplicate terminal notifications stay idempotent.

Run against the pre-fix baseline these tests establish red (committed
receipts without their outbox transition, duplicate receipts); against
the atomic finalization they establish green.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    append_receipt_with_parent,
    create_outbox_item_with_parent,
)

# -- Helpers --


def _make_manager(storage: SQLiteStorage) -> OutboxManager:
    return OutboxManager(
        storage=storage,
        lifecycle=DeliveryLifecycleService(),
    )


async def _create_outbox(
    storage: SQLiteStorage,
    *,
    outbox_id: str,
    event_id: str,
    delivery_plan_id: str,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Create an in_progress outbox row, then hand it to the adapter-local
    queue the way the production pipeline does (in_progress -> queued)."""
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter="mesh-1",
        target_channel="0",
        attempt_number=attempt_number,
        status="in_progress",
    )
    await create_outbox_item_with_parent(storage, item)
    await storage.mark_outbox_queued(outbox_id)
    return item


def _terminal_record(
    *,
    outbox_id: str,
    event_id: str,
    delivery_plan_id: str,
    outcome: str = "exhausted",
    attempt_number: int = 1,
) -> QueueTerminalRecord:
    return QueueTerminalRecord(
        event_id=event_id,
        adapter="mesh-1",
        outbox_id=outbox_id,
        delivery_plan_id=delivery_plan_id,
        attempt_number=attempt_number,
        native_channel_id="0",
        outcome=outcome,
        error="budget exhausted",
    )


# ===================================================================
# 1. Valid terminal outcome commits receipt + transition atomically
# ===================================================================


@pytest.mark.asyncio
async def test_exhausted_from_queued_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    """exhausted on a queued outbox -> one failed receipt with inherited
    lineage, outbox dead_lettered and linked to that receipt."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-q-ex",
        event_id="evt-q-ex",
        delivery_plan_id="plan-q-ex",
    )
    # The hand-off produced a queued receipt carrying replay lineage.
    await append_receipt_with_parent(
        temp_storage,
        build_delivery_receipt(
            receipt_id="rcpt-queued-ex",
            event_id="evt-q-ex",
            delivery_plan_id="plan-q-ex",
            target_adapter="mesh-1",
            target_channel="0",
            route_id="route-1",
            status="queued",
            source="replay",
            replay_run_id="replay-42",
            parent_receipt_id="rcpt-original",
            outbox_id="obox-q-ex",
            attempt_number=1,
        ),
    )

    manager = _make_manager(temp_storage)
    await manager.record_terminal(
        _terminal_record(
            outbox_id="obox-q-ex",
            event_id="evt-q-ex",
            delivery_plan_id="plan-q-ex",
        )
    )

    receipts = await temp_storage.list_receipts_for_event("evt-q-ex")
    failed = [r for r in receipts if r.status == "failed"]
    assert len(failed) == 1, f"expected exactly one failed receipt, got {failed}"
    receipt = failed[0]
    assert receipt.failure_kind == "adapter_transient"
    assert receipt.outbox_id == "obox-q-ex"
    assert receipt.attempt_number == 1
    # Lineage inherited from the queued receipt of the same attempt.
    assert receipt.source == "replay"
    assert receipt.replay_run_id == "replay-42"
    assert receipt.parent_receipt_id == "rcpt-original"

    outbox = await temp_storage.get_outbox_item("obox-q-ex")
    assert outbox is not None
    assert outbox.status == "dead_lettered"
    assert outbox.receipt_id == receipt.receipt_id
    assert outbox.worker_id is None


@pytest.mark.asyncio
async def test_cancelled_from_queued_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    """cancelled on a queued outbox -> failed receipt committed together
    with the cancelled transition and linked via outbox.receipt_id."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-q-cancel",
        event_id="evt-q-cancel",
        delivery_plan_id="plan-q-cancel",
    )

    manager = _make_manager(temp_storage)
    await manager.record_terminal(
        _terminal_record(
            outbox_id="obox-q-cancel",
            event_id="evt-q-cancel",
            delivery_plan_id="plan-q-cancel",
            outcome="cancelled",
        )
    )

    receipts = await temp_storage.list_receipts_for_event("evt-q-cancel")
    failed = [r for r in receipts if r.status == "failed"]
    assert len(failed) == 1

    outbox = await temp_storage.get_outbox_item("obox-q-cancel")
    assert outbox is not None
    assert outbox.status == "cancelled"
    assert outbox.receipt_id == failed[0].receipt_id


@pytest.mark.asyncio
async def test_abandoned_from_queued_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    """abandoned on a queued outbox -> failed receipt committed together
    with the abandoned transition and linked via outbox.receipt_id."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-q-abandon",
        event_id="evt-q-abandon",
        delivery_plan_id="plan-q-abandon",
    )

    manager = _make_manager(temp_storage)
    await manager.record_terminal(
        _terminal_record(
            outbox_id="obox-q-abandon",
            event_id="evt-q-abandon",
            delivery_plan_id="plan-q-abandon",
            outcome="abandoned",
        )
    )

    receipts = await temp_storage.list_receipts_for_event("evt-q-abandon")
    failed = [r for r in receipts if r.status == "failed"]
    assert len(failed) == 1

    outbox = await temp_storage.get_outbox_item("obox-q-abandon")
    assert outbox is not None
    assert outbox.status == "abandoned"
    assert outbox.receipt_id == failed[0].receipt_id


# ===================================================================
# 2. Stale callback loses to a competing attempt/state change
# ===================================================================


@pytest.mark.asyncio
async def test_stale_callback_after_competing_send(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback validates against a pre-competition snapshot (the
    race window between validation and commit) while the row was
    already finalized as sent by the competing attempt."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-race",
        event_id="evt-race",
        delivery_plan_id="plan-race",
    )
    # Competing attempt wins: the queued item is delivered and marked
    # sent with its own receipt.
    await temp_storage.mark_outbox_sent("obox-race", receipt_id="rcpt-winner")

    # Freeze the manager's view at the pre-competition snapshot so the
    # stale callback passes validation; the transaction must still
    # refuse to commit against the already-final row.
    current_get = temp_storage.get_outbox_item

    async def _stale_snapshot(outbox_id: str) -> DeliveryOutboxItem | None:
        row = await current_get(outbox_id)
        if row is not None and outbox_id == "obox-race":
            return replace(row, status="queued")
        return row

    monkeypatch.setattr(temp_storage, "get_outbox_item", _stale_snapshot)

    manager = _make_manager(temp_storage)
    with caplog.at_level(logging.WARNING):
        await manager.record_terminal(
            _terminal_record(
                outbox_id="obox-race",
                event_id="evt-race",
                delivery_plan_id="plan-race",
            )
        )

    # No terminal receipt was committed for the stale callback.
    receipts = await temp_storage.list_receipts_for_event("evt-race")
    assert [r for r in receipts if r.status == "failed"] == []

    # The competing attempt's outcome is untouched.
    outbox = await current_get("obox-race")
    assert outbox is not None
    assert outbox.status == "sent"
    assert outbox.receipt_id == "rcpt-winner"

    assert "Terminal outcome rejected" in caplog.text


@pytest.mark.asyncio
async def test_storage_guard_rejects_stale_attempt_number(
    temp_storage: SQLiteStorage,
) -> None:
    """Direct storage seam: the guarded update only matches the exact
    attempt, so a callback for an earlier attempt commits nothing."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-late",
        event_id="evt-late",
        delivery_plan_id="plan-late",
        attempt_number=2,
    )

    stale_receipt = build_delivery_receipt(
        event_id="evt-late",
        delivery_plan_id="plan-late",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="failed",
        failure_kind="adapter_transient",
        outbox_id="obox-late",
        attempt_number=1,
    )
    committed = await temp_storage.finalize_outbox_terminal(
        stale_receipt,
        outbox_id="obox-late",
        attempt_number=1,
        terminal_status="dead_lettered",
        event_id="evt-late",
        target_adapter="mesh-1",
        failure_kind="adapter_transient",
        error_summary="stale attempt",
    )
    assert committed is False

    receipts = await temp_storage.list_receipts_for_event("evt-late")
    assert receipts == []
    outbox = await temp_storage.get_outbox_item("obox-late")
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.attempt_number == 2
    assert outbox.receipt_id is None


# ===================================================================
# 3. Mid-transaction failure rolls everything back
# ===================================================================


@pytest.mark.asyncio
async def test_duplicate_receipt_id_rolls_back_transition(
    temp_storage: SQLiteStorage,
) -> None:
    """The guarded transition succeeds, the receipt insert then fails on
    the UNIQUE receipt_id constraint, and the whole transaction rolls
    back."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-rb",
        event_id="evt-rb",
        delivery_plan_id="plan-rb",
    )
    # An unrelated receipt already owns the receipt_id our terminal
    # receipt will collide with.
    await append_receipt_with_parent(
        temp_storage,
        build_delivery_receipt(
            receipt_id="rcpt-collide",
            event_id="evt-other",
            delivery_plan_id="plan-other",
            target_adapter="mesh-1",
            target_channel="0",
            route_id="route-1",
            status="failed",
            failure_kind="adapter_transient",
            attempt_number=1,
        ),
    )

    colliding_receipt = build_delivery_receipt(
        receipt_id="rcpt-collide",
        event_id="evt-rb",
        delivery_plan_id="plan-rb",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="failed",
        failure_kind="adapter_transient",
        outbox_id="obox-rb",
        attempt_number=1,
    )
    with pytest.raises(StorageError):
        await temp_storage.finalize_outbox_terminal(
            colliding_receipt,
            outbox_id="obox-rb",
            attempt_number=1,
            terminal_status="dead_lettered",
            event_id="evt-rb",
            target_adapter="mesh-1",
            failure_kind="adapter_transient",
            error_summary="collide",
        )

    # Rollback proof: the transition did not survive the failed insert.
    outbox = await temp_storage.get_outbox_item("obox-rb")
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.receipt_id is None
    assert await temp_storage.list_receipts_for_event("evt-rb") == []


@pytest.mark.asyncio
async def test_manager_write_failure_leaves_no_partial_state(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt-insert failure at the SQL layer is logged by the
    manager and persists nothing — the transition rolls back with it."""
    import medre.core.storage.sqlite.statements as statements

    monkeypatch.setattr(
        statements, "_INSERT_RECEIPT", "INSERT INTO no_such_table VALUES (?)"
    )
    await _create_outbox(
        temp_storage,
        outbox_id="obox-wf",
        event_id="evt-wf",
        delivery_plan_id="plan-wf",
    )

    manager = _make_manager(temp_storage)
    with caplog.at_level(logging.ERROR):
        await manager.record_terminal(
            _terminal_record(
                outbox_id="obox-wf",
                event_id="evt-wf",
                delivery_plan_id="plan-wf",
            )
        )

    assert "Failed to record terminal queue outcome" in caplog.text
    outbox = await temp_storage.get_outbox_item("obox-wf")
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.receipt_id is None
    assert await temp_storage.list_receipts_for_event("evt-wf") == []


# ===================================================================
# 4. Duplicate terminal notifications stay idempotent
# ===================================================================


@pytest.mark.asyncio
async def test_duplicate_exhausted_notifications_commit_once(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _create_outbox(
        temp_storage,
        outbox_id="obox-dup",
        event_id="evt-dup",
        delivery_plan_id="plan-dup",
    )

    manager = _make_manager(temp_storage)
    record = _terminal_record(
        outbox_id="obox-dup",
        event_id="evt-dup",
        delivery_plan_id="plan-dup",
    )
    await manager.record_terminal(record)
    with caplog.at_level(logging.WARNING):
        await manager.record_terminal(record)

    receipts = await temp_storage.list_receipts_for_event("evt-dup")
    failed = [r for r in receipts if r.status == "failed"]
    assert len(failed) == 1

    outbox = await temp_storage.get_outbox_item("obox-dup")
    assert outbox is not None
    assert outbox.status == "dead_lettered"
    assert outbox.receipt_id == failed[0].receipt_id

    assert "already terminal" in caplog.text
