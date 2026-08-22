"""Atomic queued-delivery finalization storage tests."""

from __future__ import annotations

from datetime import UTC, datetime

import msgspec
import pytest

from medre.core.events import DeliveryReceipt, NativeMessageRef
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import admit_event, append_receipt_with_parent


EVENT_ID = "evt-atomic-finalize"
OUTBOX_ID = "obox-atomic-finalize"
PLAN_ID = "plan-atomic-finalize"
ADAPTER = "mesh-atomic"
CHANNEL = "0"


async def _seed_queued_attempt(storage: SQLiteStorage) -> DeliveryReceipt:
    await admit_event(storage, EVENT_ID)
    queued = DeliveryReceipt(
        receipt_id="rcpt-queued-atomic",
        event_id=EVENT_ID,
        delivery_plan_id=PLAN_ID,
        target_adapter=ADAPTER,
        target_channel=CHANNEL,
        route_id="route-atomic",
        status="queued",
        attempt_number=1,
        outbox_id=OUTBOX_ID,
        confirmation_level="local_queue",
        created_at=datetime.now(UTC),
    )
    await append_receipt_with_parent(storage, queued)
    await storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=OUTBOX_ID,
            event_id=EVENT_ID,
            route_id="route-atomic",
            delivery_plan_id=PLAN_ID,
            target_adapter=ADAPTER,
            target_channel=CHANNEL,
            attempt_number=1,
            status="in_progress",
        )
    )
    await storage.mark_outbox_queued(
        OUTBOX_ID,
        receipt_id=queued.receipt_id,
        attempt_number=1,
    )
    return queued


def _sent_evidence(
    *, receipt_id: str = "rcpt-sent-atomic", native_id: str = "pkt-atomic"
) -> tuple[NativeMessageRef, DeliveryReceipt]:
    now = datetime.now(UTC)
    native_ref = NativeMessageRef(
        id=f"nref-{native_id}",
        event_id=EVENT_ID,
        adapter=ADAPTER,
        native_channel_id=CHANNEL,
        native_message_id=native_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={},
        created_at=now,
    )
    sent = DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=EVENT_ID,
        delivery_plan_id=PLAN_ID,
        target_adapter=ADAPTER,
        target_channel=CHANNEL,
        route_id="route-atomic",
        status="sent",
        adapter_message_id=native_id,
        attempt_number=1,
        parent_receipt_id="rcpt-queued-atomic",
        outbox_id=OUTBOX_ID,
        confirmation_level="local_transport",
        created_at=now,
    )
    return native_ref, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "native_updates",
        "receipt_updates",
        "outbox_id",
        "attempt_number",
        "message",
    ),
    [
        ({"direction": "inbound"}, {}, OUTBOX_ID, 1, "outbound native ref"),
        ({}, {"status": "failed"}, OUTBOX_ID, 1, "sent receipt"),
        ({"event_id": "evt-other"}, {}, OUTBOX_ID, 1, "event_id must match"),
        ({"adapter": "other"}, {}, OUTBOX_ID, 1, "adapter must match"),
        (
            {"native_message_id": "pkt-other"},
            {},
            OUTBOX_ID,
            1,
            "native_message_id must match",
        ),
        ({}, {"outbox_id": "obox-other"}, OUTBOX_ID, 1, "outbox_id must match"),
        ({}, {"attempt_number": 2}, OUTBOX_ID, 1, "attempt_number must match"),
        ({}, {"attempt_number": 0}, OUTBOX_ID, 0, "attempt_number must be >= 1"),
    ],
)
async def test_finalize_queued_delivery_rejects_invalid_evidence(
    temp_storage: SQLiteStorage,
    native_updates: dict[str, object],
    receipt_updates: dict[str, object],
    outbox_id: str,
    attempt_number: int,
    message: str,
) -> None:
    native_ref, sent = _sent_evidence()
    candidate_ref = msgspec.structs.replace(native_ref, **native_updates)
    candidate_receipt = msgspec.structs.replace(sent, **receipt_updates)

    with pytest.raises(ValueError, match=message):
        await temp_storage.finalize_queued_delivery(
            candidate_ref,
            candidate_receipt,
            outbox_id=outbox_id,
            attempt_number=attempt_number,
        )


@pytest.mark.asyncio
async def test_finalize_queued_delivery_commits_all_evidence(
    temp_storage: SQLiteStorage,
) -> None:
    await _seed_queued_attempt(temp_storage)
    native_ref, sent = _sent_evidence()

    committed = await temp_storage.finalize_queued_delivery(
        native_ref,
        sent,
        outbox_id=OUTBOX_ID,
        attempt_number=1,
    )

    assert committed is True
    assert (
        await temp_storage.resolve_native_ref(ADAPTER, CHANNEL, "pkt-atomic")
        == EVENT_ID
    )
    receipts = await temp_storage.list_receipts_for_event(EVENT_ID)
    assert [receipt.status for receipt in receipts] == ["queued", "sent"]
    outbox = await temp_storage.get_outbox_item(OUTBOX_ID)
    assert outbox is not None
    assert outbox.status == "sent"
    assert outbox.receipt_id == sent.receipt_id


