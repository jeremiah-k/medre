"""Event timeline visibility for durable named replay admission."""

from __future__ import annotations

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.timeline import assemble_event_timeline
from tests.helpers.pipeline import make_event


async def _admit_named_replay(
    storage: SQLiteStorage,
    *,
    event_id: str = "evt-event-trace-replay",
    run_id: str = "run-event-trace",
) -> DeliveryOutboxItem:
    event = make_event(event_id=event_id, source_adapter="src")
    await storage.append(event)
    return await storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="obox-event-trace-replay",
            event_id=event_id,
            route_id="route-event-trace",
            delivery_plan_id="plan-event-trace",
            target_adapter="dest",
            target_channel="room",
            attempt_number=1,
            status="in_progress",
            worker_id="replay-worker",
            replay_run_id=run_id,
        ),
        allocate_new_generation=True,
    )


async def test_event_timeline_exposes_named_replay_before_first_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    claimed = await _admit_named_replay(temp_storage)

    result = await assemble_event_timeline(temp_storage, claimed.event_id)

    assert result is not None
    assert result["receipts"] == []
    assert result["source"] == "none"
    assert result["replay_run_ids"] == ["run-event-trace"]
    assert result["replay_runs"] == {"run-event-trace": []}
    assert [item.outbox_id for item in result["outbox_items"]] == [claimed.outbox_id]
    generation = next(
        entry
        for entry in result["timeline_entries"]
        if entry["entry_type"] == "outbox_generation"
    )
    assert generation["data"]["outbox_id"] == claimed.outbox_id
    assert generation["data"]["status"] == "in_progress"
    assert generation["data"]["replay_run_id"] == "run-event-trace"
    assert generation["data"]["created_at"] is not None
    assert generation["data"]["updated_at"] is not None


async def test_event_timeline_keeps_retry_source_separate_from_replay_origin(
    temp_storage: SQLiteStorage,
) -> None:
    claimed = await _admit_named_replay(temp_storage)
    receipt = DeliveryReceipt(
        receipt_id="rcpt-event-trace-retry",
        event_id=claimed.event_id,
        delivery_plan_id=claimed.delivery_plan_id,
        target_adapter=claimed.target_adapter,
        target_channel=claimed.target_channel,
        route_id=claimed.route_id,
        status="failed",
        error="transient",
        attempt_number=2,
        outbox_id=claimed.outbox_id,
        source="retry",
        replay_run_id="run-event-trace",
    )
    await temp_storage.append_receipt(receipt)

    result = await assemble_event_timeline(temp_storage, claimed.event_id)

    assert result is not None
    assert result["source"] == "retry"
    assert result["replay_run_ids"] == ["run-event-trace"]
    assert [r.receipt_id for r in result["replay_runs"]["run-event-trace"]] == [
        receipt.receipt_id
    ]
    receipt_entry = next(
        entry
        for entry in result["timeline_entries"]
        if entry["entry_type"] == "receipt"
    )
    assert receipt_entry["data"]["source"] == "retry"
    assert receipt_entry["data"]["replay_run_id"] == "run-event-trace"
