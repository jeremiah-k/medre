"""Adversarial tests for opaque content and SQL-level inspection boundaries."""

import sqlite3
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.events import (
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
)
from medre.core.events.metadata import NativeMetadata
from medre.core.evidence.collector import EvidenceCollector
from medre.core.storage.backend import DeliveryOutboxItem, EventFilter
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.timeline import assemble_event_timeline
from tests.helpers.storage import make_storage_event


@pytest.mark.parametrize("surface", ["payload", "native", "custom", "relation"])
async def test_opaque_event_content_has_expiring_source_authority(tmp_path, surface):
    store = SQLiteStorage(str(tmp_path / "opaque.db"))
    await store.initialize()
    event = msgspec.structs.replace(make_storage_event(event_id="opaque"), payload={})
    if surface == "payload":
        event = msgspec.structs.replace(event, payload={"private_copy": "secret"})
    elif surface == "native":
        event = msgspec.structs.replace(
            event, metadata=EventMetadata(native=NativeMetadata(data={"raw": "secret"}))
        )
    elif surface == "custom":
        event = msgspec.structs.replace(
            event, metadata=EventMetadata(custom={"quote": "secret"})
        )
    else:
        event = msgspec.structs.replace(
            event,
            relations=(EventRelation("reaction", "parent", None, "secret", None),),
        )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    try:
        await store.admit_ingress(event, None, "live", content_deadline=deadline)
        assert len(await store.list_mandatory_content_dependencies(event.event_id)) == 1
        assert (
            await store.read_event_for_replay(
                event.event_id, now=deadline - timedelta(seconds=1)
            )
        ).status == "retained"
        assert (
            await store.read_event_for_replay(event.event_id, now=deadline)
        ).status == "denied"
        assert (
            await store.read_event_for_replay(
                event.event_id, now=deadline - timedelta(seconds=1)
            )
        ).status == "denied"
        assert (
            "secret"
            not in msgspec.json.encode(
                await store.get_metadata(event.event_id)
            ).decode()
        )
        assert len([event async for event in store.query_metadata(EventFilter())]) == 1
    finally:
        await store.close()


async def test_operator_reads_never_select_sensitive_evidence_columns(tmp_path):
    store = SQLiteStorage(str(tmp_path / "inspection.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="inspected"))
        receipt = DeliveryReceipt(
            receipt_id="receipt",
            event_id="inspected",
            delivery_plan_id="plan",
            target_adapter="target",
            status="failed",
            error="SECRET-error",
            rendering_evidence='{"text":"SECRET-render"}',
            capability_reason="SECRET-reason",
        )
        await store.append_receipt(receipt)
        await store.store_native_ref(
            NativeMessageRef(
                id="ref",
                event_id="inspected",
                adapter="target",
                native_channel_id="room",
                native_message_id="native",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                metadata={"private": "SECRET-native"},
                created_at=datetime.now(timezone.utc),
            )
        )
        await store.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="outbox",
                event_id="inspected",
                delivery_plan_id="plan",
                route_id="route",
                target_adapter="target",
                metadata={"private": "SECRET-outbox"},
                error_summary="SECRET-failure",
            )
        )
        denied = {
            ("canonical_events", "payload"),
            ("delivery_receipts", "error"),
            ("delivery_receipts", "rendering_evidence"),
            ("delivery_receipts", "capability_reason"),
            ("native_message_refs", "metadata"),
            ("delivery_outbox", "metadata"),
            ("delivery_outbox", "error_summary"),
            ("delivery_outbox", "failure_kind_detail"),
        }

        def authorize(action, table, column, database, source):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and (table, column) in denied
                else sqlite3.SQLITE_OK
            )

        await store._run_in_thread(store._require_db().set_authorizer, authorize)
        try:
            bundle = await EvidenceCollector(store).collect_for_event("inspected")
            timeline = await assemble_event_timeline(store, "inspected")
            assert "SECRET" not in msgspec.json.encode(bundle).decode()
            assert "SECRET" not in msgspec.json.encode(timeline).decode()
        finally:
            await store._run_in_thread(store._require_db().set_authorizer, None)
        # Inspection withholds copies without modifying recovery evidence.
        assert (await store.list_receipts_for_event("inspected"))[
            0
        ].error == "SECRET-error"
    finally:
        await store.close()


async def test_reply_context_is_typed_scoped_and_expires(tmp_path):
    store = SQLiteStorage(str(tmp_path / "reply.db"))
    await store.initialize()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="parent"),
            payload={"body": "quote", "private_copy": "unrelated payload"},
        )
        await store.admit_ingress(event, None, "live", content_deadline=deadline)
        read = await store.read_reply_context(
            "parent", now=deadline - timedelta(seconds=1)
        )
        assert read.status == "retained"
        assert read.event.payload == {"body": "quote"}
        assert read.event.relations == ()
        denied = await store.read_reply_context("parent", now=deadline)
        assert denied.status == "denied" and denied.event is None
        absent = msgspec.structs.replace(
            make_storage_event(event_id="absent"),
            payload={"body": None, "text": "secondary"},
        )
        await store.admit_ingress(absent, None, "live")
        assert (
            await store.read_reply_context("absent", now=datetime.now(timezone.utc))
        ).status == "unavailable"
    finally:
        await store.close()


