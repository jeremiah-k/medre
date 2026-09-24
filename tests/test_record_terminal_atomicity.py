"""Atomicity regression tests for OutboxManager.record_terminal.

Pins the all-or-nothing contract between the terminal receipt and the
outbox transition at the real SQLite + OutboxManager seam:

* a valid terminal callback commits newly proven attempt evidence (when the
  queue reports a send failure), lifecycle-terminal evidence, and the outbox
  transition together; cancelled/abandoned callbacks remain lifecycle-only;
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
import sqlite3
from dataclasses import replace

import pytest
from msgspec.structs import force_setattr

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events import NativeMessageRef
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    QueuedDeliveryFinalization,
    StorageError,
    TerminalOutboxFinalization,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_event,
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
    """Exhaustion records failed-attempt then dead-letter lifecycle evidence."""
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
    assert receipt.parent_receipt_id == "rcpt-queued-ex"
    terminal = [r for r in receipts if r.status == "dead_lettered"]
    assert len(terminal) == 1
    assert terminal[0].receipt_kind == "lifecycle"
    assert terminal[0].attempt_number == receipt.attempt_number
    assert terminal[0].parent_receipt_id == receipt.receipt_id

    outbox = await temp_storage.get_outbox_item("obox-q-ex")
    assert outbox is not None
    assert outbox.status == "dead_lettered"
    assert outbox.receipt_id == terminal[0].receipt_id
    assert outbox.worker_id is None


@pytest.mark.asyncio
async def test_terminal_callback_rejects_lineage_read_failure(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A history-read error must not be mistaken for a missing queued receipt."""
    await _create_outbox(
        temp_storage,
        outbox_id="obox-lineage-read-failure",
        event_id="evt-lineage-read-failure",
        delivery_plan_id="plan-lineage-read-failure",
    )

    async def _raise_lineage_read(*_args, **_kwargs):
        raise StorageError("lineage read failed")

    monkeypatch.setattr(
        temp_storage,
        "list_receipts_for_delivery",
        _raise_lineage_read,
    )

    manager = _make_manager(temp_storage)
    await manager.record_terminal(
        _terminal_record(
            outbox_id="obox-lineage-read-failure",
            event_id="evt-lineage-read-failure",
            delivery_plan_id="plan-lineage-read-failure",
        )
    )

    row = await temp_storage.get_outbox_item("obox-lineage-read-failure")
    assert row is not None
    assert row.status == "queued"
    assert await temp_storage.list_receipts_for_event("evt-lineage-read-failure") == []


@pytest.mark.asyncio
async def test_cancelled_from_queued_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    """Cancellation is lifecycle-only and does not invent a failed attempt."""
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
    assert [r for r in receipts if r.status == "failed"] == []
    terminal = [r for r in receipts if r.status == "cancelled"]
    assert len(terminal) == 1
    assert terminal[0].receipt_kind == "lifecycle"

    outbox = await temp_storage.get_outbox_item("obox-q-cancel")
    assert outbox is not None
    assert outbox.status == "cancelled"
    assert outbox.receipt_id == terminal[0].receipt_id


