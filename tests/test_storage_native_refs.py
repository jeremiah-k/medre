"""Tests for SQLiteStorage: idempotent native refs, source_native_ref round-trip,
relation target_native_thread_id, NULL channel native ref idempotency,
list_native_refs_for_event, and RelationResolver integration.
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
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.storage.sqlite.storage import SQLiteStorage
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

    async def test_null_channel_resolve_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A native ref with native_channel_id=None can be stored and resolved."""
        event = make_storage_event(event_id="evt-null-rt")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-null-rt",
            event_id="evt-null-rt",
            adapter="meshcore",
            native_channel_id=None,
            native_message_id="pkt-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        resolved = await temp_storage.resolve_native_ref("meshcore", None, "pkt-42")
        assert resolved == "evt-null-rt"

        # Also confirm that a non-matching adapter returns None.
        assert await temp_storage.resolve_native_ref("other", None, "pkt-42") is None


# ===================================================================
# Same native message ID on different channels stays distinct
# ===================================================================


class TestDistinctChannels:
    """The same native_message_id on different channels maps to different events."""

    async def test_same_message_id_different_channels(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Two refs with same native_message_id but different channels resolve independently."""
        event_a = make_storage_event(event_id="evt-ch-a")
        event_b = make_storage_event(event_id="evt-ch-b")
        await temp_storage.append(event_a)
        await temp_storage.append(event_b)

        ref_a = NativeMessageRef(
            id="nref-ch-a",
            event_id="evt-ch-a",
            adapter="matrix",
            native_channel_id="!roomA:server",
            native_message_id="$msg-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        ref_b = NativeMessageRef(
            id="nref-ch-b",
            event_id="evt-ch-b",
            adapter="matrix",
            native_channel_id="!roomB:server",
            native_message_id="$msg-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        assert (
            await temp_storage.resolve_native_ref("matrix", "!roomA:server", "$msg-42")
            == "evt-ch-a"
        )
        assert (
            await temp_storage.resolve_native_ref("matrix", "!roomB:server", "$msg-42")
            == "evt-ch-b"
        )

    async def test_same_message_id_different_adapters(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Same message ID on different adapters resolves independently."""
        event_x = make_storage_event(event_id="evt-ad-x")
        event_y = make_storage_event(event_id="evt-ad-y")
        await temp_storage.append(event_x)
        await temp_storage.append(event_y)

        ref_x = NativeMessageRef(
            id="nref-ad-x",
            event_id="evt-ad-x",
            adapter="adapter_x",
            native_channel_id="ch-1",
            native_message_id="msg-99",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        ref_y = NativeMessageRef(
            id="nref-ad-y",
            event_id="evt-ad-y",
            adapter="adapter_y",
            native_channel_id="ch-1",
            native_message_id="msg-99",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref_x)
        await temp_storage.store_native_ref(ref_y)

        assert (
            await temp_storage.resolve_native_ref("adapter_x", "ch-1", "msg-99")
            == "evt-ad-x"
        )
        assert (
            await temp_storage.resolve_native_ref("adapter_y", "ch-1", "msg-99")
            == "evt-ad-y"
        )


# ===================================================================
# list_native_refs_for_event
# ===================================================================


class TestListNativeRefsForEvent:
    """list_native_refs_for_event returns all native refs for a given event."""

    async def test_empty_when_no_refs(self, temp_storage: SQLiteStorage) -> None:
        """An event with no stored native refs returns an empty list."""
        event = make_storage_event(event_id="evt-no-nrefs")
        await temp_storage.append(event)

        refs = await temp_storage.list_native_refs_for_event("evt-no-nrefs")
        assert refs == []

    async def test_returns_single_ref(self, temp_storage: SQLiteStorage) -> None:
        """One stored native ref is returned correctly."""
        event = make_storage_event(event_id="evt-single-nref")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-single",
            event_id="evt-single-nref",
            adapter="slack",
            native_channel_id="C123",
            native_message_id="123.456",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        await temp_storage.store_native_ref(ref)

        refs = await temp_storage.list_native_refs_for_event("evt-single-nref")
        assert len(refs) == 1
        assert refs[0].id == "nref-single"
        assert refs[0].adapter == "slack"
        assert refs[0].native_channel_id == "C123"
        assert refs[0].native_message_id == "123.456"
        assert refs[0].direction == "outbound"
        assert refs[0].event_id == "evt-single-nref"

    async def test_multiple_adapter_refs_for_same_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple native refs from different adapters for the same event are retrievable."""
        event = make_storage_event(event_id="evt-multi-adapters")
        await temp_storage.append(event)

        t0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2025, 6, 1, 12, 0, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, 12, 0, 2, tzinfo=timezone.utc)

        ref_matrix = NativeMessageRef(
            id="nref-matrix",
            event_id="evt-multi-adapters",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$m1",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            created_at=t0,
        )
        ref_slack = NativeMessageRef(
            id="nref-slack",
            event_id="evt-multi-adapters",
            adapter="slack",
            native_channel_id="C_GENERAL",
            native_message_id="1700000000.000001",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            created_at=t1,
        )
        ref_mesh = NativeMessageRef(
            id="nref-mesh",
            event_id="evt-multi-adapters",
            adapter="meshtastic",
            native_channel_id=None,
            native_message_id="mesh-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            created_at=t2,
        )
        await temp_storage.store_native_ref(ref_matrix)
        await temp_storage.store_native_ref(ref_slack)
        await temp_storage.store_native_ref(ref_mesh)

        refs = await temp_storage.list_native_refs_for_event("evt-multi-adapters")
        assert len(refs) == 3

        # Ordered by created_at ASC.
        assert refs[0].adapter == "matrix"
        assert refs[1].adapter == "slack"
        assert refs[2].adapter == "meshtastic"

    async def test_does_not_return_refs_from_other_events(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Refs from other events are excluded."""
        event_a = make_storage_event(event_id="evt-a")
        event_b = make_storage_event(event_id="evt-b")
        await temp_storage.append(event_a)
        await temp_storage.append(event_b)

        ref_a = NativeMessageRef(
            id="nref-a",
            event_id="evt-a",
            adapter="matrix",
            native_channel_id="ch-a",
            native_message_id="msg-a",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        ref_b = NativeMessageRef(
            id="nref-b",
            event_id="evt-b",
            adapter="matrix",
            native_channel_id="ch-b",
            native_message_id="msg-b",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        refs = await temp_storage.list_native_refs_for_event("evt-a")
        assert len(refs) == 1
        assert refs[0].event_id == "evt-a"


# ===================================================================
# RelationResolver with real SQLiteStorage
# ===================================================================


class TestRelationResolverWithStorage:
    """RelationResolver resolves native refs through real SQLiteStorage."""

    async def test_resolve_event_relations_populates_target_event_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_event_relations fills target_event_id from a stored native ref mapping."""
        # 1. Store a target event and its native ref.
        target_event = make_storage_event(event_id="evt-target-1")
        await temp_storage.append(target_event)

        target_nref = NativeMessageRef(
            id="nref-target-1",
            event_id="evt-target-1",
            adapter="discord",
            native_channel_id="channel-1",
            native_message_id="msg-target-abc",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(target_nref)

        # 2. Build a source event with an unresolved relation (no target_event_id).
        unresolved_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="discord",
                native_channel_id="channel-1",
                native_message_id="msg-target-abc",
            ),
            key=None,
            fallback_text=None,
        )
        source_event = make_storage_event(
            event_id="evt-source-1",
            relations=(unresolved_rel,),
        )

        # 3. Resolve via RelationResolver backed by real storage.
        resolver = RelationResolver(storage=temp_storage)
        resolved = await resolver.resolve_event_relations(source_event)

        # 4. The target_event_id should now be populated.
        assert resolved.relations[0].target_event_id == "evt-target-1"
        # target_native_ref is preserved.
        assert resolved.relations[0].target_native_ref is not None
        assert (
            resolved.relations[0].target_native_ref.native_message_id
            == "msg-target-abc"
        )

    async def test_resolve_event_relations_no_change_when_already_resolved(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A relation with target_event_id already set is returned unchanged."""
        resolved_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-already-known",
            target_native_ref=NativeRef(
                adapter="matrix",
                native_channel_id="!room:server",
                native_message_id="$m1",
            ),
            key=None,
            fallback_text=None,
        )
        event = make_storage_event(
            event_id="evt-pre-resolved", relations=(resolved_rel,)
        )

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_event_relations(event)

        assert result.relations[0].target_event_id == "evt-already-known"

    async def test_resolve_event_relations_preserves_unresolvable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A relation whose native ref has no stored mapping is preserved as-is."""
        unresolved_rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="unknown_adapter",
                native_channel_id="ch-x",
                native_message_id="msg-not-stored",
            ),
            key="👍",
            fallback_text=None,
        )
        event = make_storage_event(
            event_id="evt-unresolvable", relations=(unresolved_rel,)
        )

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_event_relations(event)

        # target_event_id stays None — native ref could not be resolved.
        assert result.relations[0].target_event_id is None
        assert result.relations[0].target_native_ref is not None

    async def test_resolve_event_relations_no_relations_returns_same_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An event with no relations is returned as-is."""
        event = make_storage_event(event_id="evt-no-rels")

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_event_relations(event)

        assert result.event_id == "evt-no-rels"
        assert result.relations == ()

    async def test_resolve_relation_raises_on_no_ref_and_no_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_relation raises ValueError when both target_event_id and
        target_native_ref are absent."""
        bare_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )

        resolver = RelationResolver(storage=temp_storage)
        with pytest.raises(ValueError, match="target_native_ref"):
            await resolver.resolve_relation(bare_rel)

    async def test_resolve_relation_returns_already_resolved(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A relation with target_event_id set is returned unchanged."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-known",
            target_native_ref=NativeRef(
                adapter="matrix",
                native_channel_id="ch",
                native_message_id="msg",
            ),
            key=None,
            fallback_text=None,
        )

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_relation(rel)

        assert result.target_event_id == "evt-known"
        # Should be the same object (already resolved path).
        assert result is rel

    async def test_resolve_relation_populates_from_storage(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_relation looks up native ref and returns new relation with target_event_id."""
        target_event = make_storage_event(event_id="evt-rr-target")
        await temp_storage.append(target_event)

        nref = NativeMessageRef(
            id="nref-rr-1",
            event_id="evt-rr-target",
            adapter="slack",
            native_channel_id="C123",
            native_message_id="123.456",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        unresolved = EventRelation(
            relation_type="thread",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="slack",
                native_channel_id="C123",
                native_message_id="123.456",
            ),
            key=None,
            fallback_text=None,
        )

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_relation(unresolved)

        assert result.target_event_id == "evt-rr-target"
        assert result.target_native_ref is not None

    async def test_resolve_relation_returns_original_when_unresolvable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_relation returns the original relation when no mapping exists."""
        unresolved = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="ghost",
                native_channel_id="ch-ghost",
                native_message_id="msg-ghost",
            ),
            key=None,
            fallback_text=None,
        )

        resolver = RelationResolver(storage=temp_storage)
        result = await resolver.resolve_relation(unresolved)

        assert result.target_event_id is None
        assert result is unresolved


# ===================================================================
# NULL channel native ref: cross-adapter independence
# ===================================================================


class TestNullChannelCrossAdapter:
    """Same native_message_id with NULL channel on different adapters resolves
    independently — the adapter name is part of the uniqueness key even
    when native_channel_id is NULL."""

    async def test_null_channel_different_adapters_independent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """LXMF-style NULL-channel refs for different adapters resolve to
        different events even with identical native_message_ids."""
        event_a = make_storage_event(event_id="evt-lxmf-a")
        event_b = make_storage_event(event_id="evt-lxmf-b")
        await temp_storage.append(event_a)
        await temp_storage.append(event_b)

        ref_a = NativeMessageRef(
            id="nref-lxmf-a",
            event_id="evt-lxmf-a",
            adapter="lxmf_alpha",
            native_channel_id=None,
            native_message_id="hash-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        ref_b = NativeMessageRef(
            id="nref-lxmf-b",
            event_id="evt-lxmf-b",
            adapter="lxmf_bravo",
            native_channel_id=None,
            native_message_id="hash-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        assert (
            await temp_storage.resolve_native_ref("lxmf_alpha", None, "hash-42")
            == "evt-lxmf-a"
        )
        assert (
            await temp_storage.resolve_native_ref("lxmf_bravo", None, "hash-42")
            == "evt-lxmf-b"
        )

    async def test_null_channel_idempotent_same_adapter(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Repeated store of the same NULL-channel ref for the same adapter
        is idempotent — only one row exists after two inserts."""
        event = make_storage_event(event_id="evt-null-idem-2")
        await temp_storage.append(event)

        for i in range(3):
            ref = NativeMessageRef(
                id=f"nref-null-attempt-{i}",
                event_id="evt-null-idem-2",
                adapter="lxmf_idem",
                native_channel_id=None,
                native_message_id="hash-repeat",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
            await temp_storage.store_native_ref(ref)

        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs "
            "WHERE adapter = ? AND native_channel_id IS NULL "
            "AND native_message_id = ?",
            ("lxmf_idem", "hash-repeat"),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "nref-null-attempt-0"
        assert (
            await temp_storage.resolve_native_ref("lxmf_idem", None, "hash-repeat")
            == "evt-null-idem-2"
        )
