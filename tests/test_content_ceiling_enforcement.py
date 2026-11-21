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
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
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


async def _admit_with_ceiling(
    store: SQLiteStorage, event_id: str, *, seconds: int = 3600
) -> None:
    """Admit one event with a body through the real ceiling producer.

    ``admit_ingress`` with a content deadline installs the mandatory
    dependency and the immutable ceiling row in the admission transaction.
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
    await _admit_with_ceiling(temp_storage, owner_b, seconds=86400)
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
    await temp_storage.enforce_content_ceilings(now=ceiling_a + timedelta(seconds=1))
    # One live referent must retain its association and bytes.
    still_shared = await _count(
        temp_storage,
        "SELECT COUNT(*) FROM event_attachment_associations WHERE content_ref=?",
        (_BLOB_REF,),
    )
    assert still_shared == 1
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM attachment_blobs WHERE content_ref=?",
            (_BLOB_REF,),
        )
        == 1
    )
    await temp_storage.enforce_content_ceilings(now=ceiling_b + timedelta(seconds=1))
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
    staged = await temp_storage.create_outbox_item(
        make_outbox_item(
            delivery_plan_id=f"plan-{event_id}",
            event_id=event_id,
            target_adapter="radio",
        ),
        dispatch_snapshot=_snapshot({"deadline": _future(7200).isoformat()}),
    )
    outbox_id = staged.outbox_id
    assert staged.active_attempt is None and staged.status == "pending"
    for table in (
        "delivery_receipts",
        "delivery_observations",
        "radio_attempt_progress",
    ):
        row = await temp_storage._read_one(
            f"SELECT 1 FROM {table} WHERE outbox_id=? LIMIT 1",  # nosec B608 - closed literal table list
            (outbox_id,),
        )
        assert row is None
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    row = await temp_storage._read_one(
        "SELECT status FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
    )
    assert row["status"] == "pending"
    # The deadline cleanup can still retire the provably unstarted row.
    # The seeded plan's deadline is 7200s after admission and the content
    # ceiling is 3600s later: choose a time after both durable deadlines.
    retired = await temp_storage.retire_expired_unstarted_outbox(
        now=ceiling + timedelta(seconds=7201)
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
    try:
        await _check_additive_ceiling_reopen(reopened)
    finally:
        await reopened.close()


async def _check_additive_ceiling_reopen(reopened: SQLiteStorage) -> None:
    names = {
        str(row["name"])
        for row in await reopened._read_all(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('event_content_ceiling_log', 'idx_ceiling_enforcement_order')"
        )
    }
    assert names == {"event_content_ceiling_log", "idx_ceiling_enforcement_order"}
    assert await reopened.enforce_content_ceilings(now=datetime.now(timezone.utc)) == []


# -- Adversarial review: pending/repaired progress and late-write denial --


async def test_first_time_enforcement_advances_across_a_page_of_receipts(
    temp_storage: SQLiteStorage,
) -> None:
    prior = [f"ceiling-prior-{index:02}" for index in range(25)]
    for event_id in prior:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, prior[-1]) + timedelta(seconds=1)
    enforced = await temp_storage.enforce_content_ceilings(now=now, limit=25)
    assert {item.event_id for item in enforced} == set(prior)
    # Wrap the policy cursor, then admit work ordered after the clean page.
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    pending = "ceiling-pending"
    await _admit_with_ceiling(temp_storage, pending)
    later = await _read_ceiling(temp_storage, pending) + timedelta(seconds=1)
    enforced = await temp_storage.enforce_content_ceilings(now=later, limit=1)
    assert [item.event_id for item in enforced] == [pending]
    assert await temp_storage.enforce_content_ceilings(now=later, limit=1) == []


async def test_receipt_cursor_preserves_unexamined_short_page(
    temp_storage: SQLiteStorage,
) -> None:
    prior = [f"ceiling-done-{index:02}" for index in range(25)]
    for event_id in prior:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, prior[-1]) + timedelta(seconds=1)
    assert len(await temp_storage.enforce_content_ceilings(now=now, limit=25)) == 25
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    pending = ["ceiling-unprocessed-first", "ceiling-unprocessed-second"]
    for event_id in pending:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, pending[-1]) + timedelta(seconds=1)
    for event_id in pending:
        outcomes = await temp_storage.enforce_content_ceilings(now=now, limit=1)
        assert [outcome.event_id for outcome in outcomes] == [event_id]
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []


async def test_evidence_reads_hide_provider_values_at_expiry_without_receipt(
    temp_storage: SQLiteStorage,
    monkeypatch,
) -> None:
    import medre.core.storage.sqlite.connection as connection

    event_id = "ceiling-expiry-read"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    monkeypatch.setattr(
        connection,
        "ceiling_read_time",
        lambda: ceiling.isoformat(timespec="microseconds"),
    )

    refs = await temp_storage.list_native_refs_for_event(event_id)
    assert refs and all(not ref.metadata for ref in refs)
    references = await temp_storage.get_native_ref(
        refs[0].adapter, refs[0].native_channel_id, refs[0].native_message_id
    )
    assert references is not None and not references.metadata
    observations = await temp_storage.list_delivery_observations_for_outbox(outbox_id)
    assert observations and all(
        not row.metadata and row.error is None for row in observations
    )
    assert not await temp_storage._read_one("SELECT 1 FROM event_content_ceiling_log")
    assert await temp_storage._read_one(
        "SELECT 1 FROM native_message_refs WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    )


async def test_pending_event_is_not_starved_by_residual_repair(
    temp_storage: SQLiteStorage,
) -> None:
    """Both queues make progress with limit=1 and retain exact residual counts."""
    first, pending = "ceiling-repair-prior", "ceiling-repair-pending"
    await _admit_with_ceiling(temp_storage, first)
    first_ceiling = await _read_ceiling(temp_storage, first)
    await temp_storage.enforce_content_ceilings(
        now=first_ceiling + timedelta(seconds=1), limit=1
    )
    await temp_storage._write(
        "UPDATE canonical_events SET metadata=? WHERE event_id=?",
        ('{"late":"synthetic"}', first),
    )
    await _admit_with_ceiling(temp_storage, pending)
    pending_ceiling = await _read_ceiling(temp_storage, pending)
    repaired = await temp_storage.enforce_content_ceilings(
        now=max(first_ceiling, pending_ceiling) + timedelta(seconds=2), limit=1
    )
    assert {(r.event_id, r.residual_pass) for r in repaired} == {
        (first, True),
        (pending, False),
    }
    residual = next(r for r in repaired if r.residual_pass)
    assert residual.logical_bytes_removed == len(b'{"late":"synthetic"}')
    verification = await temp_storage.verify_content_ceiling_receipts()
    assert verification["verdict"] == "passed"
    assert verification["receipts_examined"] == 2


async def test_native_only_incoming_quote_without_reference_is_purged(
    temp_storage: SQLiteStorage,
) -> None:
    """Canonical source-native identity also owns quotes before ref publication."""
    owner, neighbor = "ceiling-native-source", "ceiling-native-quote"
    await _admit_with_ceiling(temp_storage, owner)
    await _admit_plain(temp_storage, neighbor)
    now = datetime.now(timezone.utc).isoformat()
    await temp_storage._write(
        "UPDATE canonical_events SET source_native_adapter='matrix', "
        "source_native_channel_id='room', source_native_message_id='message' "
        "WHERE event_id=?",
        (owner,),
    )
    await temp_storage._write(
        "INSERT INTO event_relations (event_id, relation_type, "
        "target_native_adapter, target_native_channel_id, "
        "target_native_message_id, metadata, created_at) "
        "VALUES (?, 'reply', 'matrix', 'room', 'message', '{}', ?)",
        (neighbor, now),
    )
    await temp_storage._write(
        "INSERT INTO event_relation_excerpts(relation_id, excerpt) "
        "SELECT id, 'sensitive quote' FROM event_relations WHERE event_id=?",
        (neighbor,),
    )
    ceiling = await _read_ceiling(temp_storage, owner)
    before = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1), event_id=owner
    )
    assert before["residual_stores"]["event_relation_excerpts:incoming"] == 1
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    assert (
        await temp_storage._read_one("SELECT excerpt FROM event_relation_excerpts")
        is None
    )


async def test_late_observation_values_are_masked_before_read(
    temp_storage: SQLiteStorage,
) -> None:
    """Retained evidence facts remain readable, provider text cannot reappear."""
    event_id = "ceiling-late-observation-read"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await temp_storage._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES (?, ?, ?, 'radio', ?, 1, "
        "'delivered', ?, ?)",
        (
            f"late-{event_id}",
            event_id,
            f"plan-{event_id}",
            outbox_id,
            '{"provider":"late"}',
            now,
        ),
    )
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES "
        "('observation', ?, 1, 'error', ?, ?)",
        (f"late-{event_id}", "synthetic late failure", now),
    )
    for read in (
        await temp_storage.list_delivery_observations_for_event(event_id),
        await temp_storage.list_delivery_observations_for_outbox(outbox_id),
        await temp_storage.list_delivery_observations_for_attempt(outbox_id, 1),
    ):
        late = [
            observation
            for observation in read
            if observation.observation_id == f"late-{event_id}"
        ]
        assert len(late) == 1
        assert late[0].state == "delivered"
        assert not late[0].metadata
        assert not late[0].error
        assert all(not observation.metadata for observation in read)
        assert all(not observation.error for observation in read)
    assert (
        await temp_storage._read_one(
            "SELECT metadata FROM delivery_observations WHERE event_id=? "
            "AND metadata<>'{}'",
            (event_id,),
        )
        is not None
    )  # The race is real until the next sweep.


async def test_audit_counts_sole_referent_blob_and_failed_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    """An audit describes the bytes it would actually delete, not just refs."""
    event_id = "ceiling-audit-sole-owner"
    blob = "sha256:" + "e" * 64
    await _admit_with_ceiling(temp_storage, event_id)
    now = datetime.now(timezone.utc).isoformat()
    await temp_storage._write(
        "INSERT INTO attachment_blobs(content_ref, size_bytes, media_kind, "
        "mime_type, created_at, data) VALUES (?, 4, 'image', 'image/png', ?, ?)",
        (blob, now, b"data"),
    )
    await temp_storage._write(
        "INSERT INTO event_attachment_associations(event_id, content_ref, "
        "created_at) VALUES (?, ?, ?)",
        (event_id, blob, now),
    )
    ceiling = await _read_ceiling(temp_storage, event_id)
    audit = await temp_storage.audit_content_ceilings(
        now=ceiling + timedelta(seconds=1), event_id=event_id
    )
    assert audit["residual_stores"]["event_attachment_associations"] == 1
    assert audit["residual_stores"]["attachment_blobs:last_reference"] == 1
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    await temp_storage._write(
        "UPDATE canonical_events SET metadata=? WHERE event_id=?",
        ('{"late":"synthetic"}', event_id),
    )
    failed = await temp_storage.verify_content_ceiling_receipts(event_id=event_id)
    assert failed["verdict"] == "failed"
    assert failed["event_state"] == "receipt_failed"


async def test_incoming_quote_prefers_canonical_target_over_native_hint(
    temp_storage: SQLiteStorage,
) -> None:
    """A canonical link to B must not be purged while A's native ID expires."""
    owner, actual, quoting = (
        "ceiling-native-hint-owner",
        "ceiling-canonical-target",
        "ceiling-quoting-neighbor",
    )
    await _admit_with_ceiling(temp_storage, owner)
    await _admit_with_ceiling(temp_storage, actual, seconds=7200)
    await _admit_plain(temp_storage, quoting)
    now = datetime.now(timezone.utc).isoformat()
    await temp_storage._write(
        "UPDATE canonical_events SET source_native_adapter='matrix', "
        "source_native_channel_id='room', source_native_message_id='M' "
        "WHERE event_id=?",
        (owner,),
    )
    await temp_storage._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "target_native_adapter, target_native_channel_id, "
        "target_native_message_id, metadata, created_at) "
        "VALUES (?, 'reply', ?, 'matrix', 'room', 'M', '{}', ?)",
        (quoting, actual, now),
    )
    await temp_storage._write(
        "INSERT INTO event_relation_excerpts(relation_id, excerpt) "
        "SELECT id, 'quoted B, not A' FROM event_relations WHERE event_id=?",
        (quoting,),
    )
    first = await _read_ceiling(temp_storage, owner)
    await temp_storage.enforce_content_ceilings(now=first + timedelta(seconds=1))
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_relation_excerpts") == 1
    )
    audit = await temp_storage.audit_content_ceilings(
        now=first + timedelta(seconds=1), event_id=actual
    )
    assert audit["residual_stores"]["event_relation_excerpts:incoming"] == 1


