"""Pipeline text enrichment and channel-matching tests.

Moved from test_pipeline_native_refs.py to keep file under 1500 lines.
Contains text enrichment tests (fallback_text / original_text population)
and channel-aware native-ref matching tests (Fix 9).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.engine.pipeline import (
    PipelineConfig,
    PipelineRunner,
)
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.routing import Router
from medre.core.storage import SQLiteStorage


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
        assert enriched_rel.metadata.get("original_text") == "Hello from the original message"

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
