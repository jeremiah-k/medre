"""Shared, synthetic ceiling-audit storage fixtures (no adapter dependencies)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


async def _install_elapsed_policy(store: SQLiteStorage, event_id: str) -> datetime:
    """Install one already-elapsed ceiling for a seeded test event.

    Test setup only: this writes the immutable policy row directly when the
    scenario needs an event whose admission did not carry a ceiling.
    """
    now = datetime.now(timezone.utc)
    await store._write(
        "INSERT INTO event_content_access_policies "
        "(event_id, version, hard_ceiling, allowed_consumers, "
        "max_optional_hold_microseconds, created_at) "
        "VALUES (?, 1, ?, '[]', 1, ?)",
        (
            event_id,
            (now - timedelta(seconds=1)).isoformat(timespec="microseconds"),
            now.isoformat(timespec="microseconds"),
        ),
    )
    return now


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
        "'error', ?, ?)",
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


async def _seed_late_write(store: SQLiteStorage, event_id: str, outbox_id: str) -> str:
    """A late completion write for the in-flight generation.

    The observation identity is derived from the outbox generation so a
    multi-generation test can seed two distinct residual findings.
    """
    observation_id = f"obs-late-{outbox_id}"
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await store._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES (?, ?, ?, 'radio', ?, 1, "
        "'delivered', ?, ?)",
        (
            observation_id,
            event_id,
            f"plan-{event_id}",
            outbox_id,
            '{"provider":"late"}',
            now,
        ),
    )
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('observation', ?, 1, "
        "'error', ?, ?)",
        (observation_id, f"late {MARKER}", now),
    )
    return observation_id


async def _count(store: SQLiteStorage, sql: str, params: tuple = ()) -> int:
    row = await store._read_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))