async def test_incoming_quote_battery_uses_target_indexes(
    temp_storage: SQLiteStorage,
) -> None:
    """An expired event probes indexed identities, not every unrelated quote."""
    from medre.core.storage.sqlite._ceiling_enforcement import (
        _INCOMING_EXCERPTS_PREDICATE,
    )

    plan_sql = (
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM event_relation_excerpts x "
        "WHERE "
        + _INCOMING_EXCERPTS_PREDICATE.format(
            ev="?"
        )  # nosec B608 - module-owned predicate with a bound event reference
    )
    details = await temp_storage._read_all(
        plan_sql, ("unrelated-owner",) * plan_sql.count("?")
    )
    plan = " ".join(str(row["detail"]) for row in details)
    assert "idx_relations_target_event_id" in plan
    assert "idx_relations_target_native_ref" in plan
    assert "SCAN x" not in plan


async def test_residual_cursor_revisits_old_receipts_despite_new_high_ids(
    temp_storage: SQLiteStorage,
) -> None:
    """A finite cycle returns to the first receipt even under appended IDs."""
    ids = [f"receipt-page-{index:02}" for index in range(30)]
    for event_id in ids:
        await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, ids[-1])
    now = ceiling + timedelta(seconds=1)
    enforced = await temp_storage.enforce_content_ceilings(now=now, limit=50)
    assert len(enforced) == 30
    await temp_storage._write(
        "UPDATE canonical_events SET metadata=? WHERE event_id=?",
        ('{"late":"text"}', ids[0]),
    )
    # A clean first page consumes the scan allowance but leaves old history.
    await temp_storage._write(
        "INSERT OR REPLACE INTO _medre_schema_meta(key, value) VALUES (?, ?)",
        ("content_ceiling_residual_cursor", '["receipt-page-00","receipt-page-29"]'),
    )
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    # More receipts arrive after the finite traversal began.
    newcomers = [f"receipt-z-{index:02}" for index in range(25)]
    for event_id in newcomers:
        await _admit_with_ceiling(temp_storage, event_id)
        await temp_storage._write(
            "INSERT INTO event_content_ceiling_log(event_id, enforced_at, ceiling, "
            "rows_removed, logical_bytes_removed, blobs_removed, blob_bytes_removed) "
            "SELECT event_id, ?, hard_ceiling, 0, 0, 0, 0 "
            "FROM event_content_access_policies WHERE event_id=?",
            (now.isoformat(), event_id),
        )
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    repaired = await temp_storage.enforce_content_ceilings(now=now, limit=1)
    assert any(item.event_id == ids[0] and item.residual_pass for item in repaired)