@pytest.mark.asyncio
async def test_abandoned_from_queued_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    """Abandonment is lifecycle-only and does not invent a failed attempt."""
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
    assert [r for r in receipts if r.status == "failed"] == []
    terminal = [r for r in receipts if r.status == "abandoned"]
    assert len(terminal) == 1

    outbox = await temp_storage.get_outbox_item("obox-q-abandon")
    assert outbox is not None
    assert outbox.status == "abandoned"
    assert outbox.receipt_id == terminal[0].receipt_id


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
        status="dead_lettered",
        receipt_kind="lifecycle",
        failure_kind="adapter_transient",
        outbox_id="obox-late",
        attempt_number=1,
    )
    committed = await temp_storage.finalize_outbox_terminal(
        TerminalOutboxFinalization(lifecycle_receipt=stale_receipt)
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
        status="dead_lettered",
        receipt_kind="lifecycle",
        failure_kind="adapter_transient",
        outbox_id="obox-rb",
        attempt_number=1,
    )
    with pytest.raises(StorageError):
        await temp_storage.finalize_outbox_terminal(
            TerminalOutboxFinalization(lifecycle_receipt=colliding_receipt)
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
    terminal = [r for r in receipts if r.status == "dead_lettered"]
    assert len(failed) == 1
    assert len(terminal) == 1

    outbox = await temp_storage.get_outbox_item("obox-dup")
    assert outbox is not None
    assert outbox.status == "dead_lettered"
    assert outbox.receipt_id == terminal[0].receipt_id

    assert "already terminal" in caplog.text


# ===================================================================
# 5. Storage contract validation is explicit at the transaction boundary
# ===================================================================


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("receipt_kind", "lifecycle evidence"),
        ("nonterminal_status", "error-terminal lifecycle status"),
        ("missing_outbox", "requires outbox_id"),
        ("missing_receipt_id", "requires receipt_id"),
        ("incomplete_identity", "complete delivery identity"),
        ("nonpositive_attempt", "attempt_number must be >= 1"),
    ],
)
def test_terminal_finalization_command_validates_evidence_shape(
    case: str,
    message: str,
) -> None:
    """The command rejects malformed evidence before any storage call exists."""
    receipt = build_delivery_receipt(
        receipt_id="rcpt-contract-terminal",
        event_id="evt-contract-terminal",
        delivery_plan_id="plan-contract-terminal",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="dead_lettered",
        receipt_kind="lifecycle",
        outbox_id="obox-contract-terminal",
        attempt_number=1,
    )
    if case == "receipt_kind":
        receipt = build_delivery_receipt(
            receipt_id="rcpt-contract-attempt",
            event_id="evt-contract-terminal",
            delivery_plan_id="plan-contract-terminal",
            target_adapter="mesh-1",
            target_channel="0",
            route_id="route-1",
            status="failed",
            outbox_id="obox-contract-terminal",
            attempt_number=1,
        )
    elif case == "nonterminal_status":
        force_setattr(receipt, "status", "sent")
    elif case == "missing_outbox":
        force_setattr(receipt, "outbox_id", None)
    elif case == "missing_receipt_id":
        force_setattr(receipt, "receipt_id", "")
    elif case == "incomplete_identity":
        force_setattr(receipt, "target_adapter", "")
    elif case == "nonpositive_attempt":
        force_setattr(receipt, "attempt_number", 0)
    else:  # pragma: no cover - parametrization exhaustiveness guard
        raise AssertionError(case)

    with pytest.raises(ValueError, match=message):
        TerminalOutboxFinalization(lifecycle_receipt=receipt)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("kind", "attempt evidence"),
        ("missing_receipt_id", "attempt_receipt requires receipt_id"),
        ("status", "status='failed'"),
        ("lineage", "same delivery attempt as attempt_receipt"),
    ],
)
async def test_terminal_finalization_validates_attempt_receipt_linkage(
    temp_storage: SQLiteStorage,
    case: str,
    message: str,
) -> None:
    """Optional failed-attempt evidence must be the lifecycle receipt's parent."""
    attempt = build_delivery_receipt(
        receipt_id="rcpt-contract-parent",
        event_id="evt-contract-link",
        delivery_plan_id="plan-contract-link",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="failed",
        outbox_id="obox-contract-link",
        attempt_number=1,
    )
    lifecycle = build_delivery_receipt(
        receipt_id="rcpt-contract-child",
        event_id="evt-contract-link",
        delivery_plan_id="plan-contract-link",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="dead_lettered",
        receipt_kind="lifecycle",
        parent_receipt_id=attempt.receipt_id,
        outbox_id="obox-contract-link",
        attempt_number=1,
    )
    if case == "kind":
        attempt = build_delivery_receipt(
            receipt_id="rcpt-not-attempt",
            event_id=lifecycle.event_id,
            delivery_plan_id=lifecycle.delivery_plan_id,
            target_adapter=lifecycle.target_adapter,
            target_channel=lifecycle.target_channel,
            route_id="route-1",
            status="cancelled",
            receipt_kind="lifecycle",
            outbox_id=lifecycle.outbox_id,
            attempt_number=1,
        )
    elif case == "missing_receipt_id":
        force_setattr(attempt, "receipt_id", "")
        force_setattr(lifecycle, "parent_receipt_id", "")
    elif case == "status":
        attempt = build_delivery_receipt(
            receipt_id=lifecycle.parent_receipt_id or "rcpt-contract-parent",
            event_id=lifecycle.event_id,
            delivery_plan_id=lifecycle.delivery_plan_id,
            target_adapter=lifecycle.target_adapter,
            target_channel=lifecycle.target_channel,
            route_id="route-1",
            status="sent",
            outbox_id=lifecycle.outbox_id,
            attempt_number=1,
        )
    elif case == "lineage":
        attempt = build_delivery_receipt(
            receipt_id="rcpt-wrong-parent",
            event_id=lifecycle.event_id,
            delivery_plan_id=lifecycle.delivery_plan_id,
            target_adapter=lifecycle.target_adapter,
            target_channel=lifecycle.target_channel,
            route_id="route-1",
            status="failed",
            outbox_id=lifecycle.outbox_id,
            attempt_number=1,
        )

    with pytest.raises(ValueError, match=message):
        TerminalOutboxFinalization(
            lifecycle_receipt=lifecycle,
            attempt_receipt=attempt,
        )


