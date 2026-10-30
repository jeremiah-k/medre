"""Relation identity survives excerpt loss without inventing text."""

from __future__ import annotations

from medre.core.events import EventRelation
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_missing_excerpt_keeps_relation_identity_without_inventing_text(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "missing-excerpt.db"))
    await store.initialize()
    try:
        await store.append(
            make_storage_event(
                event_id="quote-lost",
                relations=(
                    EventRelation(
                        relation_type="reply",
                        target_event_id="target-1",
                        target_native_ref=None,
                        key=None,
                        fallback_text="original quote",
                    ),
                ),
            )
        )
        # Excerpt loss is out-of-band for identity: the relation row survives
        # and no read path may resurrect or invent the quoted text.
        await store._write(
            "DELETE FROM event_relation_excerpts WHERE relation_id="
            "(SELECT id FROM event_relations WHERE event_id='quote-lost')"
        )
        for read in (
            store.get("quote-lost"),
            store.get_envelope("quote-lost"),
        ):
            event = await read
            assert event is not None
            assert event.relations[0].target_event_id == "target-1"
            assert event.relations[0].fallback_text is None
    finally:
        await store.close()
