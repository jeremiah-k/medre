# medre-test-tier: compatibility
"""Resolved-target reaction quoting after the target's ingress completes.

A described reaction's preview comes from core relation enrichment only;
the enricher's Phase 2 reads the target through the authorized
``read_reply_context`` quote read. That read borrows either a live source
hold or a previously issued bounded ``reply_quotes`` body grant. Durable
ingress completion releases the source hold (content-retention spec:
terminal settlement), and a realistic reaction arrives only after its
target was fully processed — so the quote can only work when admission
itself issued the bounded grant while the source owner was still live.

Contract locked here:

* Ceiling-governed admission allowlists ``reply_quotes`` and issues one
  exact bounded grant in the admission transaction, so a resolved-target
  reaction delivered after ingress completion quotes the target text.
* Without a configured ceiling no grant exists and the described reaction
  degrades honestly to ``an earlier message`` (never to an empty quote).
* A target whose stored event is absent degrades the same way even when
  the grant machinery is fully provisioned.
* The grant is bounded: it expires with the event ceiling and is honestly
  revocable; after either, quoting degrades again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import msgspec

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.planning.relation_enricher import RelationEnricher
from medre.core.rendering.renderer import RenderingContext
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.matrix import matrix_payload_content as _payload_content
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.storage import make_storage_event

_ROOM = "!XFIPQjPsEzStODRDVw:lab.test"
_MATRIX = "medre"
_SEED_TEXT = "NREACT seed target line"
_KEY = "\U0001f984"


async def _admit_and_complete(
    storage: SQLiteStorage,
    event: CanonicalEvent,
    *,
    deadline: datetime | None = None,
    worker_id: str = "worker",
) -> None:
    """Admit through the durable runtime path and finish the work.

    Completion settles the ingress content dependency in the same
    transaction as the lifecycle state, exactly as the durable worker
    does after fanout planning.
    """
    await storage.admit_ingress(event, None, "live", content_deadline=deadline)
    [work] = await storage.claim_ingress_work(worker_id=worker_id, limit=1)
    assert work.event_id == event.event_id
    assert await storage.complete_ingress_work(event.event_id, worker_id=worker_id)


def _seed_event(event_id: str) -> CanonicalEvent:
    return make_storage_event(
        event_id=event_id,
        payload={"body": _SEED_TEXT},
        source_adapter=_MATRIX,
    )


def _inbound_ref(event_id: str) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-inbound-{event_id}",
        event_id=event_id,
        adapter=_MATRIX,
        native_channel_id=_ROOM,
        native_message_id="$seed-matrix-event",
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        created_at=datetime.now(timezone.utc),
    )


def _reaction_event(target_event_id: str | None) -> CanonicalEvent:
    """A radio-sourced reaction whose relation mirrors the resolved lab shape.

    The relation carries the resolved canonical target id AND the
    matrix-native ref (the product contract variable the normal topology
    hints), so Phase 1 native-ref work is a no-op and Phase 2 is the only
    possible text source. ``target_event_id=None`` models the unobserved
    target class; a stored-but-absent target id models a target whose
    canonical row is gone.
    """
    relation = EventRelation(
        relation_type="reaction",
        target_event_id=target_event_id,
        target_native_ref=(
            NativeRef(
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$seed-matrix-event",
            )
            if target_event_id is not None
            else None
        ),
        key=_KEY,
        fallback_text=None,
        metadata={},
    )
    return CanonicalEvent(
        event_id="reaction-1",
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="radio-c",
        source_transport_id="!f2746318",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(relation,),
        payload={"body": ""},
        metadata=EventMetadata(),
    )


async def _render_reaction_body(
    storage: SQLiteStorage, reaction: CanonicalEvent
) -> str:
    """Enrich for the matrix destination and return the described body."""
    enriched = await RelationEnricher(storage).enrich_for_target(
        reaction,
        target_adapter=_MATRIX,
        target_channel=_ROOM,
    )
    renderer = MatrixRenderer(
        configs={_MATRIX: _StubMatrixConfig(adapter_id=_MATRIX, relay_prefix="")}
    )
    result = await renderer.render(
        enriched,
        RenderingContext(
            target_adapter=_MATRIX,
            target_channel=_ROOM,
            delivery_strategy="direct",
        ),
    )
    return str(_payload_content(result)["body"])


async def test_resolved_target_quotes_preview_after_ingress_completes(
    tmp_path,
) -> None:
    """The lab defect: the ref resolves, the target was processed long ago,
    and the described reaction must still quote the actual target text."""
    store = SQLiteStorage(str(tmp_path / "quote.db"))
    await store.initialize()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        await _admit_and_complete(store, _seed_event("seed-1"), deadline=deadline)

        body = await _render_reaction_body(store, _reaction_event("seed-1"))
        assert f'reacted {_KEY} to "{_SEED_TEXT}"' in body
        assert "an earlier message" not in body
    finally:
        await store.close()


async def test_unceilinged_completed_target_still_degrades_honestly(
    tmp_path,
) -> None:
    """No configured ceiling means no issued grant: after ingress completion
    the quote is unavailable and the wording stays honest."""
    store = SQLiteStorage(str(tmp_path / "no-ceiling.db"))
    await store.initialize()
    try:
        await _admit_and_complete(store, _seed_event("seed-2"))

        body = await _render_reaction_body(store, _reaction_event("seed-2"))
        assert f"reacted {_KEY} to an earlier message" in body
        assert 'to ""' not in body
        assert _SEED_TEXT not in body
    finally:
        await store.close()


async def test_absent_target_still_degrades_honestly(tmp_path) -> None:
    """A target id with no stored canonical event degrades even with the
    grant machinery fully provisioned."""
    store = SQLiteStorage(str(tmp_path / "absent.db"))
    await store.initialize()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        await _admit_and_complete(store, _seed_event("seed-3"), deadline=deadline)

        body = await _render_reaction_body(store, _reaction_event("missing-target"))
        assert f"reacted {_KEY} to an earlier message" in body
        assert 'to ""' not in body
    finally:
        await store.close()


async def test_quote_expires_with_the_ceiling(tmp_path) -> None:
    """The issued grant is bounded by the event ceiling: at the ceiling the
    quote read denies again (deterministic clock, no sleeps)."""
    store = SQLiteStorage(str(tmp_path / "bounded.db"))
    await store.initialize()
    try:
        admitted_at = datetime.now(timezone.utc)
        deadline = admitted_at + timedelta(hours=24)
        await _admit_and_complete(store, _seed_event("seed-4"), deadline=deadline)

        before = await store.read_reply_context(
            "seed-4", now=deadline - timedelta(seconds=1)
        )
        assert before.status == "retained"
        assert before.event is not None
        assert before.event.payload["body"] == _SEED_TEXT

        at_ceiling = await store.read_reply_context("seed-4", now=deadline)
        assert at_ceiling.status == "denied"
        assert at_ceiling.event is None
    finally:
        await store.close()


async def test_revoked_grant_degrades_honestly(tmp_path) -> None:
    """Revocation is honest: after an explicit revoke the described reaction
    degrades instead of quoting."""
    store = SQLiteStorage(str(tmp_path / "revoked.db"))
    await store.initialize()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        await _admit_and_complete(store, _seed_event("seed-5"), deadline=deadline)
        assert await store.revoke_optional_body_hold(
            "seed-5", "reply_quotes", now=datetime.now(timezone.utc)
        )

        body = await _render_reaction_body(store, _reaction_event("seed-5"))
        assert f"reacted {_KEY} to an earlier message" in body
        assert 'to ""' not in body
    finally:
        await store.close()


async def test_ceiling_admission_issues_bounded_reply_quote_grant(
    tmp_path,
) -> None:
    """Admission contract: the ceiling row allowlists exactly the
    ``reply_quotes`` consumer and one exact bounded grant is issued in the
    same transaction — never any other consumer, never an unbounded grant."""
    store = SQLiteStorage(str(tmp_path / "grant.db"))
    await store.initialize()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        await store.admit_ingress(
            _seed_event("seed-6"), None, "live", content_deadline=deadline
        )

        policy = await store._read_one(
            "SELECT version, hard_ceiling, allowed_consumers, "
            "max_optional_hold_microseconds FROM event_content_access_policies "
            "WHERE event_id='seed-6'"
        )
        assert policy is not None
        assert policy["version"] == 1
        assert policy["hard_ceiling"] == deadline.isoformat(timespec="microseconds")
        assert policy["allowed_consumers"] == '["reply_quotes"]'

        grant = await store._read_one(
            "SELECT scope, consumer, issued_at, expires_at, revoked_at, expired_at "
            "FROM event_content_access_grants WHERE event_id='seed-6'"
        )
        assert grant is not None
        assert grant["scope"] == "body"
        assert grant["consumer"] == "reply_quotes"
        assert grant["revoked_at"] is None
        assert grant["expired_at"] is None
        assert grant["expires_at"] == deadline.isoformat(timespec="microseconds")
        assert grant["issued_at"] < grant["expires_at"]
    finally:
        await store.close()


async def test_duplicate_admission_keeps_the_issued_grant(tmp_path) -> None:
    """Duplicate native admission returns the original event without
    disturbing the already-issued grant."""
    store = SQLiteStorage(str(tmp_path / "dup.db"))
    await store.initialize()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        seed = _seed_event("seed-7")
        inbound = _inbound_ref("seed-7")
        first = await store.admit_ingress(
            seed, inbound, "live", content_deadline=deadline
        )
        assert first.created is True

        imposter = msgspec.structs.replace(_seed_event("seed-7-imposter"))
        second = await store.admit_ingress(
            imposter, _inbound_ref("seed-7-imposter"), "live", content_deadline=deadline
        )
        assert second.created is False
        assert second.event_id == "seed-7"

        grant = await store._read_one(
            "SELECT consumer FROM event_content_access_grants WHERE event_id='seed-7'"
        )
        assert grant is not None
        assert grant["consumer"] == "reply_quotes"
    finally:
        await store.close()
