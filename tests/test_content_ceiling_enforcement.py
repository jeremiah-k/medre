"""Per-store hard content-privacy ceiling enforcement qualification.

Covers the guarded ceiling-enforcement transition end to end over the real
``SQLiteStorage``: an elapsed immutable ceiling purges every enumerated
content store for the event, verification demands zero residual
content-potential values before the receipt commits, linkage and evidence
rows survive with their content values cleared, shared attachment bytes
survive while another event still refers to them, already-enforced events
are re-purged for late residual writes without a second receipt, and the
collector coherence accepts the ceiling receipt. Privacy placeholders only:
every seeded value is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item

MARKER = "CEILING-QUALIFICATION-BODY"
_QUOTE = "quoted synthetic excerpt text"
_FINGERPRINT = "ab" * 32
_BLOB_REF = "sha256:" + "cd" * 32


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


async def _admit_with_ceiling(store: SQLiteStorage, event_id: str) -> None:
    """Admit one event with a body through the real ceiling producer.

    ``admit_ingress`` with a content deadline installs the mandatory
    dependency and the immutable ceiling row in the admission transaction.
    """
    await store.admit_ingress(
        make_storage_event(event_id=event_id, payload={"body": MARKER}),
        None,
        "live",
        content_deadline=_future(3600),
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

    Returns the seeded outbox identity. Every seeded value is synthetic;
    the marker exists so assertions can prove exactly which values left.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    # Opaque envelope values on the canonical row.
    await store._write(
        "UPDATE canonical_events SET payload=?, metadata=? WHERE event_id=?",
        ('{"opaque":"envelope-note"}', '{"custom":{"note":"opaque"}}', event_id),
    )
    # Same-event relation with content-potential key/metadata and excerpt.
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
    # Native reference with provider metadata.
    await store._write(
        "INSERT INTO native_message_refs (id, event_id, adapter, native_message_id, "
        "direction, metadata, created_at) VALUES (?, ?, 'source', 'nm-1', "
        "'inbound', ?, ?)",
        (f"nref-{event_id}", event_id, '{"native":"opaque"}', now),
    )
    # Outbox generation with a frozen dispatch snapshot and inline context.
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
    # Delivery receipt with separated diagnostic text.
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
    # Delivery observation with provider metadata and its own diagnostic.
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
    # Radio recovery plan riding the outbox generation.
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
    # Attachment blob plus its association.
    await store._write(
        "INSERT INTO attachment_blobs (content_ref, size_bytes, media_kind, "
        "mime_type, created_at, data) VALUES (?, 10, 'image', 'image/png', ?, ?)",
        (_BLOB_REF, now, b"ATTACHDATA"),
    )
    await store._write(
        "INSERT INTO event_attachment_associations (event_id, content_ref, "
        "created_at) VALUES (?, ?, ?)",
        (event_id, _BLOB_REF, now),
    )
    return outbox_id


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


async def _count(store: SQLiteStorage, sql: str, params: tuple = ()) -> int:
    row = await store._read_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


# ---------------------------------------------------------------------------
# The full per-store purge
# ---------------------------------------------------------------------------


async def test_elapsed_ceiling_purges_every_enumerated_store(
    temp_storage: SQLiteStorage,
) -> None:
    """One elapsed ceiling removes every enumerated content value and
    verifies the residual battery before the receipt commits."""
    event_id = "ceiling-full"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    await _seed_incoming_excerpt(temp_storage, event_id)

    ceiling = await _read_ceiling(temp_storage, event_id)
    enforced = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert len(enforced) == 1
    summary = enforced[0]
    assert summary.event_id == event_id
    assert summary.residual_pass is False
    assert summary.rows_removed > 0
    assert summary.logical_bytes_removed > 0
    assert summary.blobs_removed == 1
    assert summary.blob_bytes_removed == 10

    # Separated content rows are gone.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_body_content WHERE event_id=?",
            (event_id,),
        )
        == 0
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_relation_excerpts x "
            "JOIN event_relations r ON r.id=x.relation_id WHERE r.event_id=?",
            (event_id,),
        )
        == 0
    )
    # Incoming excerpts quoting the expired event are gone too.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_relation_excerpts x "
            "JOIN event_relations r ON r.id=x.relation_id WHERE r.target_event_id=?",
            (event_id,),
        )
        == 0
    )
    # Opaque envelope cleared; identity columns survive.
    row = await temp_storage._read_one(
        "SELECT payload, metadata FROM canonical_events WHERE event_id=?", (event_id,)
    )
    assert row is not None
    assert (row["payload"], row["metadata"]) == ("{}", "{}")
    # Relation linkage survives with cleared content values.
    row = await temp_storage._read_one(
        "SELECT key, metadata FROM event_relations WHERE event_id=?", (event_id,)
    )
    assert (row["key"], row["metadata"]) == (None, "{}")
    # Native reference linkage survives with cleared metadata.
    row = await temp_storage._read_one(
        "SELECT metadata FROM native_message_refs WHERE event_id=?", (event_id,)
    )
    assert row["metadata"] == "{}"
    # Outbox generation survives (never deleted) with cleared inline context.
    row = await temp_storage._read_one(
        "SELECT status, metadata FROM delivery_outbox WHERE outbox_id=?",
        (outbox_id,),
    )
    assert row["status"] == "pending"
    assert row["metadata"] == "{}"
    # Receipt evidence row survives with no diagnostic text anywhere.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM delivery_receipts WHERE event_id=?",
            (event_id,),
        )
        == 1
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM delivery_evidence_diagnostics d WHERE "
            "(d.family='receipt' AND d.row_id IN (SELECT receipt_id "
            "FROM delivery_receipts WHERE event_id=?)) OR "
            "(d.family='observation' AND d.row_id IN (SELECT observation_id "
            "FROM delivery_observations WHERE event_id=?)) OR "
            "(d.family='outbox' AND d.row_id IN (SELECT outbox_id "
            "FROM delivery_outbox WHERE event_id=?)) OR "
            "(d.family='ingress' AND d.row_id=?)",
            (event_id, event_id, event_id, event_id),
        )
        == 0
    )
    # Observation linkage survives with cleared provider metadata.
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE event_id=?", (event_id,)
    )
    assert row["metadata"] == "{}"
    # Radio plan and frozen snapshot rows are gone.
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM radio_attempt_progress") == 0
    )
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM delivery_outbox_snapshots")
        == 0
    )
    # Attachment association and unreferenced blob bytes are gone.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_attachment_associations WHERE event_id=?",
            (event_id,),
        )
        == 0
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 0
    )
    # The receipt recorded the enforcement, content-free.
    receipt = await temp_storage._read_one(
        "SELECT enforced_at, ceiling, rows_removed, blobs_removed, "
        "blob_bytes_removed FROM event_content_ceiling_log WHERE event_id=?",
        (event_id,),
    )
    assert receipt is not None
    assert datetime.fromisoformat(str(receipt["ceiling"])) == ceiling
    assert receipt["rows_removed"] == summary.rows_removed
    # Grants are tombstoned and dependencies released.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_content_access_grants WHERE event_id=? "
            "AND expired_at IS NULL",
            (event_id,),
        )
        == 0
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_content_dependencies WHERE event_id=? "
            "AND released_at IS NULL",
            (event_id,),
        )
        == 0
    )
    # Every authorized read path now denies the expired content.
    body = await temp_storage.read_body_for_outbox(event_id, outbox_id)
    assert body.status == "denied"
    replay = await temp_storage.read_event_for_replay(event_id)
    assert replay.status == "denied"
    # The collector coherence accepts the ceiling receipt.
    collected = await temp_storage.collect_event_content(event_id, dry_run=True)
    assert collected["status"] == "already_collected"


async def test_unelapsed_ceiling_is_not_enforced(temp_storage: SQLiteStorage) -> None:
    """A ceiling that has not elapsed enforces nothing."""
    event_id = "ceiling-future"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    enforced = await temp_storage.enforce_content_ceilings(
        now=ceiling - timedelta(seconds=1)
    )
    assert enforced == []
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_content_ceiling_log")
        == 0
    )


async def test_enforcement_is_idempotent(temp_storage: SQLiteStorage) -> None:
    """A second sweep finds no new candidates and never rewrites a receipt."""
    event_id = "ceiling-idem"
    await _admit_with_ceiling(temp_storage, event_id)
    await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    first = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert len(first) == 1
    receipt_before = await temp_storage._read_one(
        "SELECT enforced_at, rows_removed FROM event_content_ceiling_log "
        "WHERE event_id=?",
        (event_id,),
    )
    second = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=2)
    )
    assert second == []
    receipt_after = await temp_storage._read_one(
        "SELECT enforced_at, rows_removed FROM event_content_ceiling_log "
        "WHERE event_id=?",
        (event_id,),
    )
    assert receipt_before == receipt_after


async def test_shared_blob_survives_other_event_reference(
    temp_storage: SQLiteStorage,
) -> None:
    """Content-addressed bytes outlive one event's ceiling while another
    event still refers to them, and go with the last reference."""
    owner_a = "ceiling-share-a"
    owner_b = "ceiling-share-b"
    await _admit_with_ceiling(temp_storage, owner_a)
    await _admit_with_ceiling(temp_storage, owner_b)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await temp_storage._write(
        "INSERT INTO attachment_blobs (content_ref, size_bytes, media_kind, "
        "mime_type, created_at, data) VALUES (?, 10, 'image', 'image/png', ?, ?)",
        (_BLOB_REF, now, b"ATTACHDATA"),
    )
    for event_id in (owner_a, owner_b):
        await temp_storage._write(
            "INSERT INTO event_attachment_associations (event_id, content_ref, "
            "created_at) VALUES (?, ?, ?)",
            (event_id, _BLOB_REF, now),
        )
    ceiling_a = await _read_ceiling(temp_storage, owner_a)
    ceiling_b = await _read_ceiling(temp_storage, owner_b)
    await temp_storage.enforce_content_ceilings(
        now=max(ceiling_a, ceiling_b) + timedelta(seconds=1)
    )
    # Both associations gone; the shared bytes went with the last one.
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_attachment_associations WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 0
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 0
    )


async def test_late_evidence_write_is_repurged_without_second_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    """A write authorized before the ceiling lands after enforcement; the
    next sweep re-purges the residual values and keeps one receipt."""
    event_id = "ceiling-late"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))

    # A late completion write for the in-flight generation.
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await temp_storage._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES ('obs-late', ?, ?, 'radio', ?, 1, "
        "'delivered', ?, ?)",
        (event_id, f"plan-{event_id}", outbox_id, '{"provider":"late"}', now),
    )
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('observation', 'obs-late', 1, "
        "'error', ?, ?)",
        (f"late {MARKER}", now),
    )
    followup = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=2)
    )
    assert len(followup) == 1
    assert followup[0].residual_pass is True
    assert followup[0].event_id == event_id
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id='obs-late'"
    )
    assert row["metadata"] == "{}"
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM delivery_evidence_diagnostics WHERE "
            "row_id='obs-late'",
        )
        == 0
    )
    # Exactly one receipt remains.
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_content_ceiling_log")
        == 1
    )


async def test_unstarted_outbox_survives_enforcement_for_deadline_cleanup(
    temp_storage: SQLiteStorage,
) -> None:
    """Enforcement never deletes outbox rows: unstarted expired work stays
    available to the guarded deadline retirement transition."""
    event_id = "ceiling-outbox"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    row = await temp_storage._read_one(
        "SELECT status FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
    )
    assert row["status"] == "pending"
    # The deadline cleanup can still retire the provably unstarted row.
    retired = await temp_storage.retire_expired_unstarted_outbox(
        now=ceiling + timedelta(seconds=2)
    )
    assert [item.outbox_id for item in retired] == [outbox_id]


async def test_additive_predecessor_gains_ceiling_objects(
    temp_storage: SQLiteStorage,
) -> None:
    """A validated database without the ceiling objects upgrades additively."""
    import sqlite3

    db_path = temp_storage._db_path
    await temp_storage.close()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX IF EXISTS idx_ceiling_enforcement_order")
    raw.execute("DROP TABLE event_content_ceiling_log")
    raw.commit()
    raw.close()
    reopened = SQLiteStorage(db_path)
    await reopened.initialize()
    names = {
        str(row["name"])
        for row in await reopened._read_all(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('event_content_ceiling_log', 'idx_ceiling_enforcement_order')"
        )
    }
    assert names == {"event_content_ceiling_log", "idx_ceiling_enforcement_order"}
    assert await reopened.enforce_content_ceilings(now=datetime.now(timezone.utc)) == []
    await reopened.close()
