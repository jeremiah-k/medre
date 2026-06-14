"""Pipeline text enrichment and channel-matching tests.

Moved from test_pipeline_native_refs.py to keep file under 1500 lines.
Contains text enrichment tests (fallback_text / original_text population)
and channel-aware native-ref matching tests (Fix 9).
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.engine.pipeline import (
    PipelineConfig,
    PipelineRunner,
)
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeMetadata,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage

# ===================================================================
# Text enrichment tests
# ===================================================================


class TestTextEnrichment:
    """Pipeline _enrich_relations_for_target populates fallback_text and
    metadata["original_text"] from the target event's payload."""

    async def test_text_enriched_from_target_payload(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation with empty fallback_text gets text from target event body."""
        # Store a prior event with known body text.
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-text-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Hello from the original message"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-text-001",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-text-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        assert enriched_rel.fallback_text == "Hello from the original message"
        assert (
            enriched_rel.metadata.get("original_text")
            == "Hello from the original message"
        )

    async def test_text_enrichment_skips_when_both_populated(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation with both fallback_text and original_text already set
        is not overwritten."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-skip-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Different text"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-skip-001",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text="already set",
            metadata={"original_text": "already set"},
        )
        event = CanonicalEvent(
            event_id="enrich-skip-text-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        # Should NOT be overwritten.
        assert enriched_rel.fallback_text == "already set"
        assert enriched_rel.metadata.get("original_text") == "already set"

    async def test_text_enrichment_runs_even_with_native_ref(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Text enrichment runs even when relation already has a matching
        target_native_ref for the target adapter."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-nref-text-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Original text here"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        existing_nref = NativeRef(
            adapter="target_adapter",
            native_channel_id="ch-1",
            native_message_id="msg-1",
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-nref-text-001",
            target_native_ref=existing_nref,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-nref-text-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        # Native ref should be preserved.
        assert enriched_rel.target_native_ref is existing_nref
        # But text enrichment should still have populated fallback_text.
        assert enriched_rel.fallback_text == "Original text here"
        assert enriched_rel.metadata.get("original_text") == "Original text here"

    async def test_text_enrichment_falls_back_to_text_field(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When target event has 'text' but no 'body', uses 'text'."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-textfield-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Text field content"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-textfield-001",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-textfield-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        assert enriched_rel.fallback_text == "Text field content"


# ===================================================================
# Test C: missing mapping safety (storage.get returns None)
# ===================================================================


class TestTextEnrichmentMissingMapping:
    """storage.get returns None -- no crash, relation preserved unchanged."""

    async def test_storage_get_returns_none_no_crash(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When storage.get(event_id) returns None, relation is unchanged."""
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="nonexistent-event-id",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-missing-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        # Should not crash.
        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        # Relation preserved unchanged (no fallback_text set).
        assert enriched_rel.fallback_text is None
        assert "original_text" not in enriched_rel.metadata


# ===================================================================
# Test D: storage.get raises exception
# ===================================================================


class TestTextEnrichmentStorageGetFailure:
    """storage.get raises exception -- no crash, relation preserved."""

    async def test_storage_get_raises_no_crash(self) -> None:
        """When storage.get raises, relation is unchanged and no crash."""

        class _FailingStorage:
            """Storage where get() always raises."""

            def list_native_refs_for_event(self, event_id):
                raise RuntimeError("DB connection lost")

            async def get(self, event_id):
                raise RuntimeError("DB connection lost")

        storage = _FailingStorage()
        config = PipelineConfig(
            storage=storage,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="boom-event-id",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-fail-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        # Should not crash.
        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        # Relation preserved unchanged.
        assert enriched_rel.fallback_text is None
        assert enriched_rel.target_native_ref is None


# ===================================================================
# Fix 9: channel-matching tests
# ===================================================================


class TestChannelAwareStrictMatching:
    """When target_channel is specified, adapter-only fallback is NOT used.
    Only exact adapter+channel matches are accepted.  When target_channel is
    None, adapter-only fallback is allowed (existing behaviour)."""

    async def test_target_channel_wrong_channel_not_enriched(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """target_channel specified, refs have same adapter but different
        channel -> relation is NOT enriched with wrong-channel ref."""
        # Store a native ref with the right adapter but wrong channel.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-wrong-ch",
                event_id="evt-strict-001",
                adapter="mesh-1",
                native_channel_id="99",
                native_message_id="wrong-ch-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strict-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strict-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Should NOT be enriched — wrong channel, no fallback.
        assert enriched_rel.target_native_ref is None

    async def test_target_channel_exact_match_enriched(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """target_channel specified, exact channel exists -> relation IS
        enriched."""
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-exact",
                event_id="evt-exact-001",
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="exact-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-exact-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-exact-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.native_message_id == "exact-msg"
        assert enriched_rel.target_native_ref.native_channel_id == "0"

    async def test_no_target_channel_adapter_only_fallback(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """target_channel None, adapter-only match still enriches."""
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-adapt",
                event_id="evt-adapt-001",
                adapter="mesh-1",
                native_channel_id="5",
                native_message_id="adapt-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-adapt-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-adapt-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel=None
        )
        enriched_rel = result.relations[0]

        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.native_message_id == "adapt-msg"


class TestExistingNativeRefStrictChannel:
    """When relation already has a target_native_ref, strict channel matching
    applies when target_channel is specified."""

    async def test_existing_ref_channel_none_target_channel_set_falls_through(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing target_native_ref with adapter match but
        native_channel_id=None and target_channel='0' should NOT be treated
        as enriched — falls through to lookup."""
        # Store a native ref so lookup can find it.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-lookup",
                event_id="evt-strict-nref-001",
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="lookup-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="old-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strict-nref-001",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strict-nref-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Should NOT be the old ref — should be the looked-up one.
        assert enriched_rel.target_native_ref is not existing_nref
        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.native_channel_id == "0"
        assert enriched_rel.target_native_ref.native_message_id == "lookup-msg"

    async def test_existing_ref_channel_matches_target_channel_compatible(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing target_native_ref with adapter match and
        native_channel_id='0' with target_channel='0' remains compatible."""
        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="existing-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-compat-nref-001",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-compat-nref-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Should be the same ref — compatible.
        assert enriched_rel.target_native_ref is existing_nref


# ===================================================================
# Fix 2: strip incompatible target_native_ref when no exact match
# ===================================================================


class TestStrictChannelStripsIncompatibleRef:
    """When an existing target_native_ref is for the right adapter but wrong
    channel and no exact match is found in storage, the incompatible ref
    is stripped (set to None) so the renderer doesn't use it."""

    async def test_existing_adapter_ref_wrong_channel_stripped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing ref adapter==target, channel='1', target_channel='0',
        no exact match in storage → target_native_ref becomes None."""
        # Store a ref for a DIFFERENT channel so lookup finds no exact match.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-strip-1",
                event_id="evt-strip-001",
                adapter="mesh-1",
                native_channel_id="1",
                native_message_id="wrong-ch-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id="1",
            native_message_id="old-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strip-001",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strip-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Incompatible ref should be stripped.
        assert enriched_rel.target_native_ref is None

    async def test_existing_adapter_ref_none_channel_stripped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing ref adapter==target, channel=None, target_channel='0',
        no exact match → target_native_ref is None."""
        # No matching ref in storage.
        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="no-ch-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strip-002",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strip-002",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        assert enriched_rel.target_native_ref is None

    async def test_existing_different_adapter_ref_preserved(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing ref for a different adapter → kept unchanged when no match."""
        existing_nref = NativeRef(
            adapter="other-adapter",
            native_channel_id="1",
            native_message_id="other-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strip-003",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strip-003",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Different adapter ref is NOT stripped.
        assert enriched_rel.target_native_ref is existing_nref

    async def test_existing_exact_ref_preserved(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing ref adapter+channel match target → kept unchanged."""
        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="correct-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strip-004",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strip-004",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        assert enriched_rel.target_native_ref is existing_nref

    async def test_existing_incompatible_replaced_by_exact_match(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing ref wrong channel, but storage has exact match → replaced
        with correct ref."""
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-strip-exact",
                event_id="evt-strip-005",
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="exact-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        existing_nref = NativeRef(
            adapter="mesh-1",
            native_channel_id="1",
            native_message_id="wrong-ch-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-strip-005",
            target_native_ref=existing_nref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="src-strip-005",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hi"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(
            event, "mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Should be replaced with the exact match, not stripped to None.
        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref is not existing_nref
        assert enriched_rel.target_native_ref.native_channel_id == "0"
        assert enriched_rel.target_native_ref.native_message_id == "exact-msg"


# ===================================================================
# Fix 4: sender info enrichment
# ===================================================================


class TestSenderInfoEnrichment:
    """Pipeline-level sender metadata enrichment layering.

    When no projection callback is wired into PipelineConfig, core
    planning must NOT interpret transport-native identity keys
    (``displayname``, ``meshtastic.longname``, bare ``longname``).
    Only the generic ``source_transport_id`` terminal fallback applies
    to ``original_sender``; ``original_sender_displayname`` stays unset.
    """

    async def test_pipeline_does_not_read_native_displayname(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Without a projection callback, native ``displayname`` is ignored."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-sender-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Hello"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={"displayname": "Alice", "sender": "@alice:server"}
                )
            ),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-sender-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-sender-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-2",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        # Native identity keys are never read; only the generic
        # source_transport_id terminal fallback applies.
        assert enriched_rel.metadata.get("original_sender_displayname") is None
        assert enriched_rel.metadata.get("original_sender") == "node-1"
        # Text enrichment should also have run.
        assert enriched_rel.fallback_text == "Hello"
        assert enriched_rel.metadata.get("original_text") == "Hello"

    async def test_pipeline_does_not_read_native_longname(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Without a projection callback, bare ``longname`` is ignored."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-longname-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-42",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Msg"},
            metadata=EventMetadata(native=NativeMetadata(data={"longname": "BobNode"})),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-longname-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-longname-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-2",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        assert enriched_rel.metadata.get("original_sender_displayname") is None
        # sender falls back to source_transport_id only
        assert enriched_rel.metadata.get("original_sender") == "node-42"

    async def test_pipeline_does_not_read_meshtastic_namespaced_longname(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Without a projection callback, ``meshtastic.longname`` is ignored."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-mesh-longname-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-43",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Msg"},
            metadata=EventMetadata(
                native=NativeMetadata(data={"meshtastic.longname": "AlphaNode"})
            ),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-mesh-longname-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="enrich-mesh-longname-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-2",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        assert enriched_rel.metadata.get("original_sender_displayname") is None
        # sender falls back to source_transport_id only
        assert enriched_rel.metadata.get("original_sender") == "node-43"

    async def test_sender_not_overwritten_when_already_present(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Existing original_sender_displayname/original_sender are preserved."""
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="prior-exist-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Msg"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={"displayname": "ShouldNotUse", "sender": "@no:server"}
                )
            ),
        )
        await temp_storage.append(prior_event)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-exist-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
            metadata={
                "original_sender_displayname": "PreExisting",
                "original_sender": "@pre:existing",
            },
        )
        event = CanonicalEvent(
            event_id="enrich-exist-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-2",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply"},
            metadata=EventMetadata(),
        )

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        result = await runner._enrich_relations_for_target(event, "target_adapter")
        enriched_rel = result.relations[0]

        assert enriched_rel.metadata.get("original_sender_displayname") == "PreExisting"
        assert enriched_rel.metadata.get("original_sender") == "@pre:existing"
