# medre-test-tier: compatibility
"""End-to-end hard content-privacy ceiling proof with the operator audit.

Drives the full lifecycle against the real ``SQLiteStorage`` backend:
content-bearing events (bodies, relations and excerpts including one event
quoting another, attachments sharing one content-addressed blob, dispatch
contexts, radio progress, receipt diagnostics), ceiling expiry, the guarded
enforcement sweep, and then the read-only operator audit surfaces:

- ``audit_content_ceilings`` / ``medre storage ceiling audit`` walk the
  residual battery for elapsed ceilings and report per-store residuals
  without purging anything, flagging the documented late-write case.
- ``verify_content_ceiling_receipts`` / ``medre storage ceiling verify``
  re-run the battery over the enforcement receipts and report pass/fail
  per receipt with the receipt's counters.

Assertions: zero residuals per store after the sweep, receipt counters
matching the sweep summaries, content unreachable through every read path
(including the quoting event's excerpt of the expired event), shared blob
bytes surviving for their other referent until the last reference goes,
late-write residuals flagged by the audit and cleared by the next sweep,
stable idempotent audit re-runs, strictly read-only CLI behavior, and
scriptable exit codes. Privacy placeholders only: every seeded value is
synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medre.cli.exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_FAILED
from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item

MARKER = "CEILING-AUDIT-E2E-BODY"
_QUOTE = "quoted synthetic excerpt text"
_FINGERPRINT = "ab" * 32
_BLOB_REF = "sha256:" + "ef" * 32


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _snapshot(context: dict) -> OutboxDispatchSnapshotWrite:
    """Freeze a direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": "native",
            "capability_field": None,
            "capability_reason": None,
            **context,
        },
    )


async def _admit_with_ceiling(
    store: SQLiteStorage, event_id: str, *, seconds: int = 3600
) -> None:
    """Admit one event with a body through the real ceiling producer.

    A negative *seconds* installs an already-elapsed ceiling for the
    real-time CLI scenarios.
    """
    await store.admit_ingress(
        make_storage_event(event_id=event_id, payload={"body": MARKER}),
        None,
        "live",
        content_deadline=_future(seconds),
    )


async def _admit_plain(store: SQLiteStorage, event_id: str) -> None:
    """Admit one event without a ceiling (a quoting neighbor)."""
    await store.admit_ingress(
        make_storage_event(event_id=event_id, payload={"text": "neighbor"}),
        None,
        "live",
    )


async def _read_ceiling(store: SQLiteStorage, event_id: str) -> datetime:
    row = await store._read_one(
        "SELECT hard_ceiling FROM event_content_access_policies WHERE event_id=?",
        (event_id,),
    )
    assert row is not None
    return datetime.fromisoformat(str(row["hard_ceiling"]))


