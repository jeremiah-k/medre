"""Additional hard-ceiling read, purge and retained-summary contract probes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import medre.core.storage.sqlite.connection as connection
from medre.core.events import NativeMessageRef
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.storage.sqlite._ceiling_enforcement import (
    _UNREADABLE_RADIO_PARTS,
    _retain_radio_evidence_summary,
)
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_plain,
    _admit_with_ceiling,
    _install_elapsed_policy,
)
from tests.helpers.handoff_resolution import _observation, _seed_reserved_claim


async def test_ceiling_is_checked_during_sql_execution_not_earlier_binding(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SQL lock wait cannot reuse a still-valid read clock from before T."""
    event_id = "ceiling-sql-clock"
    await _admit_with_ceiling(temp_storage, event_id)
    now = datetime.now(timezone.utc)
    ceiling = now + timedelta(seconds=20)
    await temp_storage._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        (ceiling.isoformat(timespec="microseconds"), event_id),
    )
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="clock-native-ref",
            event_id=event_id,
            adapter="matrix",
            native_channel_id="room",
            native_message_id="message",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            metadata={"secret": "provider-text"},
        )
    )
    calls = 0

    def before_then_after() -> str:
        nonlocal calls
        calls += 1
        return (ceiling + timedelta(seconds=(-1 if calls == 1 else 1))).isoformat(
            timespec="microseconds"
        )

    monkeypatch.setattr(connection, "ceiling_read_time", before_then_after)
    ref = await temp_storage.get_native_ref("matrix", "room", "message")
    assert ref is not None and ref.event_id == event_id
    assert ref.metadata == {}
    assert calls >= 2  # Bind occurred before SQLite evaluated its policy row.
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM event_content_ceiling_log WHERE event_id=?", (event_id,)
        )
        is None
    )


