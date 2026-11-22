"""A late ACK settles an uncertain handoff through the reconciliation pass.

Driving the pre-dispatch evidence recheck over an upgraded populated
predecessor: the seeded ``retry_wait`` row carries the current
``uncertain_native_handoff`` detail, the appended message-level delivered
observation proves provider acceptance, and the reconciliation entrypoint
settles the exact attempt ``sent`` without a new dispatch attempt and
without fabricating a receipt.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryObservation
from medre.core.evidence.handoff_resolution import (
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
)
from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryPolicy

_NOW = "2026-11-20T00:00:00+00:00"
_LATE_ACK_AT = datetime(2026, 11, 20, 0, 5, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"


def _build_predecessor(path: Path) -> None:
    ddl = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(ddl).hexdigest() == _FIXTURE_SHA256
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl.decode("utf-8"))
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('event-1', 'message.text', 1, ?, 'radio', 'radio', ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO durable_ingress_work (event_id, provenance, status,"
            " attempts, created_at, updated_at)"
            " VALUES ('event-1', 'radio', 'completed', 1, ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, status, created_at,"
            " updated_at) VALUES ('work-1', 'event-1', 'plan', 'radio', 1,"
            " 'retry_wait', ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt,"
            " field, diagnostic, created_at) VALUES ('outbox', 'work-1', 1,"
            " 'failure_kind_detail', ?, ?)",
            (UNCERTAIN_NATIVE_HANDOFF_DETAIL, _NOW),
        )


async def test_late_ack_reconciles_uncertain_handoff_to_sent(tmp_path: Path) -> None:
    path = tmp_path / "predecessor.sqlite"
    _build_predecessor(path)
    with closing(sync_open(str(path))):
        pass
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    try:
        item = await storage.get_outbox_item("work-1")
        assert item is not None
        assert item.status == "retry_wait"
        assert item.active_attempt is None
        assert item.attempt_number == 1
        assert item.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL

        observation = DeliveryObservation(
            observation_id="obs-late-ack",
            event_id="event-1",
            delivery_plan_id="plan",
            target_adapter="radio",
            target_channel=None,
            native_channel_id=None,
            outbox_id="work-1",
            attempt_number=1,
            adapter_message_id="native-ack-1",
            state="delivered",
            confirmation_level="end_to_end",
            error=None,
            metadata={},
            observed_at=_LATE_ACK_AT,
        )
        assert await storage.append_delivery_observation(observation) is True

        # Make the retry_wait row due and reclaim it the way a fresh poll
        # does; the pre-dispatch recheck runs on the claimed, unreserved row.
        past = "2026-11-19T00:00:00+00:00"
        await storage._write_rowcount(
            "UPDATE delivery_outbox SET lease_until = ?, next_attempt_at = ?"
            " WHERE outbox_id = 'work-1'",
            (past, past),
        )
        claimed = [
            row
            for row in await storage.claim_due_outbox_items(
                now=datetime.now(UTC).isoformat(), worker_id="reconciler-a"
            )
            if row.outbox_id == "work-1"
        ]
        assert claimed
        claimed_item = claimed[0]

        policy = RetryPolicy(
            max_attempts=3,
            backoff_base=2.0,
            max_delay_seconds=60.0,
            jitter=False,
        )
        outcome = await DeliveryLifecycleService().recheck_last_uncertain_handoff(
            storage,
            claimed_item,
            policy,
            handoff_resolver=EvidenceBackedHandoffResolver(storage),
        )

        assert outcome is not None
        assert outcome.outcome == "accepted"
        assert outcome.receipt_id is None
        assert outcome.attempt_number == 1

        settled = await storage.get_outbox_item("work-1")
        assert settled is not None
        assert settled.status == "sent"
        assert settled.attempt_number == 1
        assert settled.receipt_id is None
        assert settled.active_attempt is None

        rows = await storage.list_delivery_observations_for_outbox("work-1")
        assert [row.observation_id for row in rows] == ["obs-late-ack"]
    finally:
        await storage.close()