async def _seed_every_copy(store: SQLiteStorage, event_id: str) -> str:
    """Seed one content-potential value in every enumerated store.

    Returns the seeded outbox identity. Every seeded value is synthetic.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "UPDATE canonical_events SET payload=?, metadata=? WHERE event_id=?",
        ('{"opaque":"envelope-note"}', '{"custom":{"note":"opaque"}}', event_id),
    )
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', NULL, 'opaque-key', ?, ?)",
        (event_id, '{"relation":"opaque"}', now),
    )
    relation = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=?", (event_id,)
    )
    await store._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) VALUES (?, ?)",
        (relation["id"], _QUOTE),
    )
    await store._write(
        "INSERT INTO native_message_refs (id, event_id, adapter, native_message_id, "
        "direction, metadata, created_at) VALUES (?, ?, 'source', 'nm-1', "
        "'inbound', ?, ?)",
        (f"nref-{event_id}", event_id, '{"native":"opaque"}', now),
    )
    item = make_outbox_item(
        delivery_plan_id=f"plan-{event_id}", event_id=event_id, target_adapter="radio"
    )
    created = await store.create_outbox_item(
        item,
        dispatch_snapshot=_snapshot({"deadline": _future(7200).isoformat()}),
    )
    outbox_id = created.outbox_id
    await store._write(
        "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
        ('{"inline":"dispatch-context"}', outbox_id),
    )
    await store._write(
        "INSERT INTO delivery_receipts (receipt_id, event_id, delivery_plan_id, "
        "target_adapter, status, receipt_kind, source, created_at) "
        "VALUES (?, ?, ?, 'radio', 'queued', 'attempt', 'live', ?)",
        (f"rcpt-{event_id}", event_id, f"plan-{event_id}", now),
    )
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('receipt', ?, 1, 'error', ?, ?)",
        (f"rcpt-{event_id}", f"failure detail {MARKER}", now),
    )
    await store._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES (?, ?, ?, 'radio', ?, 1, 'delivered', "
        "?, ?)",
        (
            f"obs-{event_id}",
            event_id,
            f"plan-{event_id}",
            outbox_id,
            '{"provider":"opaque"}',
            now,
        ),
    )
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('observation', ?, 1, "
        "'last_error', ?, ?)",
        (f"obs-{event_id}", f"late failure {MARKER}", now),
    )
    await store._write(
        "INSERT INTO radio_attempt_progress (outbox_id, attempt_number, "
        "plan_fingerprint, plan_json, provenance_json, parts_json, revision, "
        "created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?)",
        (
            outbox_id,
            _FINGERPRINT,
            '{"plan":"synthetic"}',
            '{"provenance":"synthetic"}',
            f'{{"parts":["{MARKER}"]}}',
            now,
            now,
        ),
    )
    return outbox_id


async def _insert_blob(store: SQLiteStorage, content_ref: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "INSERT INTO attachment_blobs (content_ref, size_bytes, media_kind, "
        "mime_type, created_at, data) VALUES (?, 10, 'image', 'image/png', ?, ?)",
        (content_ref, now, b"ATTACHDATA"),
    )


async def _associate_blob(
    store: SQLiteStorage, event_id: str, content_ref: str
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "INSERT INTO event_attachment_associations (event_id, content_ref, "
        "created_at) VALUES (?, ?, ?)",
        (event_id, content_ref, now),
    )


async def _seed_incoming_excerpt(store: SQLiteStorage, event_id: str) -> None:
    """Neighbor event quotes the target event through a relation excerpt."""
    neighbor = f"neighbor-{event_id}"
    await _admit_plain(store, neighbor)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', ?, NULL, '{}', ?)",
        (neighbor, event_id, now),
    )
    relation = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=?", (neighbor,)
    )
    await store._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) VALUES (?, ?)",
        (relation["id"], _QUOTE),
    )


async def _seed_late_write(store: SQLiteStorage, event_id: str, outbox_id: str) -> None:
    """A late completion write for the in-flight generation."""
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES ('obs-late', ?, ?, 'radio', ?, 1, "
        "'delivered', ?, ?)",
        (event_id, f"plan-{event_id}", outbox_id, '{"provider":"late"}', now),
    )
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('observation', 'obs-late', 1, "
        "'error', ?, ?)",
        (f"late {MARKER}", now),
    )


async def _count(store: SQLiteStorage, sql: str, params: tuple = ()) -> int:
    row = await store._read_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


def _run_cli(*argv: str) -> tuple[int, str, str]:
    """Run one CLI invocation, capturing stdout/stderr and the exit code."""
    from medre.cli.main import main

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = 0
        try:
            main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _parse_cli_json(stdout: str) -> dict:
    return json.loads(stdout)


# ---------------------------------------------------------------------------
# The full lifecycle: sweep, audit, verify, read-path denial
# ---------------------------------------------------------------------------


async def test_full_lifecycle_audit_and_verify_prove_purge(
    temp_storage: SQLiteStorage,
) -> None:
    """Ingest, expire, sweep, then prove the purge with the audit surfaces."""
    event_id = "ceiling-e2e-full"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    await _seed_incoming_excerpt(temp_storage, event_id)
    # A second ceiling event shares one content-addressed blob with the
    # target; its own ceiling stays far in the future here so exactly one
    # ceiling elapses for the first sweep.
    sharer = "ceiling-e2e-sharer"
    await _admit_with_ceiling(temp_storage, sharer, seconds=86400)
    await _insert_blob(temp_storage, _BLOB_REF)
    await _associate_blob(temp_storage, event_id, _BLOB_REF)
    await _associate_blob(temp_storage, sharer, _BLOB_REF)

    # Pre-sweep: the audit reports exactly what the enforcement would
    # remove, per store, without purging anything.
    ceiling = await _read_ceiling(temp_storage, event_id)
    pre_audit = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert pre_audit["elapsed_pending_has_more"] is False
    assert pre_audit["residual_enforced_has_more"] is False
    assert pre_audit["receipts"] == 0
    assert len(pre_audit["elapsed_pending"]) == 1
    pending = pre_audit["elapsed_pending"][0]
    assert pending["event_id"] == event_id
    assert pending["residual_stores"] == {
        "event_body_content": 1,
        "event_relation_excerpts": 1,
        "event_relation_excerpts:incoming": 1,
        "canonical_events": 1,
        "event_relations": 1,
        "native_message_refs": 1,
        "delivery_outbox": 1,
        "delivery_observations": 1,
        "delivery_evidence_diagnostics": 2,
        "radio_attempt_progress": 1,
        "delivery_outbox_snapshots": 1,
        "event_attachment_associations": 1,
    }
    # The read-only audit purged nothing: the body is still loadable.
    body_read = await temp_storage.read_body_for_outbox(event_id, outbox_id)
    assert body_read.status == "retained"

    enforced = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert len(enforced) == 1
    summary = enforced[0]
    assert summary.residual_pass is False
    assert summary.blobs_removed == 0  # the shared blob still has its sharer

    # Post-sweep audit: zero residuals anywhere, one receipt.
    post_audit = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=2)
    )
    assert post_audit["elapsed_pending"] == []
    assert post_audit["residual_enforced"] == []
    assert post_audit["receipts"] == 1

    # Post-sweep verification: the receipt re-checks clean with counters
    # that match the sweep summary exactly.
    verification = await temp_storage.verify_content_ceiling_receipts()
    assert verification["verdict"] == "passed"
    assert verification["receipts_examined"] == 1
    assert verification["receipts_failed"] == 0
    receipt = verification["receipts"][0]
    assert receipt["event_id"] == event_id
    assert receipt["status"] == "passed"
    assert receipt["residual_stores"] == {}
    assert receipt["rows_removed"] == summary.rows_removed
    assert receipt["logical_bytes_removed"] == summary.logical_bytes_removed
    assert receipt["blobs_removed"] == 0
    assert receipt["blob_bytes_removed"] == 0

    # Content is unreachable through every read path: the outbox body read
    # and the replay read both deny; the collector accepts the receipt.
    body_read = await temp_storage.read_body_for_outbox(event_id, outbox_id)
    assert body_read.status == "denied"
    replay_read = await temp_storage.read_event_for_replay(event_id)
    assert replay_read.status == "denied"
    collected = await temp_storage.collect_event_content(event_id, dry_run=True)
    assert collected["status"] == "already_collected"

    # The quoting event's excerpt of the expired event is gone through its
    # authorized read path too: the neighbor stays readable, its relation
    # survives as linkage, and no quoted text surfaces.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_relation_excerpts x "
            "JOIN event_relations r ON r.id=x.relation_id "
            "WHERE r.target_event_id=?",
            (event_id,),
        )
        == 0
    )
    neighbor_read = await temp_storage.read_event_for_replay(f"neighbor-{event_id}")
    assert neighbor_read.status == "retained"
    quoted = [
        relation
        for relation in neighbor_read.event.relations
        if relation.target_event_id == event_id
    ]
    assert len(quoted) == 1
    assert quoted[0].fallback_text is None

    # Shared blob bytes survive for their other referent.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 1
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_attachment_associations WHERE event_id=?",
            (sharer,),
        )
        == 1
    )

    # The audit re-run is stable and idempotent for one observation instant.
    repeat = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=2)
    )
    assert repeat == post_audit


async def test_shared_blob_bytes_go_with_last_referent_across_receipts(
    temp_storage: SQLiteStorage,
) -> None:
    """Receipt counters across two sweeps sum the shared bytes exactly once."""
    owner_a = "ceiling-e2e-share-a"
    owner_b = "ceiling-e2e-share-b"
    await _admit_with_ceiling(temp_storage, owner_a)
    # The second referent keeps its ceiling far in the future so the
    # first sweep enforces exactly one receipt.
    await _admit_with_ceiling(temp_storage, owner_b, seconds=86400)
    await _insert_blob(temp_storage, _BLOB_REF)
    await _associate_blob(temp_storage, owner_a, _BLOB_REF)
    await _associate_blob(temp_storage, owner_b, _BLOB_REF)

    ceiling_a = await _read_ceiling(temp_storage, owner_a)
    ceiling_b = await _read_ceiling(temp_storage, owner_b)
    await temp_storage.enforce_content_ceilings(now=ceiling_a + timedelta(seconds=1))
    # The first referent's purge must not take the shared bytes.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 1
    )
    first = await temp_storage.verify_content_ceiling_receipts()
    a_receipt = next(r for r in first["receipts"] if r["event_id"] == owner_a)
    assert a_receipt["blobs_removed"] == 0
    assert a_receipt["blob_bytes_removed"] == 0
    assert first["verdict"] == "passed"

    await temp_storage.enforce_content_ceilings(
        now=max(ceiling_a, ceiling_b) + timedelta(seconds=1)
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 0
    )
    both = await temp_storage.verify_content_ceiling_receipts()
    assert both["receipts_examined"] == 2
    assert both["verdict"] == "passed"
    b_receipt = next(r for r in both["receipts"] if r["event_id"] == owner_b)
    assert b_receipt["blobs_removed"] == 1
    assert b_receipt["blob_bytes_removed"] == 10
    audit = await temp_storage.audit_content_ceilings()
    assert audit["elapsed_pending"] == []
    assert audit["residual_enforced"] == []
    assert audit["receipts"] == 2


# ---------------------------------------------------------------------------
# The documented late-write case, end to end
# ---------------------------------------------------------------------------


async def test_late_write_flagged_by_audit_then_cleared_by_next_sweep(
    temp_storage: SQLiteStorage,
) -> None:
    """Post-sweep residual lands, the audit flags it, the next sweep clears it."""
    event_id = "ceiling-e2e-late"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    receipt_before = await temp_storage._read_one(
        "SELECT enforced_at, rows_removed, logical_bytes_removed, blobs_removed, "
        "blob_bytes_removed FROM event_content_ceiling_log WHERE event_id=?",
        (event_id,),
    )
    assert receipt_before is not None

    await _seed_late_write(temp_storage, event_id, outbox_id)

    # The audit flags the residual on the already-enforced event, per store.
    flagged = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=2)
    )
    assert flagged["elapsed_pending"] == []
    assert len(flagged["residual_enforced"]) == 1
    finding = flagged["residual_enforced"][0]
    assert finding["event_id"] == event_id
    assert finding["residual_stores"] == {
        "delivery_observations": 1,
        "delivery_evidence_diagnostics": 1,
    }

    # The verification fails for the receipt until the sweep re-purges,
    # while the recorded counters stay immutable.
    failing = await temp_storage.verify_content_ceiling_receipts()
    assert failing["verdict"] == "failed"
    assert failing["receipts_failed"] == 1
    failed_receipt = failing["receipts"][0]
    assert failed_receipt["event_id"] == event_id
    assert failed_receipt["status"] == "failed"
    assert failed_receipt["residual_stores"] == finding["residual_stores"]
    assert (
        failed_receipt["rows_removed"],
        failed_receipt["logical_bytes_removed"],
        failed_receipt["blobs_removed"],
        failed_receipt["blob_bytes_removed"],
    ) == (
        receipt_before["rows_removed"],
        receipt_before["logical_bytes_removed"],
        receipt_before["blobs_removed"],
        receipt_before["blob_bytes_removed"],
    )

    # The next sweep re-purges the residual without a second receipt.
    followup = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=3)
    )
    assert len(followup) == 1
    assert followup[0].residual_pass is True

    # The audit is clean again and the verification passes with the same
    # immutable receipt; read mediation denied the content in the interim.
    clean = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=4)
    )
    assert clean["residual_enforced"] == []
    assert clean["elapsed_pending"] == []
    passing = await temp_storage.verify_content_ceiling_receipts()
    assert passing["verdict"] == "passed"
    assert passing["receipts_examined"] == 1
    assert passing["receipts"][0]["status"] == "passed"
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_content_ceiling_log")
        == 1
    )
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id='obs-late'"
    )
    assert row is not None and row["metadata"] == "{}"


# ---------------------------------------------------------------------------
# Fail-closed single-event audit and verification states
# ---------------------------------------------------------------------------


async def test_single_event_audit_and_verify_fail_closed(
    temp_storage: SQLiteStorage,
) -> None:
    """Unknown, unelapsed, elapsed-unenforced, and receiptless events report
    honestly; absence of proof is never proof."""
    elapsed_unenforced = "ceiling-e2e-pending"
    await _admit_with_ceiling(temp_storage, elapsed_unenforced)
    ceiling = await _read_ceiling(temp_storage, elapsed_unenforced)

    pending = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1), event_id=elapsed_unenforced
    )
    assert pending["state"] == "elapsed_pending"
    assert pending["residual_stores"] == {"event_body_content": 1}
    assert pending["receipt"] is None

    unknown = await temp_storage.audit_content_ceilings(event_id="ceiling-e2e-missing")
    assert unknown["state"] == "unknown"

    not_elapsed = await temp_storage.audit_content_ceilings(event_id=elapsed_unenforced)
    assert not_elapsed["state"] == "not_elapsed"

    # Verification of an elapsed event without a receipt fails closed.
    unproven = await temp_storage.verify_content_ceiling_receipts(
        event_id=elapsed_unenforced
    )
    assert unproven["verdict"] == "failed"
    assert unproven["event_state"] == "no_receipt"
    assert unproven["receipts_examined"] == 0

    # An empty receipt store proves nothing and fails nothing.
    empty = await temp_storage.verify_content_ceiling_receipts()
    assert empty["verdict"] == "passed"
    assert empty["receipts_examined"] == 0

    # One sweep makes the single-event verification pass.
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    proven = await temp_storage.verify_content_ceiling_receipts(
        event_id=elapsed_unenforced
    )
    assert proven["verdict"] == "passed"
    assert proven["event_state"] == "receipt_verified"
    assert proven["receipts"][0]["status"] == "passed"


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


async def _prepare_database(db_path: str, *, with_late_write: bool = False) -> str:
    """Build a lifecycle database through the real storage backend.

    Admission refuses a deadline in the past, so the stored ceiling is
    moved behind the admission instant afterwards: the CLI scenarios run
    against the store's real clock, and the moved ceiling makes the event
    honestly elapsed by then, exactly as after the passage of time.
    """
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        event_id = "ceiling-cli-full"
        await _admit_with_ceiling(storage, event_id)
        outbox_id = await _seed_every_copy(storage, event_id)
        # Staging requires live content authority. Model elapsed time only
        # after all sample writes; otherwise the test cannot prove a sweep.
        await storage._write(
            "UPDATE event_content_access_policies SET hard_ceiling=? "
            "WHERE event_id=?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(
                    timespec="microseconds"
                ),
                event_id,
            ),
        )
        # One unshared attachment so the receipt carries blob counters.
        await _insert_blob(storage, _BLOB_REF)
        await _associate_blob(storage, event_id, _BLOB_REF)
        ceiling = await _read_ceiling(storage, event_id)
        await storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
        if with_late_write:
            await _seed_late_write(storage, event_id, outbox_id)
    finally:
        await storage.close()
    return db_path


async def _create_empty_database(db_path: str) -> None:
    """Create a valid, initialized, empty storage database."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    await storage.close()