async def test_purge_removes_render_input_hash_and_audits_it(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage._write(
        "UPDATE delivery_outbox SET payload_hash=? WHERE outbox_id=?",
        ("private-render-input-digest", stored.outbox_id),
    )
    report = await temp_storage.enforce_content_ceilings(now=now)
    assert any(item.event_id == provenance.event_id for item in report)
    row = await temp_storage._read_one(
        "SELECT payload_hash FROM delivery_outbox WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert row is not None and row["payload_hash"] is None
    inspected = await temp_storage.verify_content_ceiling_receipts(
        event_id=provenance.event_id
    )
    assert inspected["verdict"] == "passed"


async def test_legacy_non_object_observation_is_scrubbed_not_accepted(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation("legacy-list-packet", provenance)
    )
    await temp_storage._write(
        "UPDATE delivery_observations SET metadata='[]' WHERE observation_id=?",
        ("legacy-list-packet",),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    assert await temp_storage.enforce_content_ceilings(now=now)
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id=?",
        ("legacy-list-packet",),
    )
    scope = await temp_storage._read_one(
        "SELECT scope_json FROM delivery_observation_scopes WHERE observation_id=?",
        ("legacy-list-packet",),
    )
    assert row is not None and row["metadata"] == "{}"
    assert scope is not None and json.loads(scope["scope_json"]) == {
        "radio_packet": None
    }
    assert (await temp_storage.get_outbox_item(stored.outbox_id)) is not None


def test_conflicting_radio_provenance_is_never_rewritten() -> None:
    original = DeliveryAttemptProvenance(
        event_id="event",
        delivery_plan_id="plan",
        target_adapter="radio",
        target_channel="channel",
        outbox_id="obox",
        attempt_number=1,
        source="live",
    )
    contradicted = DeliveryAttemptProvenance(
        event_id="event",
        delivery_plan_id="plan",
        target_adapter="radio",
        target_channel="channel",
        outbox_id="obox",
        attempt_number=1,
        source="retry",
    )
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA + _INDEXES)
        parts = [{"ordinal": 1, "state": "accepted", "handoff_id": "origin"}]
        _retain_radio_evidence_summary(db, original, parts, 4)
        before = db.execute(
            "SELECT provenance_json FROM radio_attempt_evidence_summaries"
        ).fetchone()[0]
        _retain_radio_evidence_summary(db, contradicted, parts, 5)
        row = db.execute(
            "SELECT provenance_json, parts_json, revision FROM radio_attempt_evidence_summaries"
        ).fetchone()
        assert row["provenance_json"] == before
        assert row["parts_json"] == _UNREADABLE_RADIO_PARTS
        assert row["revision"] == 6
        from medre.core.delivery_authority import DeliveryIdentity
        from medre.core.evidence.handoff_resolution import resolve_uncertain_handoff
        from medre.core.rendering.radio_progress import RadioPartProgress
        from medre.core.storage.radio import RadioAttemptEvidenceSummary

        summary = RadioAttemptEvidenceSummary(
            original,
            tuple(RadioPartProgress(**part) for part in json.loads(row["parts_json"])),
            row["revision"],
        )
        verdict = resolve_uncertain_handoff(
            outbox_id="obox",
            attempt_number=1,
            identity=DeliveryIdentity("event", "plan", "radio", "channel"),
            checkpoint=summary,
            observations=[],
        )
        assert verdict.verdict == "unresolved"
        _retain_radio_evidence_summary(db, original, parts, 4)
        assert db.execute(
            "SELECT parts_json, revision FROM radio_attempt_evidence_summaries"
        ).fetchone()[:] == (_UNREADABLE_RADIO_PARTS, 6)


async def test_incoming_quote_read_cannot_outlive_quoted_events_ceiling(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B retains its own authority but never reads A's expired quoted text."""
    from tests.helpers.storage import make_storage_event

    owner, neighbor = "quoted-expired-owner", "quoted-live-neighbor"
    await _admit_with_ceiling(temp_storage, owner)
    await temp_storage.admit_ingress(
        make_storage_event(event_id=neighbor, payload={"text": "live neighbor"}),
        None,
        "live",
    )
    ceiling_row = await temp_storage._read_one(
        "SELECT hard_ceiling FROM event_content_access_policies WHERE event_id=?",
        (owner,),
    )
    assert ceiling_row is not None
    ceiling = datetime.fromisoformat(ceiling_row["hard_ceiling"])
    await temp_storage._write(
        "UPDATE canonical_events SET source_native_adapter='matrix', "
        "source_native_channel_id='room', source_native_message_id='native-owner' "
        "WHERE event_id=?",
        (owner,),
    )
    for suffix, target in (("direct", True), ("native", False)):
        await temp_storage._write(
            "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
            "target_native_adapter, target_native_channel_id, "
            "target_native_message_id, metadata, created_at) "
            "VALUES (?, 'reply', ?, ?, ?, ?, '{}', ?)",
            (
                neighbor,
                owner if target else None,
                None if target else "matrix",
                None if target else "room",
                None if target else "native-owner",
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ),
        )
        await temp_storage._write(
            "INSERT INTO event_relation_excerpts (relation_id, excerpt) "
            "VALUES (last_insert_rowid(), ?)",
            (f"private-{suffix}",),
        )
    before = await temp_storage.read_event_for_ingress(
        neighbor, now=ceiling - timedelta(seconds=1)
    )
    assert before.status == "retained" and before.event is not None
    assert [r.fallback_text for r in before.event.relations] == [
        "private-direct",
        "private-native",
    ]
    # Denial evaluates the quoted target's ceiling while the statement runs:
    # a read bound before the ceiling cannot expose the excerpt after it.
    real_clock = connection.ceiling_read_time

    def elapsed_clock() -> str:
        return (ceiling + timedelta(seconds=1)).isoformat(timespec="microseconds")

    monkeypatch.setattr(connection, "ceiling_read_time", elapsed_clock)
    try:
        after = await temp_storage.read_event_for_ingress(
            neighbor, now=ceiling - timedelta(seconds=1)
        )
        assert after.status == "retained" and after.event is not None
        assert [r.fallback_text for r in after.event.relations] == [None, None]
    finally:
        monkeypatch.setattr(connection, "ceiling_read_time", real_clock)
    assert after.event.payload["text"] == "live neighbor"
    rows = await temp_storage._read_all(
        "SELECT excerpt FROM event_relation_excerpts ORDER BY relation_id"
    )
    assert {r["excerpt"] for r in rows} == {"private-direct", "private-native"}


async def test_quoted_excerpt_denies_at_sql_execution_time(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read stalled across the quoted target's ceiling hides the excerpt.

    The excerpt denial evaluates the quoted target's ceiling while the
    statement runs, not from the caller's earlier bound read clock, and no
    enforcement receipt is needed for the denial.
    """
    target_id = "excerpt-ceiling-target"
    neighbor_id = "excerpt-ceiling-neighbor"
    await _admit_with_ceiling(temp_storage, target_id, seconds=3600)
    await _admit_plain(temp_storage, neighbor_id)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    await temp_storage._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', ?, NULL, '{}', ?)",
        (neighbor_id, target_id, now),
    )
    relation = await temp_storage._read_one(
        "SELECT id FROM event_relations WHERE event_id=?", (neighbor_id,)
    )
    await temp_storage._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) VALUES (?, ?)",
        (relation["id"], "quoted synthetic excerpt text"),
    )

    bound_before_ceiling = datetime.now(timezone.utc)
    read = await temp_storage.read_event_for_ingress(
        neighbor_id, now=bound_before_ceiling
    )
    assert read.status == "retained" and read.event is not None
    assert read.event.relations[0].fallback_text is not None

    ceiling = datetime.now(timezone.utc) + timedelta(seconds=3600)
    real_clock = connection.ceiling_read_time

    def stalled_clock() -> str:
        return (ceiling + timedelta(seconds=1)).isoformat(timespec="microseconds")

    monkeypatch.setattr(connection, "ceiling_read_time", stalled_clock)
    try:
        stalled = await temp_storage.read_event_for_ingress(
            neighbor_id, now=bound_before_ceiling
        )
        assert stalled.status == "retained" and stalled.event is not None
        # The bound pre-ceiling clock no longer governs the excerpt: the
        # statement evaluated the quoted target's elapsed ceiling.
        assert stalled.event.relations[0].fallback_text is None
    finally:
        monkeypatch.setattr(connection, "ceiling_read_time", real_clock)
    # Execution-time denial never records an enforcement receipt.
    row = await temp_storage._read_one(
        "SELECT 1 FROM event_content_ceiling_log WHERE event_id=?", (target_id,)
    )
    assert row is None
