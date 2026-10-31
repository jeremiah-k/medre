"""Delivery-evidence diagnostic content is separated at write time.

Free-form diagnostic text (receipt ``error``/``capability_reason``/
``rendering_evidence``, outbox ``failure_kind_detail``/``error_summary``,
observation ``error``, ingress ``last_error``) has a content lifetime, so
every new parent write stores it only in the append-only
``delivery_evidence_diagnostics`` side table, inside the parent row's
transaction, and leaves the legacy inline columns ``NULL``.  Reads coalesce:
the historical inline value wins when non-empty, otherwise the side-table
value is surfaced.  Historical inline values are never rewritten.

The tests also pin the canonical-separation fact this vertical builds on: an
ordinary nonradio text event keeps no inventoried content in
``canonical_events.payload`` or ``canonical_events.metadata``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.events import DeliveryObservation, DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.schema import _DIAGNOSTICS_DDL, _SCHEMA
from tests.helpers.storage import make_storage_event


def _receipt(
    receipt_id: str,
    event_id: str,
    *,
    status: str = "failed",
    failure_kind: str | None = "adapter_transient",
    error: str | None = None,
    capability_reason: str | None = None,
    rendering_evidence: str | None = None,
    receipt_kind: str = "attempt",
    outbox_id: str | None = None,
    attempt_number: int = 1,
    source: str = "live",
    replay_run_id: str | None = None,
    next_retry_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-dx",
        target_adapter="fake_transport",
        target_channel=None,
        route_id="route-dx",
        status=status,
        receipt_kind=receipt_kind,
        error=error,
        failure_kind=failure_kind,
        capability_reason=capability_reason,
        rendering_evidence=rendering_evidence,
        attempt_number=attempt_number,
        outbox_id=outbox_id,
        source=source,
        replay_run_id=replay_run_id,
        next_retry_at=next_retry_at,
        created_at=datetime.now(timezone.utc),
    )


def _outbox_item(
    outbox_id: str,
    event_id: str,
    *,
    status: str = "in_progress",
    attempt_number: int = 1,
    failure_kind_detail: str | None = None,
    error_summary: str | None = None,
    metadata: dict | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-dx",
        delivery_plan_id="plan-dx",
        target_adapter="fake_transport",
        target_channel="ch-dx",
        attempt_number=attempt_number,
        status=status,
        failure_kind_detail=failure_kind_detail,
        error_summary=error_summary,
        metadata=metadata,
    )


def _observation(
    observation_id: str,
    outbox_id: str,
    event_id: str,
    *,
    state: str = "failed",
    error: str | None = None,
) -> DeliveryObservation:
    return DeliveryObservation(
        observation_id=observation_id,
        event_id=event_id,
        delivery_plan_id="plan-dx",
        target_adapter="fake_transport",
        target_channel="ch-dx",
        outbox_id=outbox_id,
        attempt_number=1,
        state=state,
        confirmation_level="remote_service",
        error=error,
    )


async def _diag_rows(store, family, row_id):
    return await store._read_all(
        "SELECT field, diagnostic, attempt FROM delivery_evidence_diagnostics "
        "WHERE family = ? AND row_id = ? ORDER BY diag_id",
        (family, row_id),
    )


async def _admit(store, event_id):
    """Admit a minimal opaque canonical event for evidence foreign keys."""
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={}, metadata={}
    )
    await store.admit_ingress(event, None, "live")


# ---------------------------------------------------------------------------
# Canonical separation pin (PR B item 1)
# ---------------------------------------------------------------------------


async def test_ordinary_text_event_keeps_no_inline_canonical_content(temp_storage):
    """Admission already separates ordinary body text from the canonical row.

    Both the payload and metadata envelope JSON stay exactly ``{}`` — the
    ``opaque_envelope`` probe's definition of empty — while the text lives
    under content ownership in ``event_body_content``.
    """
    secret = "PLAIN-ORDINARY-BODY"
    event = msgspec.structs.replace(
        make_storage_event(event_id="ordinary"),
        payload={"body": secret, "text": secret},
        metadata={},
    )
    await temp_storage.admit_ingress(event, None, "live")
    row = await temp_storage._read_one(
        "SELECT payload, metadata FROM canonical_events WHERE event_id='ordinary'"
    )
    assert row["payload"] == "{}"
    assert row["metadata"] == "{}"
    bodies = await temp_storage._read_all(
        "SELECT field FROM event_body_content WHERE event_id='ordinary' ORDER BY field"
    )
    assert [b["field"] for b in bodies] == ["body", "text"]
    report = await temp_storage.assess_event_content_release("ordinary")
    assert "opaque_envelope" not in report["blockers"]
    assert secret not in json.dumps(report)


# ---------------------------------------------------------------------------
# Receipt write-time separation and coalescing reads
# ---------------------------------------------------------------------------


async def test_receipt_diagnostics_are_written_only_to_the_side_table(temp_storage):
    await _admit(temp_storage, "evt-dx")
    receipt = _receipt(
        "rcpt-dx",
        "evt-dx",
        error="ADAPTER-BLOW-UP",
        capability_reason="CAPABILITY-REASON",
        rendering_evidence='{"strategy":"direct"}',
    )
    await temp_storage.append_receipt(receipt)
    row = await temp_storage._read_one(
        "SELECT error, capability_reason, rendering_evidence, failure_kind "
        "FROM delivery_receipts WHERE receipt_id='rcpt-dx'"
    )
    assert row["error"] is None
    assert row["capability_reason"] is None
    assert row["rendering_evidence"] is None
    # Durable facts stay inline.
    assert row["failure_kind"] == "adapter_transient"
    rows = await _diag_rows(temp_storage, "receipt", "rcpt-dx")
    assert {(r["field"], r["diagnostic"]) for r in rows} == {
        ("error", "ADAPTER-BLOW-UP"),
        ("capability_reason", "CAPABILITY-REASON"),
        ("rendering_evidence", '{"strategy":"direct"}'),
    }

    # Every ordinary read path coalesces the separated text.
    history = await temp_storage.list_receipts_for_event("evt-dx")
    assert [r.error for r in history] == ["ADAPTER-BLOW-UP"]
    assert [r.capability_reason for r in history] == ["CAPABILITY-REASON"]
    assert [r.rendering_evidence for r in history] == ['{"strategy":"direct"}']

    metadata_only = await temp_storage.list_receipts_for_event(
        "evt-dx", metadata_only=True
    )
    assert [r.error for r in metadata_only] == [None]
    assert "ADAPTER-BLOW-UP" not in msgspec.json.encode(metadata_only).decode()


async def test_successful_receipt_writes_no_diagnostic_content_anywhere(temp_storage):
    """A fully successful receipt leaves no diagnostic content anywhere."""
    await _admit(temp_storage, "evt-sent")
    receipt = _receipt("rcpt-sent", "evt-sent", status="sent", failure_kind=None)
    await temp_storage.append_receipt(receipt)
    inline = await temp_storage._read_one(
        "SELECT error, capability_reason, rendering_evidence "
        "FROM delivery_receipts WHERE receipt_id='rcpt-sent'"
    )
    assert tuple(inline.values()) == (None, None, None)
    assert await _diag_rows(temp_storage, "receipt", "rcpt-sent") == []
    report = await temp_storage.inspect_content_inventory()
    diagnostics = next(
        item
        for item in report["surfaces"]
        if item["table"] == "delivery_evidence_diagnostics"
    )
    assert diagnostics["rows"] == 0


async def test_historical_inline_diagnostics_win_over_side_table(temp_storage):
    """A legacy inline value is surfaced unchanged and never rewritten."""
    await _admit(temp_storage, "evt-legacy")
    await temp_storage._write(
        "INSERT INTO delivery_receipts "
        "(receipt_id, event_id, delivery_plan_id, target_adapter, status, "
        "receipt_kind, error, created_at) "
        "VALUES ('rcpt-legacy', 'evt-legacy', 'plan-dx', 'fake_transport', "
        "'failed', 'attempt', 'HISTORICAL-INLINE', '2026-10-30T00:00:00+00:00')"
    )
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics "
        "(family, row_id, attempt, field, diagnostic, created_at) "
        "VALUES ('receipt', 'rcpt-legacy', 1, 'error', 'SIDE-TABLE', '2026-10-31')"
    )
    history = await temp_storage.list_receipts_for_event("evt-legacy")
    assert [r.error for r in history] == ["HISTORICAL-INLINE"]
    row = await temp_storage._read_one(
        "SELECT error FROM delivery_receipts WHERE receipt_id='rcpt-legacy'"
    )
    assert row["error"] == "HISTORICAL-INLINE"


async def test_receipt_diagnostics_commit_atomically_with_the_receipt(temp_storage):
    """A failed diagnostic write rolls the receipt back — and vice versa."""
    await _admit(temp_storage, "evt-atomic")
    receipt = _receipt("rcpt-atomic", "evt-atomic", error="BOOM")
    await temp_storage._write(
        "CREATE TRIGGER fail_diag BEFORE INSERT ON delivery_evidence_diagnostics "
        "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(Exception, match="Database write failed"):
        await temp_storage.append_receipt(receipt)
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_receipts WHERE receipt_id='rcpt-atomic'"
        )
    ) is None
    await temp_storage._write("DROP TRIGGER fail_diag")
    await temp_storage.append_receipt(receipt)
    assert await _diag_rows(temp_storage, "receipt", "rcpt-atomic")

    mirrored = _receipt("rcpt-atomic-2", "evt-atomic", error="BOOM")
    await temp_storage._write(
        "CREATE TRIGGER fail_receipt BEFORE INSERT ON delivery_receipts "
        "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(Exception, match="Database write failed"):
        await temp_storage.append_receipt(mirrored)
    assert await _diag_rows(temp_storage, "receipt", "rcpt-atomic-2") == []
    await temp_storage._write("DROP TRIGGER fail_receipt")


async def test_receipt_diagnostics_are_listed_by_identity_and_replay_run(temp_storage):
    await _admit(temp_storage, "evt-run")
    await temp_storage.append_receipt(
        _receipt("rcpt-ob", "evt-run", error="OB-ERROR", outbox_id="ob-run")
    )
    await temp_storage.append_receipt(
        _receipt(
            "rcpt-replay",
            "evt-run",
            error="REPLAY-ERROR",
            source="replay",
            replay_run_id="run-1",
        )
    )
    await temp_storage.append_receipt(
        _receipt(
            "rcpt-due",
            "evt-run",
            error="DUE-ERROR",
            next_retry_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    by_outbox = await temp_storage.list_receipts_for_outbox("ob-run")
    assert [r.error for r in by_outbox] == ["OB-ERROR"]
    by_replay = await temp_storage.list_receipts_by_replay_run("run-1")
    assert [r.error for r in by_replay] == ["REPLAY-ERROR"]
    due = await temp_storage.list_due_retry_receipts(
        datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    assert "DUE-ERROR" in [r.error for r in due]
    current = await temp_storage.list_receipts_for_event("evt-run")
    assert {r.error for r in current} == {"OB-ERROR", "REPLAY-ERROR", "DUE-ERROR"}


async def test_outbox_current_attempt_diagnostic_supersedes_legacy_inline(temp_storage):
    await _admit_outbox(temp_storage, "ob-new", "evt-new")
    await temp_storage._write(
        "UPDATE delivery_outbox SET error_summary='OLD-INLINE' WHERE outbox_id='ob-new'"
    )
    assert await temp_storage.mark_outbox_retry_wait(
        "ob-new",
        "2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
        error_summary="NEW-SUMMARY",
    )
    current = await temp_storage.get_outbox_item("ob-new")
    assert current is not None and current.error_summary == "NEW-SUMMARY"
    inline = await temp_storage._read_one(
        "SELECT error_summary FROM delivery_outbox WHERE outbox_id='ob-new'"
    )
    assert inline["error_summary"] == "OLD-INLINE"


async def test_diagnostic_coalescing_chunks_large_identity_lists():
    from medre.core.storage.sqlite._evidence_diagnostics import (
        _EvidenceDiagnosticsReadMixin,
    )

    class _Fake(_EvidenceDiagnosticsReadMixin):
        calls: list[int]

        def __init__(self):
            self.calls = []

        async def _read_all(self, sql, params=()):
            self.calls.append(len(params))
            assert len(params) <= 501
            return [
                {
                    "diag_id": int(row_id.split("-")[1]) + 1,
                    "row_id": row_id,
                    "field": "error",
                    "attempt": 1,
                    "diagnostic": row_id,
                }
                for row_id in params[1:]
            ]

    rows = [
        {
            "receipt_id": f"receipt-{i}",
            "error": None,
            "capability_reason": None,
            "rendering_evidence": None,
        }
        for i in range(1200)
    ]
    fake = _Fake()
    await fake._coalesce_evidence_diagnostics(
        "receipt", rows, identity_column="receipt_id"
    )
    assert fake.calls == [501, 501, 201]
    assert [r["error"] for r in rows] == [f"receipt-{i}" for i in range(1200)]


# ---------------------------------------------------------------------------
# Outbox write-time separation and attempt-scoped coalescing
# ---------------------------------------------------------------------------


async def _admit_outbox(store, outbox_id, event_id, **kwargs):
    await _admit(store, event_id)
    created = await store.create_outbox_item(
        _outbox_item(outbox_id, event_id, **kwargs)
    )
    await store._write(
        "UPDATE event_content_dependencies SET released_at='2026-10-31' "
        "WHERE event_id=? AND owner_kind='outbox' AND released_at IS NULL",
        (event_id,),
    )
    return created


async def test_outbox_retry_wait_separates_diagnostics_at_write_time(temp_storage):
    await _admit_outbox(temp_storage, "ob-dx", "evt-ob")
    await temp_storage.mark_outbox_retry_wait(
        "ob-dx",
        "2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
        failure_kind_detail="DISPATCH-DETAIL",
        error_summary="DISPATCH-SUMMARY",
    )
    row = await temp_storage._read_one(
        "SELECT failure_kind, failure_kind_detail, error_summary, attempt_number "
        "FROM delivery_outbox WHERE outbox_id='ob-dx'"
    )
    # The durable failure fact stays inline; the free-form text does not.
    assert row["failure_kind"] == "adapter_transient"
    assert row["failure_kind_detail"] is None
    assert row["error_summary"] is None
    assert row["attempt_number"] == 1
    rows = await _diag_rows(temp_storage, "outbox", "ob-dx")
    assert {(r["field"], r["diagnostic"], r["attempt"]) for r in rows} == {
        ("failure_kind_detail", "DISPATCH-DETAIL", 1),
        ("error_summary", "DISPATCH-SUMMARY", 1),
    }
    item = await temp_storage.get_outbox_item("ob-dx")
    assert item is not None
    assert item.failure_kind_detail == "DISPATCH-DETAIL"
    assert item.error_summary == "DISPATCH-SUMMARY"
    items = await temp_storage.list_outbox_items_for_event("evt-ob")
    assert [i.error_summary for i in items] == ["DISPATCH-SUMMARY"]


async def test_outbox_success_at_a_newer_attempt_no_longer_surfaces_the_failure(
    temp_storage,
):
    """Attempt-scoped coalescing: a settled attempt reads clean."""
    await _admit_outbox(temp_storage, "ob-settle", "evt-settle")
    await temp_storage.mark_outbox_retry_wait(
        "ob-settle",
        "2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
        failure_kind_detail="ATTEMPT-ONE-DETAIL",
        error_summary="ATTEMPT-ONE-SUMMARY",
    )
    claimed = await temp_storage.claim_due_outbox_items(
        "2026-11-01T00:00:00+00:00", "worker-settle"
    )
    assert [c.outbox_id for c in claimed] == ["ob-settle"]
    await temp_storage.mark_outbox_sent("ob-settle", attempt_number=2)
    item = await temp_storage.get_outbox_item("ob-settle")
    assert item is not None
    assert item.status == "sent"
    assert item.attempt_number == 2
    assert item.failure_kind_detail is None
    assert item.error_summary is None
    # The superseded diagnostics remain appended under their own attempt.
    rows = await _diag_rows(temp_storage, "outbox", "ob-settle")
    assert {r["attempt"] for r in rows} == {1}


async def test_outbox_terminal_failure_surfaces_its_own_attempt(temp_storage):
    await _admit_outbox(temp_storage, "ob-dead", "evt-dead")
    await temp_storage.mark_outbox_retry_wait(
        "ob-dead",
        "2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
        error_summary="FIRST-SUMMARY",
    )
    await temp_storage.mark_outbox_dead_lettered(
        "ob-dead",
        failure_kind="adapter_permanent",
        error_summary="TERMINAL-SUMMARY",
        attempt_number=2,
    )
    item = await temp_storage.get_outbox_item("ob-dead")
    assert item is not None
    assert item.status == "dead_lettered"
    assert item.error_summary == "TERMINAL-SUMMARY"
    assert item.failure_kind_detail is None
    rows = await _diag_rows(temp_storage, "outbox", "ob-dead")
    assert {(r["field"], r["diagnostic"], r["attempt"]) for r in rows} == {
        ("error_summary", "FIRST-SUMMARY", 1),
        ("error_summary", "TERMINAL-SUMMARY", 2),
    }


async def test_outbox_creation_separates_seeded_diagnostics(temp_storage):
    await _admit_outbox(
        temp_storage,
        "ob-seeded",
        "evt-seeded",
        failure_kind_detail="SEEDED-DETAIL",
        error_summary="SEEDED-SUMMARY",
        metadata={"provider": {"opaque": "META"}},
    )
    row = await temp_storage._read_one(
        "SELECT failure_kind_detail, error_summary, metadata, target_channel, "
        "target_address FROM delivery_outbox WHERE outbox_id='ob-seeded'"
    )
    assert row["failure_kind_detail"] is None
    assert row["error_summary"] is None
    # Structured provider metadata and route correlation stay inline.
    assert '"opaque":"META"' in row["metadata"]
    assert row["target_channel"] == "ch-dx"
    rows = await _diag_rows(temp_storage, "outbox", "ob-seeded")
    assert {(r["field"], r["diagnostic"]) for r in rows} == {
        ("failure_kind_detail", "SEEDED-DETAIL"),
        ("error_summary", "SEEDED-SUMMARY"),
    }
    item = await temp_storage.get_outbox_item("ob-seeded")
    assert item is not None
    assert item.failure_kind_detail == "SEEDED-DETAIL"
    assert item.error_summary == "SEEDED-SUMMARY"


async def test_outbox_transition_diagnostics_commit_atomically(temp_storage):
    await _admit_outbox(temp_storage, "ob-atomic", "evt-ob-atomic")
    await temp_storage._write(
        "CREATE TRIGGER fail_outbox_diag BEFORE INSERT ON "
        "delivery_evidence_diagnostics BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(Exception, match="Database write failed"):
        await temp_storage.mark_outbox_retry_wait(
            "ob-atomic",
            "2026-10-31T01:00:00+00:00",
            failure_kind="adapter_transient",
            error_summary="LOST-SUMMARY",
        )
    row = await temp_storage._read_one(
        "SELECT status FROM delivery_outbox WHERE outbox_id='ob-atomic'"
    )
    assert row["status"] == "in_progress"
    await temp_storage._write("DROP TRIGGER fail_outbox_diag")


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


async def test_observation_error_is_separated_and_coalesced(temp_storage):
    await _admit_outbox(temp_storage, "ob-obs", "evt-obs")
    inserted = await temp_storage.append_delivery_observation(
        _observation("obs-dx", "ob-obs", "evt-obs", error="OBSERVATION-ERROR")
    )
    assert inserted is True
    row = await temp_storage._read_one(
        "SELECT error, state FROM delivery_observations WHERE observation_id='obs-dx'"
    )
    assert row["error"] is None
    assert row["state"] == "failed"
    rows = await _diag_rows(temp_storage, "observation", "obs-dx")
    assert [(r["field"], r["diagnostic"]) for r in rows] == [
        ("error", "OBSERVATION-ERROR")
    ]
    history = await temp_storage.list_delivery_observations_for_event("evt-obs")
    assert [o.error for o in history] == ["OBSERVATION-ERROR"]
    clean = await temp_storage.list_delivery_observations_for_event(
        "evt-obs", metadata_only=True
    )
    assert [o.error for o in clean] == [None]
    assert "OBSERVATION-ERROR" not in msgspec.json.encode(clean).decode()


async def test_observation_without_error_writes_no_diagnostics(temp_storage):
    await _admit_outbox(temp_storage, "ob-obs-ok", "evt-obs-ok")
    inserted = await temp_storage.append_delivery_observation(
        _observation(
            "obs-ok",
            "ob-obs-ok",
            "evt-obs-ok",
            state="delivered",
            error=None,
        )
    )
    assert inserted is True
    assert await _diag_rows(temp_storage, "observation", "obs-ok") == []


async def test_observation_rejection_commits_no_diagnostics(temp_storage):
    """A duplicate observation ID lands neither parent row nor diagnostics."""
    await _admit_outbox(temp_storage, "ob-dup", "evt-dup")
    observation = _observation("obs-dup", "ob-dup", "evt-dup", error="DUP-ERROR")
    assert await temp_storage.append_delivery_observation(observation) is True
    assert await temp_storage.append_delivery_observation(observation) is False
    rows = await _diag_rows(temp_storage, "observation", "obs-dup")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Ingress work
# ---------------------------------------------------------------------------


async def test_ingress_last_error_is_separated_and_claimed_with_it(temp_storage):
    await _admit(temp_storage, "evt-ingress")
    claim = await temp_storage.claim_ingress_work(worker_id="worker-1", limit=1)
    assert [c.event_id for c in claim] == ["evt-ingress"]
    released = await temp_storage.release_ingress_work(
        "evt-ingress", worker_id="worker-1", error="PROCESSING-FAILED"
    )
    assert released is True
    row = await temp_storage._read_one(
        "SELECT last_error, status FROM durable_ingress_work WHERE event_id='evt-ingress'"
    )
    assert row["last_error"] is None
    assert row["status"] == "pending"
    rows = await _diag_rows(temp_storage, "ingress", "evt-ingress")
    assert [(r["field"], r["diagnostic"]) for r in rows] == [
        ("last_error", "PROCESSING-FAILED")
    ]
    reclaimed = await temp_storage.claim_ingress_work(worker_id="worker-2", limit=1)
    assert [c.last_error for c in reclaimed] == ["PROCESSING-FAILED"]

    failed = await temp_storage.fail_ingress_work(
        "evt-ingress", worker_id="worker-2", error="TERMINAL-FAILED"
    )
    assert failed is True
    rows = await _diag_rows(temp_storage, "ingress", "evt-ingress")
    assert [r["diagnostic"] for r in rows] == [
        "PROCESSING-FAILED",
        "TERMINAL-FAILED",
    ]
    assert (await temp_storage.claim_ingress_work(worker_id="worker-3", limit=5)) == []


async def test_ingress_diagnostics_commit_atomically_with_the_transition(temp_storage):
    await _admit(temp_storage, "evt-ingress-atomic")
    await temp_storage.claim_ingress_work(worker_id="worker-1", limit=1)
    await temp_storage._write(
        "CREATE TRIGGER fail_ingress_diag BEFORE INSERT ON "
        "delivery_evidence_diagnostics BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(Exception, match="Database write failed"):
        await temp_storage.release_ingress_work(
            "evt-ingress-atomic", worker_id="worker-1", error="LOST"
        )
    row = await temp_storage._read_one(
        "SELECT status FROM durable_ingress_work WHERE event_id='evt-ingress-atomic'"
    )
    assert row["status"] == "processing"
    await temp_storage._write("DROP TRIGGER fail_ingress_diag")


# ---------------------------------------------------------------------------
# DDL authority
# ---------------------------------------------------------------------------


def test_diagnostics_ddl_has_one_authoritative_definition():
    assert (
        _SCHEMA.count("CREATE TABLE IF NOT EXISTS delivery_evidence_diagnostics") == 1
    )
    assert _DIAGNOSTICS_DDL in _SCHEMA
    assert "family TEXT NOT NULL" in _DIAGNOSTICS_DDL


async def test_same_attempt_success_does_not_resurrect_cleared_diagnostics(
    temp_storage,
):
    await _admit_outbox(
        temp_storage,
        "ob-same-success",
        "evt-same-success",
        failure_kind_detail="OLD-DETAIL",
        error_summary="OLD-SUMMARY",
    )
    assert await temp_storage.mark_outbox_sent("ob-same-success", attempt_number=1)
    item = await temp_storage.get_outbox_item("ob-same-success")
    assert item is not None and item.status == "sent"
    assert item.error_summary is None
    assert item.failure_kind_detail is None
    assert await _diag_rows(temp_storage, "outbox", "ob-same-success")


async def test_new_replay_generation_diagnostics_follow_allocated_attempt(temp_storage):
    await _admit_outbox(temp_storage, "ob-origin", "evt-replay-diag")
    seeded = _outbox_item(
        "ob-replay",
        "evt-replay-diag",
        failure_kind_detail="GEN-DETAIL",
        error_summary="GEN-SUMMARY",
    )
    replay = replace(seeded, dispatch_source="replay")
    created = await temp_storage.create_outbox_item(
        replay, allocate_new_generation=True
    )
    assert created.attempt_number == 2
    assert created.error_summary == "GEN-SUMMARY"
    assert created.failure_kind_detail == "GEN-DETAIL"
    rows = await _diag_rows(temp_storage, "outbox", "ob-replay")
    assert {row["attempt"] for row in rows} == {2}


async def test_queued_handoff_clears_same_attempt_diagnostics(temp_storage):
    await _admit_outbox(
        temp_storage,
        "ob-same-queued",
        "evt-same-queued",
        failure_kind_detail="EARLY-DETAIL",
        error_summary="EARLY-SUMMARY",
    )
    assert await temp_storage.mark_outbox_queued("ob-same-queued", attempt_number=1)
    item = await temp_storage.get_outbox_item("ob-same-queued")
    assert item is not None and item.status == "queued"
    assert item.failure_kind_detail is None
    assert item.error_summary is None
    assert await _diag_rows(temp_storage, "outbox", "ob-same-queued")


async def test_historical_inline_outbox_diagnostic_is_not_rewritten(temp_storage):
    await _admit_outbox(temp_storage, "ob-legacy-sent", "evt-legacy-sent")
    assert await temp_storage.mark_outbox_sent("ob-legacy-sent", attempt_number=1)
    await temp_storage._write(
        "UPDATE delivery_outbox SET error_summary='INLINE-HISTORY' "
        "WHERE outbox_id='ob-legacy-sent'"
    )
    item = await temp_storage.get_outbox_item("ob-legacy-sent")
    assert item is not None and item.error_summary == "INLINE-HISTORY"


async def test_delivery_status_typed_view_coalesces_separated_error(temp_storage):
    from medre.core.delivery_authority import DeliveryIdentity

    await _admit(temp_storage, "evt-status")
    await temp_storage.append_receipt(
        _receipt("rcpt-status", "evt-status", error="STATUS-ERROR")
    )
    identity = DeliveryIdentity("evt-status", "plan-dx", "fake_transport", None)
    current = await temp_storage.delivery_status(identity)
    assert current is not None and current.error == "STATUS-ERROR"
    # The SQL view is deliberately a facts-only projection. Its persisted
    # legacy column is NULL; typed consumers must rehydrate separately.
    raw = await temp_storage._read_one(
        "SELECT error FROM delivery_status WHERE receipt_id='rcpt-status'"
    )
    assert raw is not None and raw["error"] is None