def test_cli_audit_and_verify_on_clean_store(tmp_path: Path) -> None:
    """Both commands emit their content-free JSON and exit 0 on a clean store."""
    db_path = str(tmp_path / "clean.db")
    asyncio.run(_create_empty_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    report = _parse_cli_json(stdout)
    assert report["format"] == "medre-content-ceiling-audit"
    assert report["mode"] == "audit"
    assert report["purged"] is False
    assert report["physical_erasure_claimed"] is False
    assert report["elapsed_pending"] == []
    assert report["residual_enforced"] == []
    assert report["receipts"] == 0

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    report = _parse_cli_json(stdout)
    assert report["format"] == "medre-content-ceiling-verify"
    assert report["mode"] == "verify"
    assert report["verdict"] == "passed"
    assert report["receipts_examined"] == 0


def test_cli_audit_and_verify_prove_purged_lifecycle(tmp_path: Path) -> None:
    """On a swept database the CLI proof matches the storage-level contract."""
    db_path = str(tmp_path / "lifecycle.db")
    asyncio.run(_prepare_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert audit["elapsed_pending"] == []
    assert audit["residual_enforced"] == []
    assert audit["receipts"] == 1

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "passed"
    assert verify["receipts_passed"] == 1
    receipt = verify["receipts"][0]
    assert receipt["event_id"] == "ceiling-cli-full"
    assert receipt["rows_removed"] > 0
    assert receipt["logical_bytes_removed"] > 0
    assert receipt["blobs_removed"] == 1
    assert receipt["blob_bytes_removed"] == 10


def test_cli_verify_exits_failed_on_residual_and_flags(tmp_path: Path) -> None:
    """A late-write residual makes verify exit 1; the audit flags the store."""
    db_path = str(tmp_path / "late.db")
    asyncio.run(_prepare_database(db_path, with_late_write=True))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert len(audit["residual_enforced"]) == 1
    finding = audit["residual_enforced"][0]
    assert finding["event_id"] == "ceiling-cli-full"
    assert finding["residual_stores"] == {
        "delivery_observations": 1,
        "delivery_evidence_diagnostics": 1,
    }

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == EXIT_FAILED, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "failed"
    assert verify["receipts_failed"] == 1

    # Remediation is the sweep's job; the CLI never mutates anything.

    async def _sweep() -> list:
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        try:
            return await storage.enforce_content_ceilings()
        finally:
            await storage.close()

    followup = asyncio.run(_sweep())
    assert len(followup) == 1
    assert followup[0].residual_pass is True

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "passed"


def test_cli_refusals(tmp_path: Path) -> None:
    """Missing databases and malformed flags fail closed with exit codes."""
    missing = str(tmp_path / "absent.db")
    code, _, stderr = _run_cli("storage", "ceiling", "audit", "--storage-path", missing)
    assert code == EXIT_BUILD
    assert "existing SQLite database required" in stderr

    no_store = str(tmp_path / "empty.db")
    asyncio.run(_create_empty_database(no_store))

    code, _, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", no_store, "--limit", "0"
    )
    assert code == EXIT_CONFIG
    assert "--limit" in stderr

    code, _, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", no_store, "--limit", "101"
    )
    assert code == EXIT_CONFIG

    code, _, stderr = _run_cli(
        "storage",
        "ceiling",
        "verify",
        "--storage-path",
        no_store,
        "--event",
        "",
    )
    assert code == EXIT_CONFIG