async def test_terminal_finalization_wraps_raw_sqlite_error(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage seam translates raw sqlite failures without partial authority."""
    import medre.core.storage.sqlite._delivery_finalize as delivery_finalize

    receipt = build_delivery_receipt(
        receipt_id="rcpt-terminal-sqlite-error",
        event_id="evt-terminal-sqlite-error",
        delivery_plan_id="plan-terminal-sqlite-error",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="dead_lettered",
        receipt_kind="lifecycle",
        outbox_id="obox-terminal-sqlite-error",
        attempt_number=1,
    )

    def _raise_sqlite_error(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected terminal transaction failure")

    monkeypatch.setattr(
        delivery_finalize,
        "sync_finalize_outbox_terminal",
        _raise_sqlite_error,
    )

    with pytest.raises(StorageError, match="Terminal outbox finalization failed"):
        await temp_storage.finalize_outbox_terminal(
            TerminalOutboxFinalization(lifecycle_receipt=receipt)
        )


async def test_terminal_finalization_preserves_storage_error(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed storage failure crosses the SQLite adapter unchanged."""
    import medre.core.storage.sqlite._delivery_finalize as delivery_finalize

    receipt = build_delivery_receipt(
        receipt_id="rcpt-terminal-storage-error",
        event_id="evt-terminal-storage-error",
        delivery_plan_id="plan-terminal-storage-error",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="dead_lettered",
        receipt_kind="lifecycle",
        outbox_id="obox-terminal-storage-error",
        attempt_number=1,
    )

    def _raise_storage_error(*_args, **_kwargs):
        raise StorageError("injected typed terminal failure")

    monkeypatch.setattr(
        delivery_finalize,
        "sync_finalize_outbox_terminal",
        _raise_storage_error,
    )

    with pytest.raises(StorageError, match="injected typed terminal failure"):
        await temp_storage.finalize_outbox_terminal(
            TerminalOutboxFinalization(lifecycle_receipt=receipt)
        )


def test_terminal_finalization_derives_error_summary_from_lifecycle_receipt() -> None:
    """Mutable outbox error text cannot diverge from immutable evidence."""
    error = "x" * 600
    receipt = build_delivery_receipt(
        receipt_id="rcpt-terminal-summary",
        event_id="evt-terminal-summary",
        delivery_plan_id="plan-terminal-summary",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="dead_lettered",
        receipt_kind="lifecycle",
        error=error,
        failure_kind="adapter_transient",
        outbox_id="obox-terminal-summary",
        attempt_number=1,
    )

    command = TerminalOutboxFinalization(lifecycle_receipt=receipt)

    assert command.error_summary == error[:512]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("direction", "outbound native ref"),
        ("status", "sent attempt evidence"),
        ("event", "native_ref.event_id must match"),
        ("adapter", "native_ref.adapter must match"),
        ("message_id", "native_ref.native_message_id must match"),
        ("outbox", "requires outbox_id"),
        ("nonpositive_attempt", "attempt_number must be >= 1"),
    ],
)
async def test_queued_finalization_validates_native_and_attempt_identity(
    temp_storage: SQLiteStorage,
    case: str,
    message: str,
) -> None:
    """Queued completion validates every cross-table identity before writing."""
    ref_kwargs = {
        "id": "nref-contract",
        "event_id": "evt-queued-contract",
        "adapter": "mesh-1",
        "native_channel_id": "0",
        "native_message_id": "native-1",
        "native_thread_id": None,
        "native_relation_id": None,
        "direction": "outbound",
    }
    receipt = build_delivery_receipt(
        receipt_id="rcpt-queued-contract",
        event_id="evt-queued-contract",
        delivery_plan_id="plan-queued-contract",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="sent",
        adapter_message_id="native-1",
        outbox_id="obox-queued-contract",
        attempt_number=1,
    )
    if case == "direction":
        ref_kwargs["direction"] = "inbound"
    elif case == "status":
        receipt = build_delivery_receipt(
            receipt_id="rcpt-queued-not-sent",
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter=receipt.target_adapter,
            target_channel=receipt.target_channel,
            route_id="route-1",
            status="queued",
            adapter_message_id="native-1",
            outbox_id=receipt.outbox_id,
            attempt_number=1,
        )
    elif case == "event":
        ref_kwargs["event_id"] = "evt-other"
    elif case == "adapter":
        ref_kwargs["adapter"] = "mesh-other"
    elif case == "message_id":
        ref_kwargs["native_message_id"] = "native-other"
    elif case == "outbox":
        force_setattr(receipt, "outbox_id", None)
    elif case == "nonpositive_attempt":
        force_setattr(receipt, "attempt_number", 0)

    native_ref = NativeMessageRef(**ref_kwargs)
    with pytest.raises(ValueError, match=message):
        await temp_storage.finalize_queued_delivery(
            QueuedDeliveryFinalization(native_ref=native_ref, receipt=receipt)
        )


