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
        key=None,
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
            assert value.relations[0].fallback_text is None
            assert value.relations[0].metadata == {}
            assert "private" not in str(value.payload)
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
