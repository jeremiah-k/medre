"""Pipeline native reference and immutability tests.

Tests inbound native ref persistence, native_channel_id fallback removal,
and canonical event immutability downstream of pipeline processing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
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