async def test_queued_finalization_rejects_conflicting_native_identity_atomically(
    temp_storage: SQLiteStorage,
) -> None:
    """A native identity owned by another event aborts the whole transaction."""
    await admit_event(temp_storage, "evt-native-owner")
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="nref-native-owner",
            event_id="evt-native-owner",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="native-conflict",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
    )

    item = DeliveryOutboxItem(
        outbox_id="obox-native-conflict",
        event_id="evt-native-candidate",
        route_id="route-1",
        delivery_plan_id="plan-native-candidate",
        target_adapter="mesh-1",
        target_channel="0",
        attempt_number=1,
        status="in_progress",
    )
    await create_outbox_item_with_parent(temp_storage, item)
    receipt = build_delivery_receipt(
        receipt_id="rcpt-native-conflict",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status="sent",
        adapter_message_id="native-conflict",
        outbox_id=item.outbox_id,
        attempt_number=1,
    )
    candidate_ref = NativeMessageRef(
        id="nref-native-candidate",
        event_id=item.event_id,
        adapter=item.target_adapter,
        native_channel_id=item.target_channel,
        native_message_id="native-conflict",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
    )

    with pytest.raises(
        StorageError,
        match="Native identity already maps to a different canonical event",
    ):
        await temp_storage.finalize_queued_delivery(
            QueuedDeliveryFinalization(native_ref=candidate_ref, receipt=receipt)
        )

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "in_progress"
    receipts = await temp_storage.list_receipts_for_event(item.event_id)
    assert all(candidate.receipt_id != receipt.receipt_id for candidate in receipts)


async def test_queued_finalization_wraps_raw_sqlite_error(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw SQLite failures are translated at the storage boundary."""
    import medre.core.storage.sqlite._delivery_finalize as delivery_finalize

    await admit_event(temp_storage, "evt-queued-sqlite-error")
    receipt = build_delivery_receipt(
        receipt_id="rcpt-queued-sqlite-error",
        event_id="evt-queued-sqlite-error",
        delivery_plan_id="plan-queued-sqlite-error",
        target_adapter="mesh-1",
        target_channel="0",
        route_id="route-1",
        status="sent",
        adapter_message_id="native-sqlite-error",
        outbox_id="obox-queued-sqlite-error",
        attempt_number=1,
    )
    native_ref = NativeMessageRef(
        id="nref-queued-sqlite-error",
        event_id=receipt.event_id,
        adapter=receipt.target_adapter,
        native_channel_id=receipt.target_channel,
        native_message_id=receipt.adapter_message_id or "",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
    )

    def _raise_sqlite_error(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected queued transaction failure")

    monkeypatch.setattr(
        delivery_finalize,
        "sync_finalize_queued_delivery",
        _raise_sqlite_error,
    )

    with pytest.raises(StorageError, match="Queued delivery finalization failed"):
        await temp_storage.finalize_queued_delivery(
            QueuedDeliveryFinalization(native_ref=native_ref, receipt=receipt)
        )
