"""Conformance tests for shared current-delivery authority resolution."""

from __future__ import annotations

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    DeliveryIdentity,
    delivery_identity,
)
from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import admit_event


def _receipt(
    receipt_id: str,
    *,
    sequence: int,
    event_id: str = "evt-authority",
    plan_id: str = "plan-authority",
    adapter: str = "radio",
    channel: str | None = "mesh",
    outbox_id: str | None = None,
    status: str = "sent",
    attempt: int = 1,
) -> dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "sequence": sequence,
        "event_id": event_id,
        "delivery_plan_id": plan_id,
        "target_adapter": adapter,
        "target_channel": channel,
        "outbox_id": outbox_id,
        "status": status,
        "attempt_number": attempt,
    }


def _outbox(
    outbox_id: str,
    *,
    receipt_id: str | None,
    event_id: str = "evt-authority",
    plan_id: str = "plan-authority",
    adapter: str = "radio",
    channel: str | None = "mesh",
    attempt: int = 1,
) -> dict[str, object]:
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "delivery_plan_id": plan_id,
        "target_adapter": adapter,
        "target_channel": channel,
        "receipt_id": receipt_id,
        "attempt_number": attempt,
    }


def test_delivery_identity_normalizes_empty_channel() -> None:
    assert delivery_identity(
        {
            "event_id": "e",
            "delivery_plan_id": "p",
            "target_adapter": "a",
            "target_channel": "",
        }
    ) == DeliveryIdentity("e", "p", "a", None)


def test_resolver_rejects_uncommitted_outbox_receipt() -> None:
    receipts = [
        _receipt("committed", sequence=1, outbox_id="obox-1"),
        _receipt("stale-late", sequence=2, outbox_id="obox-1", status="failed"),
    ]
    resolver = DeliveryAuthorityResolver(
        receipts,
        [_outbox("obox-1", receipt_id="committed")],
    )
    identity = delivery_identity(receipts[0])

    current = resolver.current(identity)
    assert current is not None
    assert current["receipt_id"] == "committed"


def test_resolver_keeps_all_outbox_generations_authoritative() -> None:
    receipts = [
        _receipt("gen-1", sequence=1, outbox_id="obox-1", attempt=1),
        _receipt("gen-2", sequence=3, outbox_id="obox-2", attempt=2),
        _receipt("stale", sequence=4, outbox_id="obox-1", status="failed"),
    ]
    resolver = DeliveryAuthorityResolver(
        receipts,
        [
            _outbox("obox-1", receipt_id="gen-1", attempt=1),
            _outbox("obox-2", receipt_id="gen-2", attempt=2),
        ],
    )

    current = resolver.current(delivery_identity(receipts[0]))
    assert current is not None
    assert current["receipt_id"] == "gen-2"


def test_newer_generation_outranks_late_append_from_older_generation() -> None:
    receipts = [
        _receipt("gen-2", sequence=2, outbox_id="obox-2", attempt=2),
        _receipt("gen-1-late", sequence=99, outbox_id="obox-1", attempt=1),
    ]
    resolver = DeliveryAuthorityResolver(
        receipts,
        [
            _outbox("obox-1", receipt_id="gen-1-late", attempt=1),
            _outbox("obox-2", receipt_id="gen-2", attempt=2),
        ],
    )

    current = resolver.current(delivery_identity(receipts[0]))
    assert current is not None
    assert current["receipt_id"] == "gen-2"


def test_outboxless_receipt_remains_eligible_with_outbox_generations() -> None:
    receipts = [
        _receipt("committed", sequence=1, outbox_id="obox-1"),
        _receipt("observation-only", sequence=2, outbox_id=None, status="failed"),
    ]
    resolver = DeliveryAuthorityResolver(
        receipts,
        [_outbox("obox-1", receipt_id="committed")],
    )

    current = resolver.current(delivery_identity(receipts[0]))
    assert current is not None
    assert current["receipt_id"] == "observation-only"