@pytest.mark.asyncio
async def test_finalize_queued_delivery_stale_guard_commits_nothing(
    temp_storage: SQLiteStorage,
) -> None:
    queued = await _seed_queued_attempt(temp_storage)
    await temp_storage.mark_outbox_sent(
        OUTBOX_ID,
        receipt_id="rcpt-other-sent",
        attempt_number=1,
    )
    native_ref, sent = _sent_evidence(native_id="pkt-stale")

    committed = await temp_storage.finalize_queued_delivery(
        native_ref,
        sent,
        outbox_id=OUTBOX_ID,
        attempt_number=1,
    )

    assert committed is False
    assert await temp_storage.resolve_native_ref(ADAPTER, CHANNEL, "pkt-stale") is None
    receipts = await temp_storage.list_receipts_for_event(EVENT_ID)
    assert [receipt.receipt_id for receipt in receipts] == [queued.receipt_id]


@pytest.mark.asyncio
async def test_finalize_queued_delivery_receipt_failure_rolls_back_all_tables(
    temp_storage: SQLiteStorage,
) -> None:
    queued = await _seed_queued_attempt(temp_storage)
    # Force the final receipt insert to fail after the guarded outbox UPDATE
    # and native-ref insert have executed inside the transaction.
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-collision",
            event_id=EVENT_ID,
            delivery_plan_id="plan-unrelated",
            target_adapter="other-adapter",
            target_channel=None,
            route_id="route-unrelated",
            status="sent",
            attempt_number=1,
            confirmation_level="local_transport",
            created_at=datetime.now(UTC),
        )
    )
    native_ref, sent = _sent_evidence(
        receipt_id="rcpt-collision",
        native_id="pkt-rollback",
    )

    with pytest.raises(StorageError):
        await temp_storage.finalize_queued_delivery(
            native_ref,
            sent,
            outbox_id=OUTBOX_ID,
            attempt_number=1,
        )

    assert (
        await temp_storage.resolve_native_ref(ADAPTER, CHANNEL, "pkt-rollback")
        is None
    )
    outbox = await temp_storage.get_outbox_item(OUTBOX_ID)
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.receipt_id == queued.receipt_id


@pytest.mark.asyncio
async def test_finalize_queued_delivery_rejects_conflicting_native_identity(
    temp_storage: SQLiteStorage,
) -> None:
    queued = await _seed_queued_attempt(temp_storage)
    await admit_event(temp_storage, "evt-other")
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="nref-existing-conflict",
            event_id="evt-other",
            adapter=ADAPTER,
            native_channel_id=CHANNEL,
            native_message_id="pkt-conflict",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            metadata={},
            created_at=datetime.now(UTC),
        )
    )
    native_ref, sent = _sent_evidence(native_id="pkt-conflict")

    with pytest.raises(StorageError, match="different canonical event"):
        await temp_storage.finalize_queued_delivery(
            native_ref,
            sent,
            outbox_id=OUTBOX_ID,
            attempt_number=1,
        )

    outbox = await temp_storage.get_outbox_item(OUTBOX_ID)
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.receipt_id == queued.receipt_id
    receipts = await temp_storage.list_receipts_for_event(EVENT_ID)
    assert [receipt.status for receipt in receipts] == ["queued"]


@pytest.mark.asyncio
async def test_finalize_queued_delivery_native_ref_id_collision_rolls_back(
    temp_storage: SQLiteStorage,
) -> None:
    queued = await _seed_queued_attempt(temp_storage)
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="nref-pkt-id-collision",
            event_id=EVENT_ID,
            adapter=ADAPTER,
            native_channel_id=CHANNEL,
            native_message_id="pkt-existing",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            metadata={},
            created_at=datetime.now(UTC),
        )
    )
    native_ref, sent = _sent_evidence(native_id="pkt-id-collision")

    with pytest.raises(StorageError):
        await temp_storage.finalize_queued_delivery(
            native_ref,
            sent,
            outbox_id=OUTBOX_ID,
            attempt_number=1,
        )

    assert (
        await temp_storage.resolve_native_ref(ADAPTER, CHANNEL, "pkt-id-collision")
        is None
    )
    outbox = await temp_storage.get_outbox_item(OUTBOX_ID)
    assert outbox is not None
    assert outbox.status == "queued"
    assert outbox.receipt_id == queued.receipt_id
    receipts = await temp_storage.list_receipts_for_event(EVENT_ID)
    assert [receipt.status for receipt in receipts] == ["queued"]
