"""Tests for SQLiteStorage: idempotent native refs, source_native_ref round-trip,
relation target_native_thread_id, and NULL channel native ref idempotency.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.storage import SQLiteStorage

from tests.helpers.storage import make_storage_event


# ===================================================================
# Idempotent native refs
# ===================================================================


class TestIdempotentNativeRef:
    """store_native_ref with duplicate (adapter, channel, message) is idempotent."""

    async def test_store_same_ref_twice_is_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing the same native ref twice must not raise and must resolve."""
        event = make_storage_event(event_id="evt-idem-1")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-idem-1",
            event_id="evt-idem-1",
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="msg-dup",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)
        # Second store with identical (adapter, native_channel_id, native_message_id).
        await temp_storage.store_native_ref(ref)

        resolved = await temp_storage.resolve_native_ref(
            "fake_transport", "ch-0", "msg-dup"
        )
        assert resolved == "evt-idem-1"

    async def test_store_different_refs_same_event_allowed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple distinct native refs pointing to the same event are allowed."""
        event = make_storage_event(event_id="evt-multi-ref")
        await temp_storage.append(event)

        ref_a = NativeMessageRef(
            id="nref-mr-a",
            event_id="evt-multi-ref",
            adapter="adapter_a",
            native_channel_id="ch-a",
            native_message_id="msg-a",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        ref_b = NativeMessageRef(
            id="nref-mr-b",
            event_id="evt-multi-ref",
            adapter="adapter_b",
            native_channel_id="ch-b",
            native_message_id="msg-b",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        assert (
            await temp_storage.resolve_native_ref("adapter_a", "ch-a", "msg-a")
            == "evt-multi-ref"
        )
        assert (
            await temp_storage.resolve_native_ref("adapter_b", "ch-b", "msg-b")
            == "evt-multi-ref"
        )

    async def test_missing_native_ref_returns_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Resolving a native ref that was never stored returns None."""
        result = await temp_storage.resolve_native_ref(
            "no_such_adapter", None, "no_such_msg"
        )
        assert result is None


# ===================================================================
# source_native_ref round-trip
# ===================================================================


class TestSourceNativeRefRoundTrip:
    """Events with / without source_native_ref round-trip through storage."""

    async def test_event_without_source_native_ref_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Event with source_native_ref=None survives append/get."""
        event = make_storage_event(event_id="evt-no-snr")
        await temp_storage.append(event)
        retrieved = await temp_storage.get("evt-no-snr")
        assert retrieved is not None
        assert retrieved.source_native_ref is None

    async def test_event_with_source_native_ref_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Event with populated source_native_ref survives append/get."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event-001",
            native_thread_id=None,
        )
        event = CanonicalEvent(
            event_id="evt-with-snr",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix",
            source_transport_id="node-1",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )
        await temp_storage.append(event)
        retrieved = await temp_storage.get("evt-with-snr")
        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.adapter == "matrix"
        assert retrieved.source_native_ref.native_channel_id == "!room:server"
        assert retrieved.source_native_ref.native_message_id == "$event-001"
        assert retrieved.source_native_ref.native_thread_id is None

    async def test_inbound_native_ref_duplicate_is_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing the same inbound NativeMessageRef twice is idempotent."""
        event = make_storage_event(event_id="evt-inbound-idem")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="evt-inbound-idem",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)
        # Second store with same (adapter, native_channel_id, native_message_id) is silently ignored.
        ref2 = NativeMessageRef(
            id="nref-inbound-1-dup",
            event_id="evt-inbound-idem",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref2)

        resolved = await temp_storage.resolve_native_ref(
            "matrix", "!room:server", "$msg-001"
        )
        assert resolved == "evt-inbound-idem"

    async def test_resolve_native_ref_returns_event_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_native_ref(adapter, channel, message_id) returns canonical event_id."""
        event = make_storage_event(event_id="evt-resolve-target")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-resolve-1",
            event_id="evt-resolve-target",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$target-msg",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        result = await temp_storage.resolve_native_ref(
            "matrix", "!room:server", "$target-msg"
        )
        assert result == "evt-resolve-target"


# ===================================================================
# target_native_thread_id round-trip in event_relations
# ===================================================================


class TestRelationTargetNativeThreadId:
    """target_native_ref.native_thread_id is preserved through storage."""

    async def test_target_native_thread_id_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A relation whose target_native_ref has native_thread_id survives
        append/get round-trip without silent data loss."""
        event = make_storage_event(event_id="evt-thread-rt")
        await temp_storage.append(event)

        nref = NativeRef(
            adapter="discord",
            native_channel_id="channel-1",
            native_message_id="msg-thread-1",
            native_thread_id="thread-42",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        await temp_storage.store_relation("evt-thread-rt", relation)

        relations = await temp_storage.list_relations("evt-thread-rt")
        assert len(relations) == 1
        tnref = relations[0].target_native_ref
        assert tnref is not None
        assert tnref.native_thread_id == "thread-42"
        assert tnref.adapter == "discord"
        assert tnref.native_channel_id == "channel-1"
        assert tnref.native_message_id == "msg-thread-1"

    async def test_inline_relation_with_thread_id_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Relations with native_thread_id embedded in an event round-trip."""
        nref = NativeRef(
            adapter="slack",
            native_channel_id="C123",
            native_message_id="123.456",
            native_thread_id="thread-abc",
        )
        relation = EventRelation(
            relation_type="thread",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        event = make_storage_event(event_id="evt-inline-thread", relations=(relation,))
        await temp_storage.append(event)

        retrieved = await temp_storage.get("evt-inline-thread")
        assert retrieved is not None
        assert len(retrieved.relations) == 1
        assert retrieved.relations[0].target_native_ref is not None
        assert retrieved.relations[0].target_native_ref.native_thread_id == "thread-abc"


# ===================================================================
# NULL channel native ref idempotency
# ===================================================================


class TestNullChannelNativeRefIdempotency:
    """Native refs with native_channel_id=None dedupe deterministically."""

    async def test_null_channel_ref_stores_once(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing two native refs with NULL channel and same message_id
        results in a single stored row (deterministic idempotency)."""
        event = make_storage_event(event_id="evt-null-ch")
        await temp_storage.append(event)

        ref1 = NativeMessageRef(
            id="nref-null-1",
            event_id="evt-null-ch",
            adapter="radio",
            native_channel_id=None,
            native_message_id="msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(timezone.utc),
        )
        await temp_storage.store_native_ref(ref1)

        # Second ref with same (adapter, NULL, native_message_id) but different id.
        ref2 = NativeMessageRef(
            id="nref-null-2",
            event_id="evt-null-ch",
            adapter="radio",
            native_channel_id=None,
            native_message_id="msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(timezone.utc),
        )
        await temp_storage.store_native_ref(ref2)

        # Should resolve to the first ref's event_id.
        resolved = await temp_storage.resolve_native_ref("radio", None, "msg-001")
        assert resolved == "evt-null-ch"

        # Only one row should exist.
        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND native_channel_id IS NULL AND native_message_id = ?",
            ("radio", "msg-001"),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "nref-null-1"
