"""Executable delivery-state invariants shared by SQLite and the conformance fake.

These tests intentionally exercise operation *sequences*, not isolated methods.
Each scenario is run against both persistence implementations so a permissive
fake cannot silently diverge from SQLite's compare-and-set fences.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    StorageError,
    TerminalOutboxFinalization,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.conformance.test_delivery_lifecycle_conformance import _MemoryStorage
from tests.helpers.storage_outbox import admit_event


@dataclass(frozen=True)
class _Snapshot:
    status: str
    attempt_number: int
    active_attempt: int | None
    receipt_id: str | None
    worker_id: str | None


def _item(
    *,
    outbox_id: str,
    event_id: str,
    attempt: int = 1,
    worker_id: str | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-model",
        delivery_plan_id="plan-model",
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=attempt,
        status="in_progress",
        worker_id=worker_id,
    )


def _attempt_receipt(
    receipt_id: str,
    *,
    outbox_id: str,
    event_id: str,
    attempt: int,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-model",
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-model",
        status="failed",
        receipt_kind="attempt",
        outbox_id=outbox_id,
        attempt_number=attempt,
        error="transport failed",
        failure_kind="adapter_transient",
    )


def _lifecycle_receipt(
    receipt_id: str,
    *,
    outbox_id: str,
    event_id: str,
    attempt: int,
    status: str = "dead_lettered",
    parent_receipt_id: str | None = None,
    channel: str | None = "mesh",
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-model",
        target_adapter="radio",
        target_channel=channel,
        route_id="route-model",
        status=status,  # type: ignore[arg-type]
        receipt_kind="lifecycle",
        outbox_id=outbox_id,
        attempt_number=attempt,
        parent_receipt_id=parent_receipt_id,
        error="terminal lifecycle transition",
        failure_kind="adapter_permanent",
    )


async def _admit(storage: object, item: DeliveryOutboxItem) -> None:
    if isinstance(storage, SQLiteStorage):
        await admit_event(storage, item.event_id)
    await storage.create_outbox_item(item)  # type: ignore[attr-defined]


async def _snapshot(storage: object, outbox_id: str) -> _Snapshot:
    row = await storage.get_outbox_item(outbox_id)  # type: ignore[attr-defined]
    assert row is not None
    return _Snapshot(
        status=row.status,
        attempt_number=row.attempt_number,
        active_attempt=row.active_attempt,
        receipt_id=row.receipt_id,
        worker_id=row.worker_id,
    )


async def _receipt_ids(storage: object, event_id: str) -> list[str]:
    receipts = await storage.list_receipts_for_event(event_id)  # type: ignore[attr-defined]
    return [receipt.receipt_id for receipt in receipts]


@pytest.fixture(params=("memory", "sqlite"))
async def lifecycle_storage(
    request: pytest.FixtureRequest, temp_storage: SQLiteStorage
):
    if request.param == "memory":
        return _MemoryStorage()
    return temp_storage


async def test_reserved_attempt_and_owner_fences_never_regress_identity(
    lifecycle_storage: object,
) -> None:
    event_id = "evt-model-reservation"
    item = _item(
        outbox_id="obox-model-reservation",
        event_id=event_id,
        worker_id="worker-a",
    )
    await _admit(lifecycle_storage, item)

    reserved = await lifecycle_storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
        item.outbox_id,
        "worker-a",
        1,
    )
    assert reserved == 2
    assert await _snapshot(lifecycle_storage, item.outbox_id) == _Snapshot(
        "in_progress", 1, 2, None, "worker-a"
    )

    # Wrong owner cannot consume the live reservation.
    assert not await lifecycle_storage.mark_outbox_sent(  # type: ignore[attr-defined]
        item.outbox_id,
        receipt_id="wrong-owner",
        attempt_number=2,
        expected_worker_id="worker-b",
    )
    # Prior attempt cannot consume it either.
    assert not await lifecycle_storage.mark_outbox_sent(  # type: ignore[attr-defined]
        item.outbox_id,
        receipt_id="stale-attempt",
        attempt_number=1,
        expected_worker_id="worker-a",
    )
    assert await _snapshot(lifecycle_storage, item.outbox_id) == _Snapshot(
        "in_progress", 1, 2, None, "worker-a"
    )

    assert await lifecycle_storage.mark_outbox_sent(  # type: ignore[attr-defined]
        item.outbox_id,
        receipt_id="committed-attempt-2",
        attempt_number=2,
        expected_worker_id="worker-a",
    )
    final = await _snapshot(lifecycle_storage, item.outbox_id)
    assert final.status == "sent"
    assert final.attempt_number == 2
    assert final.active_attempt is None
    assert final.receipt_id == "committed-attempt-2"


async def test_sibling_generation_blocks_stale_reservation(
    lifecycle_storage: object,
) -> None:
    """Both backends reject a reservation already represented by a sibling row."""
    event_id = "evt-model-sibling-generation"
    original = _item(
        outbox_id="obox-model-sibling-original",
        event_id=event_id,
        attempt=1,
        worker_id="worker-a",
    )
    sibling = _item(
        outbox_id="obox-model-sibling-newer",
        event_id=event_id,
        attempt=2,
        worker_id="worker-b",
    )
    await _admit(lifecycle_storage, original)
    await _admit(lifecycle_storage, sibling)

    reserved = await lifecycle_storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
        original.outbox_id,
        "worker-a",
        1,
    )
    assert reserved is None
    assert await _snapshot(lifecycle_storage, original.outbox_id) == _Snapshot(
        "in_progress", 1, None, None, "worker-a"
    )


async def test_live_reservation_blocks_sibling_generation_create(
    lifecycle_storage: object,
) -> None:
    """Both backends reuse a live reservation instead of creating its sibling."""
    event_id = "evt-model-create-after-reserve"
    original = _item(
        outbox_id="obox-model-create-original",
        event_id=event_id,
        attempt=1,
        worker_id="worker-a",
    )
    await _admit(lifecycle_storage, original)
    reserved = await lifecycle_storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
        original.outbox_id,
        "worker-a",
        1,
    )
    assert reserved == 2

    sibling = _item(
        outbox_id="obox-model-create-sibling",
        event_id=event_id,
        attempt=2,
        worker_id="worker-b",
    )
    resolved = await lifecycle_storage.create_outbox_item(sibling)  # type: ignore[attr-defined]
    assert resolved.outbox_id == original.outbox_id
    assert await _snapshot(lifecycle_storage, original.outbox_id) == _Snapshot(
        "in_progress", 1, 2, None, "worker-a"
    )


async def test_replay_generation_allocation_is_atomic_and_fresh(
    lifecycle_storage: object,
) -> None:
    """Both backends allocate replay above a live retry reservation atomically."""
    event_id = "evt-model-atomic-replay-generation"
    original = _item(
        outbox_id="obox-model-atomic-replay-original",
        event_id=event_id,
        attempt=1,
        worker_id="worker-a",
    )
    await _admit(lifecycle_storage, original)
    reserved = await lifecycle_storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
        original.outbox_id,
        "worker-a",
        1,
    )
    assert reserved == 2

    replay = _item(
        outbox_id="obox-model-atomic-replay-new",
        event_id=event_id,
        attempt=1,
        worker_id="worker-replay",
    )
    created = await lifecycle_storage.create_outbox_item(  # type: ignore[attr-defined]
        replay,
        allocate_new_generation=True,
    )
    assert created.outbox_id == replay.outbox_id
    assert created.attempt_number == 3
    assert await _snapshot(lifecycle_storage, original.outbox_id) == _Snapshot(
        "in_progress", 1, 2, None, "worker-a"
    )


async def test_terminal_finalization_is_atomic_and_same_attempt(
    lifecycle_storage: object,
) -> None:
    event_id = "evt-model-terminal"
    item = _item(outbox_id="obox-model-terminal", event_id=event_id)
    await _admit(lifecycle_storage, item)
    attempt = _attempt_receipt(
        "rcpt-model-failed",
        outbox_id=item.outbox_id,
        event_id=event_id,
        attempt=1,
    )
    terminal = _lifecycle_receipt(
        "rcpt-model-dead",
        outbox_id=item.outbox_id,
        event_id=event_id,
        attempt=1,
        parent_receipt_id=attempt.receipt_id,
    )

    committed = await lifecycle_storage.finalize_outbox_terminal(  # type: ignore[attr-defined]
        TerminalOutboxFinalization(
            lifecycle_receipt=terminal,
            attempt_receipt=attempt,
            error_summary="transport failed",
        )
    )
    assert committed
    assert await _receipt_ids(lifecycle_storage, event_id) == [
        attempt.receipt_id,
        terminal.receipt_id,
    ]
    final = await _snapshot(lifecycle_storage, item.outbox_id)
    assert final.status == "dead_lettered"
    assert final.attempt_number == 1
    assert final.active_attempt is None
    assert final.receipt_id == terminal.receipt_id


async def test_terminal_identity_mismatch_commits_nothing(
    lifecycle_storage: object,
) -> None:
    event_id = "evt-model-terminal-mismatch"
    item = _item(outbox_id="obox-model-terminal-mismatch", event_id=event_id)
    await _admit(lifecycle_storage, item)
    terminal = _lifecycle_receipt(
        "rcpt-model-wrong-channel",
        outbox_id=item.outbox_id,
        event_id=event_id,
        attempt=1,
        status="cancelled",
        channel="other-channel",
    )

    committed = await lifecycle_storage.finalize_outbox_terminal(  # type: ignore[attr-defined]
        TerminalOutboxFinalization(lifecycle_receipt=terminal)
    )
    assert not committed

    assert await _receipt_ids(lifecycle_storage, event_id) == []
    assert (await _snapshot(lifecycle_storage, item.outbox_id)).status == "in_progress"


async def test_stale_terminal_attempt_after_new_reservation_commits_nothing(
    lifecycle_storage: object,
) -> None:
    event_id = "evt-model-stale-terminal"
    item = _item(
        outbox_id="obox-model-stale-terminal",
        event_id=event_id,
        worker_id="worker-a",
    )
    await _admit(lifecycle_storage, item)
    assert (
        await lifecycle_storage.reserve_outbox_attempt(  # type: ignore[attr-defined]
            item.outbox_id,
            "worker-a",
            1,
        )
        == 2
    )
    stale = _lifecycle_receipt(
        "rcpt-model-stale-terminal",
        outbox_id=item.outbox_id,
        event_id=event_id,
        attempt=1,
        status="abandoned",
    )

    committed = await lifecycle_storage.finalize_outbox_terminal(  # type: ignore[attr-defined]
        TerminalOutboxFinalization(lifecycle_receipt=stale)
    )
    assert not committed
    assert await _receipt_ids(lifecycle_storage, event_id) == []
    assert await _snapshot(lifecycle_storage, item.outbox_id) == _Snapshot(
        "in_progress", 1, 2, None, "worker-a"
    )


async def test_sqlite_schema_rejects_invalid_outbox_state(
    temp_storage: SQLiteStorage,
) -> None:
    """DDL constraints preserve the executable lifecycle invariants."""
    event_id = "evt-model-schema-guards"
    item = _item(
        outbox_id="obox-model-schema-guards",
        event_id=event_id,
        worker_id="worker-a",
    )
    await _admit(temp_storage, item)

    invalid_updates = (
        (
            "UPDATE delivery_outbox SET attempt_number = 0 WHERE outbox_id = ?",
            (item.outbox_id,),
        ),
        (
            "UPDATE delivery_outbox SET active_attempt = attempt_number WHERE outbox_id = ?",
            (item.outbox_id,),
        ),
        (
            "UPDATE delivery_outbox "
            "SET active_attempt = attempt_number + 1, status = 'queued' "
            "WHERE outbox_id = ?",
            (item.outbox_id,),
        ),
        (
            "UPDATE delivery_outbox SET status = 'unknown_state' WHERE outbox_id = ?",
            (item.outbox_id,),
        ),
    )

    original = await _snapshot(temp_storage, item.outbox_id)
    for sql, params in invalid_updates:
        with pytest.raises(StorageError):
            await temp_storage._write(sql, params)
        assert await _snapshot(temp_storage, item.outbox_id) == original


async def test_sqlite_outbox_has_event_scoped_lineage_index(
    temp_storage: SQLiteStorage,
) -> None:
    rows = await temp_storage._read_all("PRAGMA index_list('delivery_outbox')")
    assert "idx_outbox_lineage" in {str(row["name"]) for row in rows}
