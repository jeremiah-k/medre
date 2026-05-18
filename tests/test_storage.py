"""Tests for SQLiteStorage: append/get, native ref resolve, relations,
query with EventFilter, and close/reopen persistence.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from medre.core.events import (
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.storage import EventFilter, SQLiteStorage

from tests.helpers.storage import make_storage_event


# ===================================================================
# Append / Get round-trip
# ===================================================================


class TestAppendAndGet:
    """append() then get() must return an equivalent event."""

    async def test_append_and_get_round_trip(self, temp_storage: SQLiteStorage) -> None:
        event = make_storage_event()
        await temp_storage.append(event)
        retrieved = await temp_storage.get(event.event_id)
        assert retrieved is not None
        assert retrieved.event_id == event.event_id
        assert retrieved.event_kind == event.event_kind
        assert retrieved.payload == event.payload

    async def test_get_returns_none_for_unknown_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        result = await temp_storage.get("does-not-exist")
        assert result is None


# ===================================================================
# Native ref storage and resolution
# ===================================================================


class TestNativeRef:
    """store_native_ref / resolve_native_ref."""

    async def test_store_and_resolve_native_ref(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After storing a native ref, resolve returns the canonical event_id."""
        # Must have the event first for FK constraint.
        event = make_storage_event(event_id="evt-ref-1")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-1",
            event_id="evt-ref-1",
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="native-msg-10",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        resolved = await temp_storage.resolve_native_ref(
            "fake_transport", "ch-0", "native-msg-10"
        )
        assert resolved == "evt-ref-1"

    async def test_resolve_native_ref_returns_none_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        result = await temp_storage.resolve_native_ref(
            "unknown_adapter", None, "no-msg"
        )
        assert result is None


# ===================================================================
# Relations storage
# ===================================================================


class TestRelations:
    """store_relation / list_relations."""

    async def test_store_and_list_relations(self, temp_storage: SQLiteStorage) -> None:
        event = make_storage_event(event_id="evt-rel-1")
        await temp_storage.append(event)

        nref = NativeRef(
            adapter="discord",
            native_channel_id="room-1",
            native_message_id="msg-1",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-evt-1",
            target_native_ref=nref,
            key=None,
            fallback_text="original",
        )
        await temp_storage.store_relation("evt-rel-1", relation)

        relations = await temp_storage.list_relations("evt-rel-1")
        assert len(relations) == 1
        assert relations[0].relation_type == "reply"
        assert relations[0].target_event_id == "target-evt-1"
        assert relations[0].target_native_ref is not None
        assert relations[0].target_native_ref.adapter == "discord"

    async def test_list_relations_returns_empty_for_no_relations(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-no-rel")
        await temp_storage.append(event)

        relations = await temp_storage.list_relations("evt-no-rel")
        assert relations == []

    async def test_inline_relations_stored_on_append(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Relations embedded in an event are stored on append."""
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="target-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = make_storage_event(event_id="evt-inline", relations=(relation,))
        await temp_storage.append(event)

        stored = await temp_storage.list_relations("evt-inline")
        assert len(stored) == 1
        assert stored[0].relation_type == "reaction"
        assert stored[0].key == "👍"


# ===================================================================
# Query with EventFilter
# ===================================================================


class TestQuery:
    """query() with EventFilter by event_kind, source_adapter, and limit."""

    async def _seed_events(self, storage: SQLiteStorage) -> None:
        events = [
            make_storage_event(event_id="q-1", event_kind="message.created"),
            make_storage_event(event_id="q-2", event_kind="message.text"),
            make_storage_event(event_id="q-3", event_kind="telemetry.received"),
            make_storage_event(
                event_id="q-4",
                event_kind="message.created",
                source_adapter="other_adapter",
            ),
        ]
        for e in events:
            await storage.append(e)

    async def test_query_by_event_kind(self, temp_storage: SQLiteStorage) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(event_kinds=["message.created"])
        results = [e async for e in temp_storage.query(filt)]
        ids = {e.event_id for e in results}
        assert ids == {"q-1", "q-4"}

    async def test_query_by_source_adapter(self, temp_storage: SQLiteStorage) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(source_adapters=["other_adapter"])
        results = [e async for e in temp_storage.query(filt)]
        assert len(results) == 1
        assert results[0].event_id == "q-4"

    async def test_query_with_limit(self, temp_storage: SQLiteStorage) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(limit=2)
        results = [e async for e in temp_storage.query(filt)]
        assert len(results) == 2

    async def test_query_returns_empty_when_no_match(
        self, temp_storage: SQLiteStorage
    ) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(event_kinds=["nonexistent.kind"])
        results = [e async for e in temp_storage.query(filt)]
        assert results == []


# ===================================================================
# Close / Reopen persistence
# ===================================================================


class TestPersistence:
    """Data survives close() and re-initialize()."""

    async def test_reopen_reads_existing_events(self) -> None:
        """Events written before close() are readable after reopen."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Write phase.
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()
            event = make_storage_event(event_id="persist-1")
            await storage.append(event)
            await storage.close()

            # Read phase — new storage instance on same file.
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()
            retrieved = await storage2.get("persist-1")
            assert retrieved is not None
            assert retrieved.event_id == "persist-1"
            assert retrieved.event_kind == event.event_kind
            await storage2.close()
        finally:
            os.unlink(db_path)
