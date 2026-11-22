"""Core-only end-to-end hard-ceiling enforcement and operator audit coverage.

These tests drive real SQLite storage, content lifecycle, receipt verification,
late residual writes and pagination without importing provider adapters or CLI.
"""

from __future__ import annotations

from datetime import timedelta

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _BLOB_REF,
    _admit_with_ceiling,
    _associate_blob,
    _count,
    _insert_blob,
    _read_ceiling,
    _seed_every_copy,
    _seed_incoming_excerpt,
    _seed_late_write,
)

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
    before_observation = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id=?",
        (f"obs-{event_id}",),
    )
    before_native = await temp_storage._read_one(
        "SELECT metadata FROM native_message_refs WHERE event_id=?", (event_id,)
    )
    pre_audit = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert (
        await temp_storage._read_one(
            "SELECT metadata FROM delivery_observations WHERE observation_id=?",
            (f"obs-{event_id}",),
        )
        == before_observation
    )
    assert (
        await temp_storage._read_one(
            "SELECT metadata FROM native_message_refs WHERE event_id=?", (event_id,)
        )
        == before_native
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
    orphan_ref = "orphan-not-associated"
    await _insert_blob(temp_storage, orphan_ref)
    await _associate_blob(temp_storage, owner_a, _BLOB_REF)
    await _associate_blob(temp_storage, owner_b, _BLOB_REF)

    ceiling_a = await _read_ceiling(temp_storage, owner_a)
    ceiling_b = await _read_ceiling(temp_storage, owner_b)
    await temp_storage.enforce_content_ceilings(now=ceiling_a + timedelta(seconds=1))
    # The first referent's purge must preserve both the other association
    # and the exact stored bytes, not merely an empty placeholder row.
    remaining = await temp_storage._read_one(
        "SELECT a.event_id, b.data FROM event_attachment_associations a "
        "JOIN attachment_blobs b ON b.content_ref=a.content_ref "
        "WHERE a.content_ref=?",
        (_BLOB_REF,),
    )
    assert remaining == {"event_id": owner_b, "data": b"ATTACHDATA"}
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
    orphan = await temp_storage._read_one(
        "SELECT data FROM attachment_blobs WHERE content_ref=?", (orphan_ref,)
    )
    assert orphan == {"data": b"ATTACHDATA"}
    both = await temp_storage.verify_content_ceiling_receipts()
    assert both["receipts_examined"] == 2
    assert both["verdict"] == "passed"
    b_receipt = next(r for r in both["receipts"] if r["event_id"] == owner_b)
    assert b_receipt["blobs_removed"] == 1
    assert b_receipt["blob_bytes_removed"] == len(b"ATTACHDATA")
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

    late_id = await _seed_late_write(temp_storage, event_id, outbox_id)

    # Read mediation hides the late metadata and diagnostic before repair.
    observed = await temp_storage.list_delivery_observations_for_event(event_id)
    late = next(item for item in observed if item.observation_id == late_id)
    assert late.metadata == {}
    assert late.error is None

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
    assert {
        key: passing["receipts"][0][key] for key in receipt_before
    } == receipt_before
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_content_ceiling_log")
        == 1
    )
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id=?",
        (late_id,),
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
    other = "ceiling-e2e-other-without-receipt"
    await _admit_with_ceiling(temp_storage, other)
    still_unproven = await temp_storage.verify_content_ceiling_receipts(event_id=other)
    assert still_unproven["verdict"] == "failed"
    assert still_unproven["event_state"] == "no_receipt"
    assert still_unproven["receipts_examined"] == 0


async def test_reports_continue_in_event_id_order(temp_storage: SQLiteStorage) -> None:
    events = ["ceiling-page-a", "ceiling-page-b"]
    for event_id in reversed(events):
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, events[0]) + timedelta(seconds=1)
    first_audit = await temp_storage.audit_content_ceilings(now=now, limit=1)
    assert [row["event_id"] for row in first_audit["elapsed_pending"]] == [events[0]]
    assert first_audit["elapsed_pending_has_more"] is True
    key = first_audit["elapsed_pending_next_after_event"]
    assert key == events[0]
    second_audit = await temp_storage.audit_content_ceilings(
        now=now, limit=1, after_event=key
    )
    assert [row["event_id"] for row in second_audit["elapsed_pending"]] == events[1:]
    assert second_audit["elapsed_pending_next_after_event"] is None
    assert second_audit["elapsed_pending_has_more"] is False

    await temp_storage.enforce_content_ceilings(now=now)
    first_verify = await temp_storage.verify_content_ceiling_receipts(limit=1)
    assert [row["event_id"] for row in first_verify["receipts"]] == [events[0]]
    assert first_verify["receipts_has_more"] is True
    key = first_verify["receipts_next_after_event"]
    assert key == events[0]
    second_verify = await temp_storage.verify_content_ceiling_receipts(
        limit=1, after_event=key
    )
    assert [row["event_id"] for row in second_verify["receipts"]] == events[1:]
    assert second_verify["receipts_next_after_event"] is None
    assert second_verify["receipts_has_more"] is False


async def test_elapsed_unswept_body_reads_denied_while_the_audit_reports_it(
    temp_storage: SQLiteStorage,
) -> None:
    """Execution-time denial and the audit describe the same unswept event.

    After the ceiling elapses but before any sweep records a receipt, the
    body read denies at execution time while the read-only audit still
    reports the stored body as pending purge work. Denial is not erasure.
    """
    event_id = "ceiling-audit-pre-sweep-denial"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)

    before = await temp_storage.read_body_for_outbox(
        event_id, outbox_id, now=ceiling - timedelta(seconds=1)
    )
    assert before.status == "retained"

    after = await temp_storage.read_body_for_outbox(
        event_id, outbox_id, now=ceiling + timedelta(seconds=1)
    )
    assert after.status == "denied"

    pre_audit = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert pre_audit["receipts"] == 0
    assert len(pre_audit["elapsed_pending"]) == 1
    pending = pre_audit["elapsed_pending"][0]
    assert pending["event_id"] == event_id
    assert pending["residual_stores"]["event_body_content"] == 1
    # The stored body is unchanged: denial is not erasure.
    stored = await temp_storage._read_one(
        "SELECT COUNT(*) AS c FROM event_body_content WHERE event_id=?",
        (event_id,),
    )
    assert stored is not None and stored["c"] == 1