async def test_sqlite_delivery_status_matches_shared_resolver(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "evt-authority-sql"
    await admit_event(temp_storage, event_id)

    first = DeliveryOutboxItem(
        outbox_id="obox-auth-1",
        event_id=event_id,
        route_id="route-a",
        delivery_plan_id="plan-authority-sql",
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=1,
        status="in_progress",
    )
    second = DeliveryOutboxItem(
        outbox_id="obox-auth-2",
        event_id=event_id,
        route_id="route-b",
        delivery_plan_id="plan-authority-sql",
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=2,
        status="in_progress",
    )
    await temp_storage.create_outbox_item(first)
    await temp_storage.create_outbox_item(second)

    receipts = [
        DeliveryReceipt(
            receipt_id="auth-gen-1",
            event_id=event_id,
            delivery_plan_id="plan-authority-sql",
            target_adapter="radio",
            target_channel="mesh",
            route_id="route-a",
            status="sent",
            outbox_id=first.outbox_id,
            attempt_number=1,
        ),
        DeliveryReceipt(
            receipt_id="auth-gen-2",
            event_id=event_id,
            delivery_plan_id="plan-authority-sql",
            target_adapter="radio",
            target_channel="mesh",
            route_id="route-b",
            status="sent",
            outbox_id=second.outbox_id,
            attempt_number=2,
        ),
        DeliveryReceipt(
            receipt_id="auth-stale-late",
            event_id=event_id,
            delivery_plan_id="plan-authority-sql",
            target_adapter="radio",
            target_channel="mesh",
            route_id="route-a",
            status="failed",
            outbox_id=first.outbox_id,
            attempt_number=1,
        ),
    ]
    for receipt in receipts:
        await temp_storage.append_receipt(receipt)

    assert await temp_storage.mark_outbox_sent(
        first.outbox_id,
        receipt_id="auth-gen-1",
        attempt_number=1,
    )
    assert await temp_storage.mark_outbox_sent(
        second.outbox_id,
        receipt_id="auth-gen-2",
        attempt_number=2,
    )

    stored_receipts = await temp_storage.list_receipts_for_event(event_id)
    stored_outbox = await temp_storage.list_outbox_items_for_event(event_id)
    resolver = DeliveryAuthorityResolver(stored_receipts, stored_outbox)
    identity = DeliveryIdentity(event_id, "plan-authority-sql", "radio", "mesh")
    resolved = resolver.current(identity)
    projected = await temp_storage.delivery_status(
        "plan-authority-sql",
        "radio",
        "mesh",
        event_id=event_id,
    )

    assert resolved is not None
    assert projected is not None
    assert resolved.receipt_id == "auth-gen-2"
    assert projected.receipt_id == resolved.receipt_id


async def test_sqlite_newer_generation_outranks_late_committed_older_generation(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "evt-authority-sql-generation"
    plan_id = "plan-authority-sql-generation"
    await admit_event(temp_storage, event_id)

    older = DeliveryOutboxItem(
        outbox_id="obox-auth-old",
        event_id=event_id,
        route_id="route-old",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=1,
        status="in_progress",
    )
    newer = DeliveryOutboxItem(
        outbox_id="obox-auth-new",
        event_id=event_id,
        route_id="route-new",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=2,
        status="in_progress",
    )
    await temp_storage.create_outbox_item(older)
    await temp_storage.create_outbox_item(newer)

    newer_receipt = DeliveryReceipt(
        receipt_id="auth-newer-generation",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-new",
        status="sent",
        outbox_id=newer.outbox_id,
        attempt_number=2,
    )
    older_late_receipt = DeliveryReceipt(
        receipt_id="auth-older-generation-late",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-old",
        status="sent",
        outbox_id=older.outbox_id,
        attempt_number=1,
    )
    # Commit the newer generation's evidence first, then append and commit an
    # older generation later. Append order must not regress lifecycle authority.
    await temp_storage.append_receipt(newer_receipt)
    await temp_storage.append_receipt(older_late_receipt)
    assert await temp_storage.mark_outbox_sent(
        newer.outbox_id,
        receipt_id=newer_receipt.receipt_id,
        attempt_number=2,
    )
    assert await temp_storage.mark_outbox_sent(
        older.outbox_id,
        receipt_id=older_late_receipt.receipt_id,
        attempt_number=1,
    )

    stored_receipts = await temp_storage.list_receipts_for_event(event_id)
    stored_outbox = await temp_storage.list_outbox_items_for_event(event_id)
    resolver = DeliveryAuthorityResolver(stored_receipts, stored_outbox)
    identity = DeliveryIdentity(event_id, plan_id, "radio", "mesh")
    resolved = resolver.current(identity)
    projected = await temp_storage.delivery_status(
        plan_id,
        "radio",
        "mesh",
        event_id=event_id,
    )

    assert resolved is not None
    assert projected is not None
    assert resolved.receipt_id == newer_receipt.receipt_id
    assert projected.receipt_id == newer_receipt.receipt_id


async def test_recovery_scan_prefers_newer_failed_generation_over_late_older_terminal(
    temp_storage: SQLiteStorage,
) -> None:
    """Recovery consumes the same generation-aware authority as delivery_status."""
    event_id = "evt-authority-recovery-new-failed"
    plan_id = "plan-authority-recovery-new-failed"
    await admit_event(temp_storage, event_id)
    older = DeliveryOutboxItem(
        outbox_id="obox-recovery-old-terminal",
        event_id=event_id,
        route_id="route-old",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=1,
        status="in_progress",
    )
    newer = DeliveryOutboxItem(
        outbox_id="obox-recovery-new-failed",
        event_id=event_id,
        route_id="route-new",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=2,
        status="in_progress",
    )
    await temp_storage.create_outbox_item(older)
    await temp_storage.create_outbox_item(newer)

    newer_failed = DeliveryReceipt(
        receipt_id="rcpt-recovery-new-failed",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-new",
        status="failed",
        failure_kind="adapter_transient",
        attempt_number=2,
        outbox_id=newer.outbox_id,
    )
    await temp_storage.append_receipt(newer_failed)
    assert await temp_storage.mark_outbox_retry_wait(
        newer.outbox_id,
        next_attempt_at="2099-01-01T00:00:00+00:00",
        receipt_id=newer_failed.receipt_id,
        failure_kind="adapter_transient",
        attempt_number=2,
    )

    older_failed = DeliveryReceipt(
        receipt_id="rcpt-recovery-old-failed",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-old",
        status="failed",
        failure_kind="adapter_permanent",
        attempt_number=1,
        outbox_id=older.outbox_id,
    )
    older_terminal = DeliveryReceipt(
        receipt_id="rcpt-recovery-old-terminal",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-old",
        status="dead_lettered",
        receipt_kind="lifecycle",
        failure_kind="adapter_permanent",
        attempt_number=1,
        parent_receipt_id=older_failed.receipt_id,
        outbox_id=older.outbox_id,
    )
    await temp_storage.append_receipt(older_failed)
    assert await temp_storage.finalize_outbox_terminal(
        older_terminal,
        outbox_id=older.outbox_id,
        attempt_number=1,
        terminal_status="dead_lettered",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        failure_kind="adapter_permanent",
    )

    page = await temp_storage.query_unresolved_deliveries()
    current = [item for item in page.items if item.event_id == event_id]
    assert len(current) == 1
    assert (current[0].receipt_id, current[0].status) == (
        newer_failed.receipt_id,
        "failed",
    )


async def test_recovery_scan_prefers_newer_terminal_generation_over_late_older_failed(
    temp_storage: SQLiteStorage,
) -> None:
    """Late older retry evidence cannot hide a newer terminal generation."""
    event_id = "evt-authority-recovery-new-terminal"
    plan_id = "plan-authority-recovery-new-terminal"
    await admit_event(temp_storage, event_id)
    older = DeliveryOutboxItem(
        outbox_id="obox-recovery-old-failed",
        event_id=event_id,
        route_id="route-old",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=1,
        status="in_progress",
    )
    newer = DeliveryOutboxItem(
        outbox_id="obox-recovery-new-terminal",
        event_id=event_id,
        route_id="route-new",
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        attempt_number=2,
        status="in_progress",
    )
    await temp_storage.create_outbox_item(older)
    await temp_storage.create_outbox_item(newer)

    newer_failed = DeliveryReceipt(
        receipt_id="rcpt-recovery-newer-failed",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-new",
        status="failed",
        failure_kind="adapter_permanent",
        attempt_number=2,
        outbox_id=newer.outbox_id,
    )
    newer_terminal = DeliveryReceipt(
        receipt_id="rcpt-recovery-newer-terminal",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-new",
        status="dead_lettered",
        receipt_kind="lifecycle",
        failure_kind="adapter_permanent",
        attempt_number=2,
        parent_receipt_id=newer_failed.receipt_id,
        outbox_id=newer.outbox_id,
    )
    await temp_storage.append_receipt(newer_failed)
    assert await temp_storage.finalize_outbox_terminal(
        newer_terminal,
        outbox_id=newer.outbox_id,
        attempt_number=2,
        terminal_status="dead_lettered",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        failure_kind="adapter_permanent",
    )

    older_failed = DeliveryReceipt(
        receipt_id="rcpt-recovery-older-late-failed",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="radio",
        target_channel="mesh",
        route_id="route-old",
        status="failed",
        failure_kind="adapter_transient",
        attempt_number=1,
        outbox_id=older.outbox_id,
    )
    await temp_storage.append_receipt(older_failed)
    assert await temp_storage.mark_outbox_retry_wait(
        older.outbox_id,
        next_attempt_at="2099-01-01T00:00:00+00:00",
        receipt_id=older_failed.receipt_id,
        failure_kind="adapter_transient",
        attempt_number=1,
    )

    page = await temp_storage.query_unresolved_deliveries()
    current = [item for item in page.items if item.event_id == event_id]
    assert len(current) == 1
    assert (current[0].receipt_id, current[0].status) == (
        newer_terminal.receipt_id,
        "dead_lettered",
    )
