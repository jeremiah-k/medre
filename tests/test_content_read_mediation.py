"""Content must not escape through historical list/get/inspect APIs."""

from datetime import datetime, timezone

import msgspec

from medre.core.events import EventMetadata, EventRelation
from medre.core.events.metadata import NativeMetadata
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_metadata_reads_do_not_hydrate_content_or_opaque_metadata(tmp_path):
    store = SQLiteStorage(str(tmp_path / "metadata.db"))
    await store.initialize()
    relation = EventRelation(
        relation_type="reply",
        target_event_id="parent",
        target_native_ref=None,
        key="PRIVATE-EMOJI-KEY",
        fallback_text="a private quote",
        metadata={"secret": "relation bytes"},
    )
    event = msgspec.structs.replace(
        make_storage_event(event_id="private"),
        payload={
            "body": "private body",
            "text": "other body",
            "user_text": "nested secret",
        },
        relations=(relation,),
        metadata=EventMetadata(
            native=NativeMetadata(data={"raw": "private native bytes"}),
            custom={"private": "custom secrets"},
        ),
    )
    try:
        await store.admit_ingress(event, None, "live")
        for value in (
            await store.get("private"),
            await store.get_envelope("private"),
            await store.get_metadata("private"),
        ):
            assert value.payload == {}
            assert value.metadata.native is None
            assert value.metadata.custom == {}
            assert value.relations[0].key is None
            assert value.relations[0].fallback_text is None
            assert value.relations[0].metadata == {}
            assert "private" not in str(value.payload)
        assert (await store.list_relations("private"))[0].key is None
        assert (await store.list_relations("private"))[0].fallback_text is None
        assert (await store.list_relations("private"))[0].metadata == {}
        for method in (store.query, store.query_metadata):
            rows = [entry async for entry in method(EventFilter())]
            assert len(rows) == 1 and rows[0].payload == {}
            assert rows[0].relations[0].fallback_text is None
        owned = await store.read_event_for_ingress(
            "private", now=datetime.now(timezone.utc)
        )
        assert owned.status == "retained"
        assert owned.event.payload["body"] == "private body"
        assert owned.event.relations[0].fallback_text == "a private quote"
    finally:
        await store.close()


async def test_replay_does_not_extend_source_retention_or_resurrect_content(tmp_path):
    from datetime import timedelta

    from medre.core.engine.replay.engine import ReplayEngine
    from medre.core.engine.replay.types import ReplayMode, ReplayRequest

    store = SQLiteStorage(str(tmp_path / "replay.db"))
    await store.initialize()
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=3)
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="replay"), payload={"body": "secret"}
        )
        await store.admit_ingress(event, None, "live", content_deadline=deadline)
        before = await store.list_mandatory_content_dependencies("replay")
        owned = await store.read_event_for_replay(
            "replay", now=deadline - timedelta(seconds=1)
        )
        assert owned.status == "retained" and owned.event.payload["body"] == "secret"
        assert await store.list_mandatory_content_dependencies("replay") == before
        assert (
            await store.read_event_for_replay("replay", now=deadline)
        ).status == "denied"
        assert (await store.read_event_for_replay("replay", now=now)).status == "denied"
        results = [
            x
            async for x in ReplayEngine(store).replay(
                ReplayRequest(
                    correlation_ids=["replay"],
                    mode=ReplayMode.DRY_RUN,
                )
            )
        ]
        assert results[0].stage == "store" and results[0].status == "passed"
        assert all(
            x.status == "skipped" and x.error == "replay_content_authority_denied"
            for x in results[1:]
        )
    finally:
        await store.close()


async def test_metadata_projection_never_decodes_untrusted_payload_or_relation_metadata(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        await store.admit_ingress(make_storage_event(event_id="opaque"), None, "live")
        await store._write(
            "UPDATE canonical_events SET payload=?, metadata=? WHERE event_id=?",
            (
                "{broken-json",
                '{"routing":null,"native":{"data":{"x":"private"}}}',
                "opaque",
            ),
        )
        assert (await store.get_metadata("opaque")).payload == {}
        assert [x async for x in store.query_metadata(EventFilter())][0].payload == {}
    finally:
        await store.close()


async def test_replay_reaction_uses_an_admitted_source_hold(tmp_path):
    """Reaction payloads acquire authority even without a canonical body."""
    store = SQLiteStorage(str(tmp_path / "contentless.db"))
    await store.initialize()
    try:
        reaction = msgspec.structs.replace(
            make_storage_event(event_id="reaction"), payload={"key": "👍"}
        )
        await store.admit_ingress(reaction, None, "live")
        assert len(await store.list_mandatory_content_dependencies("reaction")) == 1
        read = await store.read_event_for_replay(
            "reaction", now=datetime.now(timezone.utc)
        )
        assert read.status == "retained"
        assert read.event is not None
        assert read.event.payload["key"] == "👍"
    finally:
        await store.close()