async def test_recreated_diagnostic_after_ceiling_is_not_already_collected(
    temp_storage: SQLiteStorage,
) -> None:
    """All-store battery, not just body rows, governs collector consistency."""
    epoch = await temp_storage.open_retention_epoch(
        activated_by="operator", now=datetime.now(timezone.utc)
    )
    event_id = "ceiling-recreated-diagnostic"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    now = datetime.now(timezone.utc).isoformat()
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics "
        "(family, row_id, attempt, field, diagnostic, created_at) "
        "VALUES ('outbox', ?, 1, 'error_summary', 'late private text', ?)",
        (outbox_id, now),
    )
    assessment = await temp_storage.assess_event_content_release(event_id)
    assert "inconsistent_admission_identity" in assessment["blockers"]
    assert assessment["core_subset_status"] == "blocked"
    import pytest

    from medre.core.storage.backend import StorageError

    with pytest.raises(StorageError, match="unexpectedly recreated"):
        await temp_storage.collect_event_content(event_id)

    result = await temp_storage.collect_event_content(
        event_id,
        retention_epoch=epoch.epoch,
    )
    assert result["status"] == "blocked"


async def test_late_native_reference_metadata_is_neither_written_nor_read(
    temp_storage: SQLiteStorage,
) -> None:
    """An accepted late native identity is durable, but not provider text."""
    from medre.core.events import NativeMessageRef

    event_id = "ceiling-late-native-reference"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
    ref = NativeMessageRef(
        id=f"ref-{event_id}",
        event_id=event_id,
        adapter="matrix",
        native_channel_id="!room:example",
        native_message_id="$after-ceiling",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={"provider": "synthetic-private"},
    )
    await temp_storage.store_native_ref(ref)
    raw = await temp_storage._read_one(
        "SELECT metadata FROM native_message_refs WHERE id=?", (ref.id,)
    )
    assert raw is not None and raw["metadata"] == "{}"
    # An already-authorized native finalizer may have written opaque metadata
    # before the ceiling receipt; ordinary inspection still withholds it.
    await temp_storage._write(
        "UPDATE native_message_refs SET metadata=? WHERE id=?",
        ('{"provider":"synthetic-race"}', ref.id),
    )
    found = await temp_storage.get_native_ref(
        ref.adapter, ref.native_channel_id, ref.native_message_id
    )
    listed = await temp_storage.list_native_refs_for_event(event_id)
    assert found is not None and found.event_id == event_id and not found.metadata
    assert len(listed) == 1 and not listed[0].metadata
    assert (
        await temp_storage.resolve_native_ref(
            ref.adapter, ref.native_channel_id, ref.native_message_id
        )
        == event_id
    )