def test_cli_single_event_modes(tmp_path: Path) -> None:
    """--event reports exactly one event and verify fails closed without it."""
    db_path = str(tmp_path / "single.db")
    asyncio.run(_prepare_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage",
        "ceiling",
        "verify",
        "--storage-path",
        db_path,
        "--event",
        "ceiling-never-admitted",
    )
    assert code == EXIT_FAILED, stderr
    verify = _parse_cli_json(stdout)
    assert verify["event_state"] == "no_receipt"
    assert verify["receipts_examined"] == 0

    code, stdout, stderr = _run_cli(
        "storage",
        "ceiling",
        "audit",
        "--storage-path",
        db_path,
        "--event",
        "ceiling-cli-full",
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert audit["state"] == "enforced_clean"
    assert audit["residual_stores"] == {}
    assert audit["receipt"]["rows_removed"] > 0


def test_cli_commands_are_strictly_read_only(tmp_path: Path) -> None:
    """Audit and verify write no application data: the database image is
    byte-identical after both read-only runs. SQLite may create empty WAL
    bookkeeping sidecars for its readers — the same documented caveat as
    the catalog status command — so sidecars are not part of the claim."""
    db_path = str(tmp_path / "readonly.db")
    asyncio.run(_prepare_database(db_path))

    with open(db_path, "rb") as source:
        before = hashlib.sha256(source.read()).hexdigest()

    for mode in ("audit", "verify"):
        code, _, stderr = _run_cli(
            "storage", "ceiling", mode, "--storage-path", db_path
        )
        assert code == 0, stderr

    with open(db_path, "rb") as source:
        assert hashlib.sha256(source.read()).hexdigest() == before


def test_ceiling_commands_registered_in_parser() -> None:
    """The storage ceiling surface is registered with both modes and flags."""
    from medre.cli.main import _build_parser

    parser = _build_parser()
    parsed = parser.parse_args(
        ["storage", "ceiling", "audit", "--storage-path", "/dev/null"]
    )
    assert parsed.storage_command == "ceiling"
    assert parsed.ceiling_command == "audit"
    assert parsed.limit == 25
    assert parsed.event is None
    parsed = parser.parse_args(
        [
            "storage",
            "ceiling",
            "verify",
            "--storage-path",
            "/dev/null",
            "--limit",
            "50",
            "--event",
            "evt",
        ]
    )
    assert parsed.ceiling_command == "verify"
    assert parsed.limit == 50
    assert parsed.event == "evt"
