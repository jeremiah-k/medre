"""Cross-surface current-outcome regressions for receipt append ordering."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle

_TS = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _event(event_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=_TS,
        source_adapter="matrix-main",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "append-order authority"},
        metadata=EventMetadata(),
    )


def _receipt(
    *,
    receipt_id: str,
    event_id: str,
    status: str,
    attempt_number: int,
    source: str = "live",
    replay_run_id: str | None = None,
    failure_kind: str | None = None,
    error: str | None = None,
    outbox_id: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-append-order",
        target_adapter="meshtastic-main",
        target_channel="0",
        route_id="route-append-order",
        status=status,
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
        failure_kind=failure_kind,
        error=error,
        outbox_id=outbox_id,
        created_at=_TS,
    )


async def test_evidence_current_outcome_uses_later_append_sequence(
    tmp_path: Path,
) -> None:
    """A later receipt supersedes an older higher-attempt receipt everywhere."""
    event_id = "evt-append-order-evidence"
    db_path = tmp_path / "append-order-evidence.db"
    storage = SQLiteStorage(str(db_path))
    try:
        await storage.initialize()
        await storage.append(_event(event_id))
        await storage.append_receipt(
            _receipt(
                receipt_id="receipt-old-high-attempt",
                event_id=event_id,
                status="failed",
                attempt_number=4,
                failure_kind="adapter_transient",
                error="TimeoutError",
            )
        )
        await storage.append_receipt(
            _receipt(
                receipt_id="receipt-later-replay",
                event_id=event_id,
                status="sent",
                attempt_number=1,
                source="replay",
                replay_run_id="replay-append-order",
            )
        )
    finally:
        await storage.close()

    report = await collect_evidence_bundle(
        storage_path=str(db_path),
        event_id=event_id,
    )
    summary = report["sections"]["storage"]["data"]["incident_summary"]
    entry = next(iter(summary["delivery_state_by_target"].values()))

    assert entry["status"] == "sent"
    assert entry["attempt_number"] == 1
    assert entry["source"] == "replay"
    assert entry["replay_run_id"] == "replay-append-order"


async def test_evidence_current_outcome_uses_committed_outbox_receipt(
    tmp_path: Path,
) -> None:
    """A rejected later append stays history for outbox-backed delivery."""
    event_id = "evt-outbox-authority-evidence"
    db_path = tmp_path / "outbox-authority-evidence.db"
    storage = SQLiteStorage(str(db_path))
    try:
        await storage.initialize()
        await storage.append(_event(event_id))
        item = DeliveryOutboxItem(
            outbox_id="obox-authority",
            event_id=event_id,
            route_id="route-append-order",
            delivery_plan_id="plan-append-order",
            target_adapter="meshtastic-main",
            target_channel="0",
            status="in_progress",
            worker_id="pipeline-authority",
        )
        await storage.create_outbox_item(item)
        committed = _receipt(
            receipt_id="receipt-authoritative",
            event_id=event_id,
            status="sent",
            attempt_number=1,
            outbox_id=item.outbox_id,
        )
        await storage.append_receipt(committed)
        assert await storage.mark_outbox_sent(
            item.outbox_id,
            receipt_id=committed.receipt_id,
            attempt_number=1,
            expected_worker_id="pipeline-authority",
        )
        await storage.append_receipt(
            _receipt(
                receipt_id="receipt-late-rejected",
                event_id=event_id,
                status="failed",
                attempt_number=1,
                failure_kind="adapter_transient",
                error="late stale failure",
                outbox_id=item.outbox_id,
            )
        )
    finally:
        await storage.close()

    report = await collect_evidence_bundle(
        storage_path=str(db_path),
        event_id=event_id,
    )
    summary = report["sections"]["storage"]["data"]["incident_summary"]
    entry = next(iter(summary["delivery_state_by_target"].values()))

    assert entry["status"] == "sent"
    assert entry["attempt_number"] == 1