async def test_ceiling_tombstones_the_same_live_optional_grant(
    temp_storage: SQLiteStorage,
) -> None:
    """Enforcement must expire an actually issued grant, not an empty table."""
    event_id = "ceiling-optional-grant-tombstone"
    await temp_storage.append(make_storage_event(event_id=event_id))
    issued = datetime.now(timezone.utc)
    ceiling = issued + timedelta(hours=1)
    hold = ContentHold(
        "reply_quotes", "body", issued + timedelta(minutes=5), issued_at=issued
    )
    policy = AvailabilityPolicy(
        hard_ceiling=ceiling,
        max_optional_hold=timedelta(minutes=10),
        allowed_optional_consumers=frozenset({"reply_quotes"}),
    )
    await temp_storage.issue_optional_body_hold(event_id, hold, policy, now=issued)
    before = await temp_storage._read_one(
        "SELECT consumer, expired_at FROM event_content_access_grants WHERE event_id=?",
        (event_id,),
    )
    assert before is not None
    assert before["consumer"] == "reply_quotes" and before["expired_at"] is None
    result = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert len(result) == 1 and result[0].event_id == event_id
    after = await temp_storage._read_one(
        "SELECT consumer, expired_at FROM event_content_access_grants WHERE event_id=?",
        (event_id,),
    )
    assert after is not None
    assert after["consumer"] == before["consumer"]
    assert after["expired_at"] is not None


