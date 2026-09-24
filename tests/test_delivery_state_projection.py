"""Regression tests for runtime delivery-state projection coherence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle

_TS = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _event(event_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=_TS,
        source_adapter="matrix",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "projection coherence"},
        metadata=EventMetadata(),
    )


def _outbox(
    *, outbox_id: str, event_id: str, replay_run_id: str | None = None
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id="plan-1",
        target_adapter="radio",
        target_channel="ch-0",
        replay_run_id=replay_run_id,
    )


@pytest.mark.asyncio
async def test_target_projection_uses_current_generation_not_older_authority(
    tmp_path,
) -> None:
    event_id = "ev-current-generation-projection"
    db_path = str(tmp_path / "projection.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await storage.append(_event(event_id))
        await storage.create_outbox_item(
            outbox_id := _outbox(outbox_id="ob-live", event_id=event_id)
        )
        claimed = await storage.claim_due_outbox_items(
            now="2026-09-24T12:00:00+00:00",
            worker_id="worker-live",
            lease_seconds=30,
            limit=10,
        )
        assert [item.outbox_id for item in claimed] == [outbox_id.outbox_id]

        receipt = DeliveryReceipt(
            receipt_id="rcpt-old-live",
            event_id=event_id,
            delivery_plan_id="plan-1",
            target_adapter="radio",
            target_channel="ch-0",
            route_id="route-1",
            status="sent",
            failure_kind=None,
            error=None,
            attempt_number=1,
            source="live",
            adapter_message_id="old-message-id",
            outbox_id="ob-live",
            created_at=_TS,
        )
        await storage.append_receipt(receipt)
        assert await storage.mark_outbox_sent(
            "ob-live",
            receipt_id=receipt.receipt_id,
            attempt_number=1,
        )

        replay_candidate = _outbox(
            outbox_id="ob-replay",
            event_id=event_id,
            replay_run_id="run-fresh",
        )
        replay_row = await storage.create_outbox_item(
            replay_candidate,
            allocate_new_generation=True,
        )
        assert replay_row.attempt_number == 2
        assert replay_row.status == "pending"

    finally:
        await storage.close()

    bundle = await collect_evidence_bundle(storage_path=db_path, event_id=event_id)
    storage_data = bundle["sections"]["storage"]["data"]
    target_state = next(
        iter(storage_data["incident_summary"]["delivery_state_by_target"].values())
    )
    ledger_entry = next(
        iter(storage_data["delivery_outcome_ledger"]["entries"].values())
    )

    assert target_state["status"] == "pending"
    assert target_state["attempt_number"] == 2
    assert target_state["source"] == "replay"
    assert target_state["replay_run_id"] == "run-fresh"
    assert target_state["adapter_message_id"] is None
    assert target_state["native_message_id"] is None
    assert target_state["failure_kind"] is None
    assert target_state["error"] is None

    assert ledger_entry["lifecycle_status"] == target_state["status"]
    assert ledger_entry["current_attempt_number"] == target_state["attempt_number"]
    assert ledger_entry["source"] == target_state["source"]
    assert ledger_entry["replay_run_id"] == target_state["replay_run_id"]
    assert ledger_entry["latest_attempt_number"] == 1
    assert ledger_entry["authoritative_receipt_id"] == "rcpt-old-live"
    assert ledger_entry["current_receipt_id"] is None
    assert ledger_entry["current_receipt_status"] is None
