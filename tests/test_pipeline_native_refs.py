"""Pipeline native reference and immutability tests.

Tests inbound native ref persistence, native_channel_id fallback removal,
canonical event immutability downstream of pipeline processing, native
metadata persistence, and target native ref enrichment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.contracts.adapter import AdapterDeliveryResult
from medre.core.engine.pipeline import (
    PipelineRunner,
    _native_metadata_for_ref,
)
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import NativeMetadata
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    """An unstarted FakeTransportAdapter for creating test events."""
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    """A FakePresentationAdapter that records delivered events."""
    return FakePresentationAdapter(adapter_id="fake_presentation")


# ===================================================================
# Canonical immutability downstream tests
# ===================================================================


class TestCanonicalImmutabilityDownstream:
    """Verify that canonical events cannot be mutated after pipeline
    processing — they are frozen after creation and remain immutable
    through storage, routing, rendering, and delivery.
    """

    async def test_event_not_mutated_after_storage_and_routing(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event stored in DB is identical to the original ingress event."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="immut-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="immut-001",
            source_adapter="src",
            payload={"text": "immutable"},
        )
        # Capture original field values before pipeline processes.
        original_kind = event.event_kind
        original_payload_body = event.payload["text"]
        original_source = event.source_adapter

        try:
            await runner.handle_ingress(event)

            # Retrieve from storage — fields must match original.
            stored = await temp_storage.get("immut-001")
            assert stored is not None
            assert stored.event_kind == original_kind
            assert stored.payload["text"] == original_payload_body
            assert stored.source_adapter == original_source
        finally:
            await runner.stop()

    async def test_frozen_event_raises_on_field_assignment(self) -> None:
        """CanonicalEvent is frozen — assigning to any field raises."""
        event = make_event(event_id="freeze-001")
        with pytest.raises(AttributeError):
            event.event_kind = "tampered"
        with pytest.raises(AttributeError):
            event.payload = {"evil": True}
        with pytest.raises(AttributeError):
            event.source_adapter = "impostor"

    async def test_frozen_event_payload_dict_is_immutable(self) -> None:
        """The frozen event's payload dict cannot be reassigned.

        Note: the dict itself is not deeply frozen (that would require
        a custom mapping), but the struct field is frozen — you cannot
        replace the payload reference.
        """
        event = make_event(event_id="freeze-002")
        original_text = event.payload["text"]
        # Struct is frozen — reassignment raises.
        with pytest.raises(AttributeError):
            event.payload = {"hacked": True}
        # Original value unchanged.
        assert event.payload["text"] == original_text


# ===================================================================
# Inbound native ref persistence
# ===================================================================


class TestInboundNativeRefPersistence:
    """Pipeline persists inbound NativeMessageRef when source_native_ref exists."""

    async def test_inbound_native_ref_persisted(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline stores inbound NativeMessageRef for events with source_native_ref."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="inbound-ref-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event-001",
        )
        event = make_event(
            event_id="inbound-ref-001",
            source_adapter="src",
        )
        # Manually construct event with source_native_ref
        event = CanonicalEvent(
            event_id="inbound-ref-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=event.timestamp,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            await runner.handle_ingress(event)

            # Verify inbound native ref was persisted
            resolved = await temp_storage.resolve_native_ref(
                "matrix", "!room:server", "$event-001"
            )
            assert resolved == "inbound-ref-001"
        finally:
            await runner.stop()

    async def test_no_inbound_ref_when_source_native_ref_is_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline does not persist inbound ref when source_native_ref is None."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="no-ref-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="no-ref-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            # No inbound native ref should exist for this event
            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ? AND direction = 'inbound'",
                ("no-ref-001",),
            )
            assert len(rows) == 0
        finally:
            await runner.stop()

    async def test_inbound_native_ref_idempotent(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Storing the same inbound native ref twice is idempotent."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="idem-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$idem-001",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="idem-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            # First ingress
            await runner.handle_ingress(event)
            # Second ingress with same event (will fail FK due to same event_id,
            # but the native ref insert is OR IGNORE so it's idempotent)
            # Instead, test idempotency at the storage layer directly
            from medre.core.events import NativeMessageRef

            ref2 = NativeMessageRef(
                id="nref-idem-dup",
                event_id="idem-001",
                adapter="matrix",
                native_channel_id="!room:server",
                native_message_id="$idem-001",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await temp_storage.store_native_ref(ref2)

            resolved = await temp_storage.resolve_native_ref(
                "matrix", "!room:server", "$idem-001"
            )
            assert resolved == "idem-001"
        finally:
            await runner.stop()


# ===================================================================
# Pipeline native_channel_id fallback removal
# ===================================================================


class TestPipelineNativeChannelIdNoFallback:
    """Pipeline must not fall back to target.channel when adapter returns
    native_channel_id=None.
    """

    async def test_adapter_returns_null_channel_id_stores_null(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When adapter returns native_channel_id=None, the native ref
        stores NULL, not target.channel."""

        class _NullChannelAdapter:
            """Adapter that returns a native_message_id but native_channel_id=None."""

            adapter_id = "null_ch"
            platform = "test"
            received_events: list[object] = []

            async def deliver(self, payload: object):
                from medre.core.contracts.adapter import AdapterDeliveryResult

                return AdapterDeliveryResult(
                    native_message_id="msg-001",
                    native_channel_id=None,
                )

        adapter = _NullChannelAdapter()
        route = Route(
            id="null-ch-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="null_ch", channel="fallback-channel")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"null_ch": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="null-ch-001", source_adapter="src")
        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ?",
                ("null-ch-001",),
            )
            assert len(refs) == 1
            assert refs[0]["native_channel_id"] is None
            # Must NOT be "fallback-channel"
        finally:
            await runner.stop()

    async def test_no_native_message_id_means_no_ref(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When adapter returns native_message_id=None, no native ref is stored."""

        class _NoMsgIdAdapter:
            """Adapter that returns no native_message_id."""

            adapter_id = "no_msg_id"
            platform = "test"
            received_events: list[object] = []

            async def deliver(self, payload: object):
                from medre.core.contracts.adapter import AdapterDeliveryResult

                return AdapterDeliveryResult(
                    native_message_id=None,
                    native_channel_id="ch-1",
                )

        adapter = _NoMsgIdAdapter()
        route = Route(
            id="no-msgid-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="no_msg_id")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"no_msg_id": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="no-msgid-001", source_adapter="src")
        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ?",
                ("no-msgid-001",),
            )
            assert len(refs) == 0
        finally:
            await runner.stop()


# ===================================================================
# Native metadata persistence
# ===================================================================


class TestNativeMetadataForRef:
    """_native_metadata_for_ref extracts native metadata without mutation."""

    def test_returns_data_when_native_metadata_present(self) -> None:
        """Returns dict copy of native.data when present."""
        event = CanonicalEvent(
            event_id="meta-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(
                native=NativeMetadata(data={"sender": "alice", "guild_id": "123"})
            ),
        )
        result = _native_metadata_for_ref(event)
        assert result == {"sender": "alice", "guild_id": "123"}
        # Must be a plain dict (mutable copy), not the frozen internal one.
        assert isinstance(result, dict)
        result["extra"] = True
        # Original event not mutated.
        assert "extra" not in event.metadata.native.data  # type: ignore[union-attr]

    def test_returns_empty_when_native_is_none(self) -> None:
        """Returns {} when event.metadata.native is None."""
        event = CanonicalEvent(
            event_id="meta-002",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
        )
        assert _native_metadata_for_ref(event) == {}

    def test_returns_empty_when_native_data_is_empty(self) -> None:
        """Returns {} when native.data is empty dict."""
        event = CanonicalEvent(
            event_id="meta-003",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(native=NativeMetadata(data={})),
        )
        assert _native_metadata_for_ref(event) == {}


class TestInboundNativeMetadataPersistence:
    """Inbound NativeMessageRef carries event native metadata."""

    async def test_inbound_ref_copies_native_metadata(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Inbound NativeMessageRef.metadata contains event.metadata.native.data."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="inbound-meta-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$meta-001",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="inbound-meta-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(
                native=NativeMetadata(data={"author_id": "u-42", "msg_type": "rich"})
            ),
            source_native_ref=nref,
        )

        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'inbound'",
                ("inbound-meta-001",),
            )
            assert len(refs) == 1
            import json

            meta = json.loads(refs[0]["metadata"])
            assert meta["author_id"] == "u-42"
            assert meta["msg_type"] == "rich"
        finally:
            await runner.stop()

    async def test_inbound_ref_empty_metadata_when_no_native(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Inbound NativeMessageRef.metadata is {} when event has no native metadata."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="inbound-no-meta-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$no-meta-001",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="inbound-no-meta-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'inbound'",
                ("inbound-no-meta-001",),
            )
            assert len(refs) == 1
            import json

            meta = json.loads(refs[0]["metadata"])
            assert meta == {}
        finally:
            await runner.stop()


class TestOutboundNativeMetadataPersistence:
    """Outbound NativeMessageRef carries adapter result metadata."""

    async def test_outbound_ref_copies_adapter_metadata(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Outbound NativeMessageRef.metadata contains adapter_result.metadata."""

        class _MetaAdapter:
            adapter_id = "meta_out"
            platform = "test"

            async def deliver(self, payload: object) -> AdapterDeliveryResult:
                return AdapterDeliveryResult(
                    native_message_id="out-msg-001",
                    native_channel_id="ch-out",
                    metadata=MappingProxyType({"txn_id": "t-99", "epoch": 42}),
                )

        adapter = _MetaAdapter()
        route = Route(
            id="out-meta-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="meta_out")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"meta_out": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="out-meta-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                ("out-meta-001",),
            )
            assert len(refs) == 1
            import json

            meta = json.loads(refs[0]["metadata"])
            assert meta["txn_id"] == "t-99"
            assert meta["epoch"] == 42
        finally:
            await runner.stop()

    async def test_outbound_ref_empty_metadata_when_adapter_returns_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Outbound NativeMessageRef.metadata is {} when adapter_result.metadata is empty."""

        class _NoMetaAdapter:
            adapter_id = "no_meta_out"
            platform = "test"

            async def deliver(self, payload: object) -> AdapterDeliveryResult:
                return AdapterDeliveryResult(
                    native_message_id="out-msg-002",
                    native_channel_id="ch-out",
                    metadata=MappingProxyType({}),
                )

        adapter = _NoMetaAdapter()
        route = Route(
            id="out-no-meta-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="no_meta_out")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"no_meta_out": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="out-no-meta-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                ("out-no-meta-001",),
            )
            assert len(refs) == 1
            import json

            meta = json.loads(refs[0]["metadata"])
            assert meta == {}
        finally:
            await runner.stop()


class TestDuplicateNativeRefSuppression:
    """Duplicate inbound native refs are suppressed idempotently."""

    async def test_duplicate_native_ref_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Second event with same native ref triple is deduplicated."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="dedup-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$dedup-001",
        )

        try:
            # First event — should be accepted.
            event1 = CanonicalEvent(
                event_id="dedup-ev-001",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="src",
                source_transport_id="node-1",
                source_channel_id=None,
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "first"},
                metadata=EventMetadata(),
                source_native_ref=nref,
            )
            outcomes1 = await runner.handle_ingress(event1)
            assert len(outcomes1) >= 1

            # Second event with same native ref — should be suppressed.
            event2 = CanonicalEvent(
                event_id="dedup-ev-002",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="src",
                source_transport_id="node-1",
                source_channel_id=None,
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "second"},
                metadata=EventMetadata(),
                source_native_ref=nref,
            )
            outcomes2 = await runner.handle_ingress(event2)
            assert outcomes2 == []

            # Only the first event should be stored.
            stored = await temp_storage.get("dedup-ev-001")
            assert stored is not None
            stored2 = await temp_storage.get("dedup-ev-002")
            assert stored2 is None
        finally:
            await runner.stop()


# ===================================================================
# Target native ref enrichment
# ===================================================================


class TestEnrichRelationsForTarget:
    """_enrich_relations_for_target enriches relations with target native refs."""

    async def test_enrichment_success(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation enriched when target_event_id has a matching native ref."""
        # Pre-store a native ref for a prior event.
        prior_ref = NativeMessageRef(
            id="nref-prior-001",
            event_id="prior-ev-001",
            adapter="target_adapter",
            native_channel_id="!target:server",
            native_message_id="$target-msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(prior_ref)

        adapter = FakePresentationAdapter(adapter_id="target_adapter")
        route = Route(
            id="enrich-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target_adapter")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target_adapter": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-ev-001",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="enrich-ev-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply"},
            metadata=EventMetadata(),
        )

        try:
            enriched = await runner._enrich_relations_for_target(
                event, "target_adapter"
            )
            assert enriched.relations[0].target_native_ref is not None
            assert (
                enriched.relations[0].target_native_ref.adapter == "target_adapter"
            )
            assert (
                enriched.relations[0].target_native_ref.native_message_id
                == "$target-msg-001"
            )
            assert (
                enriched.relations[0].target_native_ref.native_channel_id
                == "!target:server"
            )
            # Preserved original fields.
            assert enriched.relations[0].target_event_id == "prior-ev-001"
            assert enriched.relations[0].key is None
            assert enriched.relations[0].fallback_text == "original message"
            # Original event not mutated.
            assert event.relations[0].target_native_ref is None
        finally:
            await runner.stop()

    async def test_enrichment_miss_no_matching_adapter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation not enriched when no native ref for target_adapter exists."""
        # Pre-store a native ref for a DIFFERENT adapter.
        prior_ref = NativeMessageRef(
            id="nref-miss-001",
            event_id="prior-miss-001",
            adapter="other_adapter",
            native_channel_id="!other:server",
            native_message_id="$other-msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(prior_ref)

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[]),
            adapters={},
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-miss-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="enrich-miss-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply"},
            metadata=EventMetadata(),
        )

        enriched = await runner._enrich_relations_for_target(event, "target_adapter")
        # No enrichment — relation unchanged.
        assert enriched.relations[0].target_native_ref is None
        # Same object returned when no changes.
        assert enriched is event

    async def test_enrichment_no_method_on_storage(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Enrichment gracefully handles storage without list_native_refs_for_event."""
        # Simulate storage that lacks list_native_refs_for_event by
        # testing with a minimal mock that raises AttributeError.
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[]),
            adapters={},
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-nomethod-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="enrich-nomethod-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply"},
            metadata=EventMetadata(),
        )

        # Use a storage mock that lacks list_native_refs_for_event.
        class _MinimalStorage:
            pass

        runner._config.storage = _MinimalStorage()  # type: ignore[assignment]

        enriched = await runner._enrich_relations_for_target(event, "any_adapter")
        assert enriched is event
        assert enriched.relations[0].target_native_ref is None

    async def test_enrichment_skips_when_already_has_matching_ref(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation already having a native ref for target_adapter is left as-is."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[]),
            adapters={},
        )
        runner = PipelineRunner(config)

        existing_nref = NativeRef(
            adapter="target_adapter",
            native_channel_id="!existing:server",
            native_message_id="$existing-msg",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-001",
            target_native_ref=existing_nref,
            key="emoji",
            fallback_text="fallback",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="enrich-skip-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply"},
            metadata=EventMetadata(),
        )

        enriched = await runner._enrich_relations_for_target(event, "target_adapter")
        # Same object — no changes needed.
        assert enriched is event
        assert enriched.relations[0].target_native_ref is existing_nref

    async def test_enrichment_no_relations(self) -> None:
        """Event with no relations returns same event."""
        config = make_pipeline_config_for_pipeline(
            storage=None,  # type: ignore[arg-type]
            router=Router(routes=[]),
            adapters={},
        )
        runner = PipelineRunner(config)
        event = make_event(event_id="enrich-empty-001")
        result = await runner._enrich_relations_for_target(event, "any")
        assert result is event


