"""Quote-attribution boundaries for the hard content privacy ceiling.

Two boundaries pin how an incoming quote excerpt is attributed when the
quoted target resolves outside the installation's own events:

- an unmatched native hint (no reference row, no canonical source identity)
  attributes the excerpt to no event: no other event's ceiling hides it and
  no other event's enforcement purges it; the quoting event's own ceiling
  governs it;
- one native identity claimed by two events through a NULL channel fails
  closed on both the read and the purge: expiry of any claiming event hides
  the excerpt, and the enforcement of any claiming event purges it.

Read and purge must agree in every phase; the residual battery is the
shared probe for that agreement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import medre.core.storage.sqlite.connection as connection
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_plain,
    _admit_with_ceiling,
    _count,
    _read_ceiling,
)

_UNQUOTED_HINT = ("matrix", "ghost-channel", "ghost-message")
_AMBIGUOUS_IDENTITY = ("radio", None, "shared-ambiguous")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


async def _insert_ref_row(
    store: SQLiteStorage,
    event_id: str,
    ref_id: str,
    adapter: str,
    channel_id: str | None,
    message_id: str,
) -> None:
    """Raw INSERT: the guarded native-ref statements merge duplicates, and
    this boundary needs two owner rows for one identity to exist at all."""
    await store._write(
        "INSERT INTO native_message_refs (id, event_id, adapter, "
        "native_channel_id, native_message_id, direction, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'inbound', '{}', ?)",
        (ref_id, event_id, adapter, channel_id, message_id, _now()),
    )


async def _insert_quoting_relation(
    store: SQLiteStorage,
    event_id: str,
    *,
    adapter: str,
    channel_id: str | None,
    message_id: str,
    excerpt: str,
) -> None:
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "target_native_adapter, target_native_channel_id, "
        "target_native_message_id, key, metadata, created_at) "
        "VALUES (?, 'reply', NULL, ?, ?, ?, NULL, '{}', ?)",
        (event_id, adapter, channel_id, message_id, _now()),
    )
    relation = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=? " "ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    assert relation is not None
    await store._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) " "VALUES (?, ?)",
        (relation["id"], excerpt),
    )


async def test_unmatched_native_hint_quote_is_governed_by_the_quoting_event(
    temp_storage: SQLiteStorage,
) -> None:
    """A quote of an identity no installation event claims waits for its
    quoting event's own ceiling and is untouched by another event's sweep."""
    quoting, unrelated = "boundary-hint-quoting", "boundary-hint-unrelated"
    await _admit_with_ceiling(temp_storage, quoting, seconds=7200)
    await _admit_with_ceiling(temp_storage, unrelated, seconds=3600)
    quoting_ceiling = await _read_ceiling(temp_storage, quoting)
    unrelated_ceiling = await _read_ceiling(temp_storage, unrelated)
    await _insert_quoting_relation(
        temp_storage,
        quoting,
        adapter=_UNQUOTED_HINT[0],
        channel_id=_UNQUOTED_HINT[1],
        message_id=_UNQUOTED_HINT[2],
        excerpt="alignment unmatched-hint excerpt",
    )

    # The unrelated event's sweep neither purges the excerpt nor counts it
    # as incoming: the battery agrees with the read that the identity
    # belongs to nobody.
    unrelated_elapsed = unrelated_ceiling + timedelta(seconds=1)
    audit = await temp_storage.audit_content_ceilings(
        now=unrelated_elapsed, event_id=unrelated
    )
    assert "event_relation_excerpts:incoming" not in audit["residual_stores"]
    await temp_storage.enforce_content_ceilings(now=unrelated_elapsed)
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_relation_excerpts",
        )
        == 1
    )

    # Before the quoting event's own ceiling the excerpt reads back intact.
    before = await temp_storage.read_event_for_ingress(
        quoting, now=quoting_ceiling - timedelta(seconds=1)
    )
    assert before.status == "retained" and before.event is not None
    assert [r.fallback_text for r in before.event.relations] == [
        "alignment unmatched-hint excerpt"
    ]

    # The quoting event's own ceiling denies the read and purges the
    # excerpt: admission installs the dependency deadline at the same
    # instant as the immutable ceiling.
    quoting_elapsed = quoting_ceiling + timedelta(seconds=1)
    after = await temp_storage.read_event_for_ingress(quoting, now=quoting_elapsed)
    assert after.status == "denied"
    pre = await temp_storage.audit_content_ceilings(
        now=quoting_elapsed, event_id=quoting
    )
    assert pre["residual_stores"]["event_relation_excerpts"] == 1
    await temp_storage.enforce_content_ceilings(now=quoting_elapsed)
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_relation_excerpts") == 0
    )
    post = await temp_storage.audit_content_ceilings(
        now=quoting_elapsed, event_id=quoting
    )
    assert post["residual_stores"] == {}


async def test_ambiguous_null_channel_native_identity_fails_closed(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One identity, two claiming events: any owner's expiry hides the
    excerpt and any owner's enforcement purges it (over-purge is
    fail-closed; expired content never stays readable)."""
    late_owner, early_owner = "boundary-owner-late", "boundary-owner-early"
    quoting = "boundary-ambiguous-quoting"
    await _admit_with_ceiling(temp_storage, late_owner, seconds=7200)
    await _admit_with_ceiling(temp_storage, early_owner, seconds=3600)
    early_ceiling = await _read_ceiling(temp_storage, early_owner)
    await _insert_ref_row(
        temp_storage, late_owner, "nref-owner-late", *_AMBIGUOUS_IDENTITY
    )
    await _insert_ref_row(
        temp_storage, early_owner, "nref-owner-early", *_AMBIGUOUS_IDENTITY
    )
    await _admit_plain(temp_storage, quoting)
    await _insert_quoting_relation(
        temp_storage,
        quoting,
        adapter=_AMBIGUOUS_IDENTITY[0],
        channel_id=_AMBIGUOUS_IDENTITY[1],
        message_id=_AMBIGUOUS_IDENTITY[2],
        excerpt="alignment ambiguous excerpt",
    )

    # Phase 1: with both owners unexpired the excerpt reads back intact.
    live = await temp_storage.read_event_for_ingress(quoting)
    assert live.status == "retained" and live.event is not None
    assert [r.fallback_text for r in live.event.relations] == [
        "alignment ambiguous excerpt"
    ]

    # Phase 2: once either claiming event's ceiling has elapsed, the read
    # hides the excerpt even though the quoting event itself is unexpired.
    # The hiding clause samples the registered read clock during statement
    # execution, not the caller's bound read instant.
    early_elapsed = early_ceiling + timedelta(seconds=1)
    real_clock = connection.ceiling_read_time

    def elapsed_clock() -> str:
        return early_elapsed.isoformat(timespec="microseconds")

    monkeypatch.setattr(connection, "ceiling_read_time", elapsed_clock)
    try:
        hidden = await temp_storage.read_event_for_ingress(
            quoting, now=datetime.now(timezone.utc)
        )
    finally:
        monkeypatch.setattr(connection, "ceiling_read_time", real_clock)
    assert hidden.status == "retained" and hidden.event is not None
    assert [r.fallback_text for r in hidden.event.relations] == [None]

    # Agreement: the battery counts the excerpt as incoming residual for the
    # expired claiming event exactly as the read hides it for that event.
    audit = await temp_storage.audit_content_ceilings(
        now=early_elapsed, event_id=early_owner
    )
    assert audit["residual_stores"]["event_relation_excerpts:incoming"] >= 1

    # Phase 3: the expired claiming event's enforcement purges the excerpt
    # even though the other claiming event is still unexpired; identity and
    # content of the surviving owner are untouched.
    await temp_storage.enforce_content_ceilings(now=early_elapsed)
    assert (
        await _count(temp_storage, "SELECT COUNT(*) FROM event_relation_excerpts") == 0
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM native_message_refs WHERE event_id=?",
            (late_owner,),
        )
        == 1
    )
    assert (
        await _count(
            temp_storage,
            "SELECT COUNT(*) FROM event_body_content WHERE event_id=?",
            (late_owner,),
        )
        == 1
    )
    post = await temp_storage.audit_content_ceilings(
        now=early_elapsed, event_id=early_owner
    )
    assert post["residual_stores"] == {}
