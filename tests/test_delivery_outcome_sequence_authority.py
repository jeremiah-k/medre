"""Cross-surface current-outcome regressions for receipt append ordering."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
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