async def test_replay_rechecks_authority_between_stages(tmp_path, monkeypatch):
    from medre.core.engine.replay.engine import ReplayEngine
    from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult

    store = SQLiteStorage(str(tmp_path / "stages.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="stages"))
        engine = ReplayEngine(store)

        async def route(event, *, request):
            await store._write(
                "UPDATE event_content_dependencies SET released_at=? WHERE event_id=?",
                (
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    event.event_id,
                ),
            )
            return (
                ReplayResult(event_id=event.event_id, stage="route", status="passed"),
                [],
                None,
            )

        async def render(*args, **kwargs):
            pytest.fail("render ran with authority released by the preceding stage")

        monkeypatch.setattr(engine, "_stage_route", route)
        monkeypatch.setattr(engine, "_stage_render", render)
        results = [
            result
            async for result in engine.replay(
                ReplayRequest(correlation_ids=["stages"], mode=ReplayMode.DRY_RUN)
            )
        ]
        assert (
            next(result for result in results if result.stage == "route").status
            == "passed"
        )
        assert (
            next(result for result in results if result.stage == "render").error
            == "replay_content_authority_denied"
        )
    finally:
        await store.close()


async def test_appending_relation_cannot_recreate_a_missing_source_owner(tmp_path):
    store = SQLiteStorage(str(tmp_path / "no-resurrection.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="owner-lost"))
        await store._write(
            "DELETE FROM event_content_dependencies WHERE event_id='owner-lost'"
        )
        await store.store_relation(
            "owner-lost", EventRelation("reply", "parent", None, None, "new quote")
        )
        assert await store.list_mandatory_content_dependencies("owner-lost") == ()
        assert (
            await store.read_event_for_replay(
                "owner-lost", now=datetime.now(timezone.utc)
            )
        ).status == "denied"
    finally:
        await store.close()


async def test_full_read_rejects_an_inline_body_in_the_envelope(tmp_path):
    from medre.core.storage.backend import StorageError

    store = SQLiteStorage(str(tmp_path / "inline.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="inline"))
        await store._write(
            "UPDATE canonical_events SET payload=? WHERE event_id=?",
            ('{"body":"alternate model"}', "inline"),
        )
        assert (await store.get_metadata("inline")).payload == {}
        with pytest.raises(StorageError, match="Canonical envelope"):
            await store.read_event_for_replay("inline", now=datetime.now(timezone.utc))
    finally:
        await store.close()


async def test_expired_reply_preserves_native_identity_without_selecting_copies(
    tmp_path,
):
    from medre.core.planning.relation_enricher import RelationEnricher

    store = SQLiteStorage(str(tmp_path / "expired-quote.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="parent"))
        await store.store_native_ref(
            NativeMessageRef(
                id="parent-ref",
                event_id="parent",
                adapter="target",
                native_channel_id="room",
                native_message_id="native-parent",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                metadata={"copy": "private parent quote"},
            )
        )
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? WHERE event_id='parent'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        reply = msgspec.structs.replace(
            make_storage_event(event_id="reply"),
            relations=(EventRelation("reply", "parent", None, None, None),),
        )

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and (table, column) in {
                ("canonical_events", "payload"),
                ("native_message_refs", "metadata"),
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        await store._run_in_thread(store._require_db().set_authorizer, authorize)
        try:
            enriched = await RelationEnricher(store).enrich_for_target(
                reply, target_adapter="target", target_channel="room"
            )
            relation = enriched.relations[0]
            assert relation.target_fact.status == "bound"
            assert relation.target_native_ref.native_message_id == "native-parent"
            assert relation.fallback_text is None
            assert "original_text" not in relation.metadata
        finally:
            await store._run_in_thread(store._require_db().set_authorizer, None)
    finally:
        await store.close()


@pytest.mark.parametrize("formatted", [False, True])
async def test_reply_quote_preserves_canonical_text_selection(tmp_path, formatted):
    from medre.core.events.text import select_message_body

    store = SQLiteStorage(str(tmp_path / "quote-selection.db"))
    await store.initialize()
    try:
        payload = {"body": "styled body", "text": "plain alias"}
        if formatted:
            payload["formatting"] = {"version": 1, "spans": []}
        event = msgspec.structs.replace(
            make_storage_event(event_id="dual-text"), payload=payload
        )
        await store.append(event)
        read = await store.read_reply_context(event.event_id)
        assert read.status == "retained"
        assert select_message_body(read.event.payload) == select_message_body(payload)
        assert "spans" not in str(read.event.payload)
    finally:
        await store.close()