async def test_native_reference_owner_takes_precedence_over_canonical_alias(
    temp_storage: SQLiteStorage,
) -> None:
    """A's expired source alias must not purge B's resolved native quote."""
    from medre.core.events import EventRelation, NativeRef

    await _admit_with_ceiling(temp_storage, "alias-a", seconds=3600)
    await _admit_with_ceiling(temp_storage, "owner-b", seconds=7200)
    await _admit_plain(temp_storage, "quoting-c")
    await temp_storage._write(
        "UPDATE canonical_events SET source_native_adapter='radio', "
        "source_native_channel_id='wire', source_native_message_id='same' "
        "WHERE event_id='alias-a'"
    )
    await temp_storage._write(
        "INSERT INTO native_message_refs "
        "(id, event_id, adapter, native_channel_id, native_message_id, "
        "direction, created_at) VALUES "
        "('alias-owner', 'owner-b', 'radio', 'wire', 'same', 'inbound', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await temp_storage.store_relation(
        "quoting-c",
        EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef("radio", "wire", "same"),
            key=None,
            fallback_text="B-only synthetic quote",
        ),
    )
    assert await temp_storage.resolve_native_ref("radio", "wire", "same") == "owner-b"
    a_ceiling = await _read_ceiling(temp_storage, "alias-a")
    a = await temp_storage.audit_content_ceilings(
        now=a_ceiling + timedelta(seconds=1), event_id="alias-a"
    )
    b = await temp_storage.audit_content_ceilings(
        now=a_ceiling + timedelta(seconds=1), event_id="owner-b"
    )
    assert a["residual_stores"].get("event_relation_excerpts:incoming") is None
    assert b["residual_stores"]["event_relation_excerpts:incoming"] == 1
    await temp_storage.enforce_content_ceilings(now=a_ceiling + timedelta(seconds=1))
    excerpt = await temp_storage._read_one(
        "SELECT excerpt FROM event_relation_excerpts"
    )
    assert excerpt is not None and excerpt["excerpt"] == "B-only synthetic quote"


async def test_native_reference_read_uses_clock_after_sqlite_lock_wait(
    temp_storage: SQLiteStorage, monkeypatch
) -> None:
    """A queued read must not release provider metadata after ceiling expiry."""
    import asyncio

    from medre.core.storage.sqlite import connection

    event_id = "ceiling-queued-native-read"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    await temp_storage._write(
        "INSERT INTO native_message_refs "
        "(id, event_id, adapter, native_message_id, direction, metadata, created_at) "
        "VALUES (?, ?, 'radio', 'native-1', 'outbound', ?, ?)",
        ("queued-nref", event_id, '{"provider":"private"}', _future(0).isoformat()),
    )
    clock = {"now": ceiling - timedelta(seconds=1)}
    monkeypatch.setattr(
        connection, "ceiling_read_time", lambda: clock["now"].isoformat()
    )
    temp_storage._lock.acquire()
    try:
        task = asyncio.create_task(temp_storage.list_native_refs_for_event(event_id))
        await asyncio.sleep(0.01)
        assert not task.done()
        clock["now"] = ceiling + timedelta(seconds=1)
    finally:
        temp_storage._lock.release()
    refs = await task
    assert len(refs) == 1
    assert refs[0].metadata == {}