class TestRendererReceivesEnrichedRelation:
    """End-to-end: renderer receives an event with enriched native refs."""

    async def test_renderer_sees_enriched_relation(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Renderer receives event with target_native_ref populated via enrichment."""
        # Pre-store a native ref for a prior event.
        prior_ref = NativeMessageRef(
            id="nref-render-001",
            event_id="prior-render-001",
            adapter="render_target",
            native_channel_id="!render:server",
            native_message_id="$render-msg-001",
            native_thread_id="thread-1",
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(prior_ref)

        # Track what the renderer receives.
        rendered_events: list[CanonicalEvent] = []

        class _SpyRenderer:
            """Renderer that captures the event it renders."""

            name = "spy"

            def can_render(self, event, target_adapter, target_platform=None):
                return True

            async def render(self, event, target_adapter, target_channel=None):
                rendered_events.append(event)
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=target_adapter,
                    target_channel=target_channel,
                    payload=dict(event.payload),
                )

        class _SimpleAdapter:
            adapter_id = "render_target"
            platform = "test"

            async def deliver(self, payload: object) -> AdapterDeliveryResult:
                return AdapterDeliveryResult(
                    native_message_id="delivered-001",
                    native_channel_id="ch-delivered",
                )

        from medre.core.rendering.renderer import RenderingPipeline

        rp = RenderingPipeline()
        rp.register(_SpyRenderer(), priority=1)

        adapter = _SimpleAdapter()
        route = Route(
            id="render-enrich-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="render_target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"render_target": adapter},
        )
        config.rendering_pipeline = rp
        runner = PipelineRunner(config)
        await runner.start()

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-render-001",
            target_native_ref=None,
            key=None,
            fallback_text="original",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="render-enrich-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "enriched reply"},
            metadata=EventMetadata(),
        )

        try:
            await runner.handle_ingress(event)

            assert len(rendered_events) == 1
            rendered = rendered_events[0]
            assert rendered.relations[0].target_native_ref is not None
            assert (
                rendered.relations[0].target_native_ref.adapter == "render_target"
            )
            assert (
                rendered.relations[0].target_native_ref.native_message_id
                == "$render-msg-001"
            )
            assert (
                rendered.relations[0].target_native_ref.native_channel_id
                == "!render:server"
            )
            assert rendered.relations[0].target_native_ref.native_thread_id == "thread-1"
            # Preserved relation fields.
            assert rendered.relations[0].target_event_id == "prior-render-001"
            assert rendered.relations[0].key is None
            assert rendered.relations[0].fallback_text == "original"
        finally:
            await runner.stop()
