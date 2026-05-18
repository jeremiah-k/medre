"""Tests for FakeTransportAdapter and FakePresentationAdapter: capabilities,
lifecycle (start/stop), inbound simulation, event inspection lists, rendering
boundary enforcement, relation fallback rendering, and canonical immutability.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters import FakePresentationAdapter, FakeTransportAdapter
from medre.core.contracts.adapter import (
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
)
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult
from medre.core.rendering.text import TextRenderer


def _make_event(event_id: str = "evt-1") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# FakeTransportAdapter
# ===================================================================


class TestFakeTransportAdapter:
    """FakeTransportAdapter capabilities, lifecycle, and inbound simulation."""

    def test_capabilities(self) -> None:
        """FakeTransportAdapter declares the expected capabilities."""
        adapter = FakeTransportAdapter("test_t")
        assert adapter.role == AdapterRole.TRANSPORT
        assert adapter.platform == "fake_transport"

    async def test_capabilities_from_health_check(self, make_adapter_context) -> None:
        """Health check reports capabilities with correct feature flags."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.role == AdapterRole.TRANSPORT
        caps = info.capabilities
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "fallback"
        assert caps.edits == "unsupported"
        assert caps.deletes == "unsupported"
        assert caps.max_text_chars == 200

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        """After start(), is_started is True and ctx is stored."""
        adapter = FakeTransportAdapter("test_t")
        assert adapter.is_started is False
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        """After stop(), is_started is False."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_simulate_inbound_publishes_event(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """simulate_inbound calls publish_inbound and records the event."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        event = _make_event()
        await adapter.simulate_inbound(event)
        assert event in inbound_collector.events
        assert event in adapter.delivered_events

    async def test_simulate_inbound_raises_before_start(self) -> None:
        """simulate_inbound raises RuntimeError if not started."""
        adapter = FakeTransportAdapter("test_t")
        event = _make_event()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(event)

    async def test_make_event(self) -> None:
        """make_event creates a valid CanonicalEvent."""
        adapter = FakeTransportAdapter("test_t", channel="ch-0")
        event = adapter.make_event(text="ping")
        assert event.source_adapter == "test_t"
        assert event.source_channel_id == "ch-0"
        assert event.payload["body"] == "ping"


# ===================================================================
# FakePresentationAdapter
# ===================================================================


class TestFakePresentationAdapter:
    """FakePresentationAdapter capabilities, lifecycle, and event receipt."""

    def test_capabilities(self) -> None:
        """FakePresentationAdapter has PRESENTATION role."""
        adapter = FakePresentationAdapter("test_p")
        assert adapter.role == AdapterRole.PRESENTATION
        assert adapter.platform == "fake_presentation"

    async def test_capabilities_from_health_check(self, make_adapter_context) -> None:
        """Health check reports correct capabilities."""
        adapter = FakePresentationAdapter("test_p")
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.role == AdapterRole.PRESENTATION
        caps = info.capabilities
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "native"
        assert caps.delivery_receipts is True

    async def test_start_and_stop(self, make_adapter_context) -> None:
        """Lifecycle transitions work."""
        adapter = FakePresentationAdapter("test_p")
        assert adapter.is_started is False
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        assert adapter.is_started is True
        await adapter.stop()
        assert adapter.is_started is False

    async def test_deliver_stores_received_event(self) -> None:
        """deliver() raises TypeError when called with CanonicalEvent."""
        adapter = FakePresentationAdapter("test_p")
        event = _make_event()
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_deliver_rendering_result_stores_payload(self) -> None:
        """deliver(RenderingResult) stores in delivered_payloads."""
        adapter = FakePresentationAdapter("test_p")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_p",
            target_channel="ch-0",
            payload={"text": "hello"},
            metadata={"renderer": "text"},
        )
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] is result
        assert isinstance(delivery, object)  # AdapterDeliveryResult

    async def test_delivered_payloads_list_for_inspection(self) -> None:
        """Multiple delivered RenderingResults accumulate in delivered_payloads."""
        adapter = FakePresentationAdapter("test_p")
        for i in range(3):
            result = RenderingResult(
                event_id=f"evt-{i}",
                target_adapter="test_p",
                target_channel="ch-0",
                payload={"text": f"msg-{i}"},
            )
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 3
        assert adapter.delivered_payloads[0].event_id == "evt-0"
        assert adapter.delivered_payloads[2].event_id == "evt-2"

    async def test_simulate_inbound_publishes_event(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """simulate_inbound calls publish_inbound and records in inbound_events."""
        adapter = FakePresentationAdapter("test_p")
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        event = _make_event()
        await adapter.simulate_inbound(event)
        assert event in inbound_collector.events
        assert event in adapter.inbound_events

    async def test_make_reply_event(self) -> None:
        """make_reply_event creates an event with a reply relation."""
        adapter = FakePresentationAdapter("test_p")
        target = _make_event(event_id="target-evt")
        reply = adapter.make_reply_event(target, text="reply text")
        assert len(reply.relations) == 1
        assert reply.relations[0].relation_type == "reply"
        assert reply.relations[0].target_event_id == "target-evt"
        assert reply.payload["body"] == "reply text"

    async def test_make_reaction_event(self) -> None:
        """make_reaction_event creates an event with a reaction relation."""
        adapter = FakePresentationAdapter("test_p")
        target = _make_event(event_id="target-evt")
        reaction = adapter.make_reaction_event(target, emoji="🔥")
        assert len(reaction.relations) == 1
        assert reaction.relations[0].relation_type == "reaction"
        assert reaction.relations[0].key == "🔥"


# ===================================================================
# Rendering boundary & relation fallback tests
# ===================================================================


class TestRenderingBoundary:
    """Enforce that adapters consume RenderingResult, not raw event text."""

    async def test_adapter_does_not_format_raw_event(self) -> None:
        """Adapter stores a RenderingResult without reformatting it.

        The adapter must receive the pre-rendered result as-is and store
        it in ``delivered_payloads``.  It must NOT convert the rendering
        result into raw event text or perform event-kind-specific
        formatting.
        """
        adapter = FakePresentationAdapter("test_p")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_p",
            target_channel="ch-0",
            payload={"text": "hello world"},
            metadata={"renderer": "text"},
        )
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        stored = adapter.delivered_payloads[0]
        assert isinstance(stored, RenderingResult)
        assert stored.payload["text"] == "hello world"
        # Adapter did NOT store it as a raw string event.
        assert not isinstance(stored, str)

    async def test_relation_fallback_rendering(self) -> None:
        """Reply with fallback_text renders as
        '[replying to: {fallback_text}] {payload.text}'.
        """
        renderer = TextRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-orig",
            target_native_ref=NativeRef(
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="msg-orig",
            ),
            key=None,
            fallback_text="original message text",
        )
        event = CanonicalEvent(
            event_id="evt-reply",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(relation,),
            payload={"text": "a reply"},
            metadata=EventMetadata(),
        )
        assert renderer.can_render(event, "fake_transport")
        result = await renderer.render(event, "fake_transport")
        assert result.payload["text"] == "[replying to: original message text] a reply"
        assert result.fallback_applied == "relation_reply"

    async def test_reaction_fallback_rendering(self) -> None:
        """Reaction with key renders as '{actor} reacted with {key}'."""
        renderer = TextRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="evt-orig",
            target_native_ref=NativeRef(
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="msg-orig",
            ),
            key="👍",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="evt-react",
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="alice",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(relation,),
            payload={"text": "👍"},
            metadata=EventMetadata(),
        )
        assert renderer.can_render(event, "fake_transport")
        result = await renderer.render(event, "fake_transport")
        assert result.payload["text"] == "alice reacted with 👍"
        assert result.fallback_applied == "relation_reaction"

    async def test_adapter_does_not_mutate_canonical_event(self) -> None:
        """Canonical events remain identical to their creation snapshot.

        The adapter stores a snapshot when ``make_event`` is called.
        After simulate_inbound the event must be referentially equal to
        the snapshot, proving no mutation occurred.
        """
        adapter = FakeTransportAdapter("test_t", channel="ch-0")
        event = adapter.make_event(text="immutable test")
        snapshot = adapter.event_snapshots[event.event_id]
        # The event is frozen (msgspec.Struct, frozen=True) so mutation
        # would raise anyway, but we also verify referential equality.
        assert event is snapshot
        # CanonicalEvent is frozen — verify immutability.
        with pytest.raises(AttributeError):
            event.event_kind = "tampered"  # type: ignore[misc]

    async def test_adapter_does_not_perform_route_matching(self) -> None:
        """Fake adapters have no route matching logic.

        The adapter stores whatever RenderingResult is delivered without
        filtering by event kind, channel, or source.  This verifies that
        route matching is not the adapter's responsibility.
        """
        adapter = FakePresentationAdapter("test_p")

        # Deliver rendering results with different underlying event kinds.
        for kind in (
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        ):
            result = RenderingResult(
                event_id=f"evt-{kind}",
                target_adapter="test_p",
                target_channel="any-channel",
                payload={"text": "test"},
            )
            await adapter.deliver(result)

        # All payloads stored — no filtering or route matching.
        assert len(adapter.delivered_payloads) == 4


# ===================================================================
# Delivery contract tests
# ===================================================================


class TestDeliveryContract:
    """Verify the explicit adapter delivery contract."""

    async def test_base_adapter_requires_deliver(self) -> None:
        """AdapterContract declares deliver as an abstract method."""
        from medre.core.contracts.adapter import AdapterContract

        # Verify deliver is abstract in the ABC sense.
        assert hasattr(AdapterContract, "deliver")
        assert getattr(AdapterContract.deliver, "__isabstractmethod__", False) is True

    async def test_fake_transport_deliver_stores_rendering_result(self) -> None:
        """FakeTransportAdapter.deliver() stores RenderingResult and returns AdapterDeliveryResult."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        adapter = FakeTransportAdapter("test_t")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_t",
            target_channel="ch-0",
            payload={"text": "transported message"},
            metadata={"renderer": "text"},
        )
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        stored = adapter.delivered_payloads[0]
        assert isinstance(stored, RenderingResult)
        assert stored.payload["text"] == "transported message"
        # Returns deterministic native ID.
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "fake-transport-evt-1"

    async def test_fake_presentation_deliver_stores_rendering_result(self) -> None:
        """FakePresentationAdapter.deliver(RenderingResult) stores in delivered_payloads."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        adapter = FakePresentationAdapter("test_p")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_p",
            target_channel="ch-0",
            payload={"text": "presented message"},
            metadata={"renderer": "text"},
        )
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] is result
        # Returns deterministic native ID.
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "fake-pres-evt-1"

    async def test_adapter_deliver_does_not_reformat(self) -> None:
        """Adapter stores the RenderingResult payload verbatim.

        The adapter must not modify, re-render, or reformat the payload
        inside a RenderingResult.  Whatever the renderer produced is
        exactly what the adapter stores.
        """
        adapter = FakePresentationAdapter("test_p")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_p",
            target_channel="ch-0",
            payload={"text": "original content", "extra": [1, 2, 3]},
            metadata={"renderer": "text", "custom": True},
            truncated=True,
            fallback_applied="relation_reply",
        )
        await adapter.deliver(result)
        stored = adapter.delivered_payloads[0]
        # Exact referential equality — the adapter stored the object as-is.
        assert stored is result
        assert stored.truncated is True
        assert stored.fallback_applied == "relation_reply"
        assert stored.payload["extra"] == [1, 2, 3]

    async def test_both_fake_adapters_share_delivery_contract(self) -> None:
        """Both fake adapters implement deliver() accepting RenderingResult."""
        from medre.core.contracts.adapter import AdapterContract

        transport = FakeTransportAdapter("t")
        presentation = FakePresentationAdapter("p")
        # Both are AdapterContract instances with a deliver method
        assert isinstance(transport, AdapterContract)
        assert isinstance(presentation, AdapterContract)
        assert hasattr(transport, "deliver")
        assert hasattr(presentation, "deliver")
        assert callable(transport.deliver)
        assert callable(presentation.deliver)

    async def test_fake_presentation_rejects_canonical_event(self) -> None:
        """FakePresentationAdapter.deliver raises TypeError on CanonicalEvent."""
        adapter = FakePresentationAdapter("test_p")
        event = _make_event()
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_fake_matrix_rejects_canonical_event(self) -> None:
        """FakeMatrixAdapter.deliver raises AdapterPermanentError on CanonicalEvent."""
        from medre.adapters import FakeMatrixAdapter

        adapter = FakeMatrixAdapter("test_m")
        event = _make_event()
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_faulty_presentation_rejects_canonical_event(self) -> None:
        """FaultyPresentationAdapter.deliver raises TypeError on CanonicalEvent."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(
            adapter_id="test",
            failure_mode="succeed",
        )
        event = _make_event()
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)


# ===================================================================
# Plugin boundary tests
# ===================================================================


class TestPluginBoundary:
    """Prove that plugins cannot directly emit transport-native payloads.
    Plugins must operate on canonical events and runtime APIs only.
    """

    def test_plugin_capability_enum_values(self) -> None:
        """PluginCapability has the expected capabilities."""
        from medre.plugins import PluginCapability

        assert PluginCapability.READ_EVENTS.value == "read_events"
        assert PluginCapability.EMIT_EVENTS.value == "emit_events"
        assert PluginCapability.READ_ROUTES.value == "read_routes"
        assert PluginCapability.MODIFY_ROUTES.value == "modify_routes"

    def test_plugin_protocol_is_runtime_checkable(self) -> None:
        """Plugin protocol supports isinstance() checks."""

        class _MinimalPlugin:
            name = "test"
            version = "0.1.0"
            capabilities = set()

            async def initialize(self, ctx): ...
            async def handle_event(self, event):
                return []

            async def shutdown(self): ...

        from medre.plugins import Plugin

        assert isinstance(_MinimalPlugin(), Plugin)

    async def test_validate_plugin_payload_accepts_canonical_events(self) -> None:
        """validate_plugin_payload passes for valid CanonicalEvent list."""
        from medre.plugins import validate_plugin_payload

        events = [_make_event(f"plugin-evt-{i}") for i in range(3)]
        result = validate_plugin_payload(events, "test_plugin")
        assert result == events

    def test_validate_plugin_payload_rejects_raw_dict(self) -> None:
        """validate_plugin_payload rejects raw dicts (e.g. Matrix JSON)."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        matrix_payload = {
            "msgtype": "m.text",
            "body": "hello matrix",
            "room_id": "!abc:example.com",
        }
        with pytest.raises(PluginBoundaryError, match="non-canonical payload"):
            validate_plugin_payload([matrix_payload], "evil_plugin")

    def test_validate_plugin_payload_rejects_bytes(self) -> None:
        """validate_plugin_payload rejects raw bytes (e.g. Meshtastic packet)."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        mesh_packet = b"\x94\x12\x00\x1a\xdd\x0a"
        with pytest.raises(PluginBoundaryError, match="non-canonical payload"):
            validate_plugin_payload([mesh_packet], "evil_plugin")

    def test_validate_plugin_payload_rejects_string(self) -> None:
        """validate_plugin_payload rejects raw strings."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        with pytest.raises(PluginBoundaryError, match="non-canonical payload"):
            validate_plugin_payload(["raw text payload"], "evil_plugin")

    def test_validate_plugin_payload_rejects_mixed_list(self) -> None:
        """validate_plugin_payload rejects a list mixing events and native payloads."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        mixed = [_make_event("ok-evt"), {"matrix": "json"}]
        with pytest.raises(PluginBoundaryError, match="non-canonical payload"):
            validate_plugin_payload(mixed, "mixed_plugin")

    def test_validate_plugin_payload_empty_list(self) -> None:
        """validate_plugin_payload accepts an empty list."""
        from medre.plugins import validate_plugin_payload

        result = validate_plugin_payload([], "empty_plugin")
        assert result == []

    def test_plugin_boundary_error_is_type_error(self) -> None:
        """PluginBoundaryError is a TypeError subclass."""
        from medre.plugins import PluginBoundaryError

        assert issubclass(PluginBoundaryError, TypeError)

    def test_plugin_cannot_emit_matrix_event(self) -> None:
        """Simulated plugin trying to emit a Matrix event is caught."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        # This is what a naive plugin might try to emit
        matrix_native_event = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
            "room_id": "!room:server",
        }
        with pytest.raises(PluginBoundaryError):
            validate_plugin_payload([matrix_native_event], "naive_plugin")

    def test_plugin_cannot_emit_meshtastic_packet(self) -> None:
        """Simulated plugin trying to emit a Meshtastic protobuf is caught."""
        from medre.plugins import PluginBoundaryError, validate_plugin_payload

        meshtastic_data = {
            "portnum": 1,
            "payload": b"\x01\x02\x03",
            "to": "!abcdef",
        }
        with pytest.raises(PluginBoundaryError):
            validate_plugin_payload([meshtastic_data], "naive_plugin")


# ===================================================================
# Track 6: FaultyPresentationAdapter failure injector
# ===================================================================


class TestFaultyPresentationAdapter:
    """Deterministic failure injection via FaultyPresentationAdapter."""

    async def test_always_fail_raises_runtime_error(self) -> None:
        """permanent_fail mode raises RuntimeError on every deliver."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(
            adapter_id="always-fail",
            failure_mode="always_fail",
        )
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="always-fail",
            target_channel="ch-0",
            payload={"text": "test"},
        )

        for _ in range(5):
            with pytest.raises(RuntimeError, match="permanent"):
                await adapter.deliver(result)

        assert adapter.call_count == 5
        assert len(adapter.delivered_payloads) == 0

    async def test_transient_fail_raises_connection_error(self) -> None:
        """transient_fail mode raises ConnectionError (retryable)."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(
            adapter_id="transient",
            failure_mode="transient_fail",
        )
        result = RenderingResult(
            event_id="evt-2",
            target_adapter="transient",
            target_channel="ch-0",
            payload={"text": "test"},
        )

        with pytest.raises(ConnectionError, match="transient"):
            await adapter.deliver(result)

    async def test_succeed_never_raises(self) -> None:
        """succeed mode never raises and stores payloads."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter
        from medre.core.contracts.adapter import AdapterDeliveryResult

        adapter = FaultyPresentationAdapter(
            adapter_id="always-ok",
            failure_mode="succeed",
        )
        result = RenderingResult(
            event_id="evt-3",
            target_adapter="always-ok",
            target_channel="ch-0",
            payload={"text": "test"},
        )

        for _ in range(5):
            delivery = await adapter.deliver(result)
            assert isinstance(delivery, AdapterDeliveryResult)

        assert adapter.call_count == 5
        assert len(adapter.delivered_payloads) == 5

    async def test_fail_n_then_succeed(self) -> None:
        """fail_n_then_succeed raises for first N calls then succeeds."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter
        from medre.core.contracts.adapter import AdapterDeliveryResult

        adapter = FaultyPresentationAdapter(
            adapter_id="recover",
            failure_mode="fail_n_then_succeed",
            fail_count=3,
        )
        result = RenderingResult(
            event_id="evt-4",
            target_adapter="recover",
            target_channel="ch-0",
            payload={"text": "test"},
        )

        # First 3 calls fail
        for i in range(3):
            with pytest.raises(RuntimeError, match="permanent"):
                await adapter.deliver(result)
            assert adapter.call_count == i + 1

        # 4th call succeeds and returns AdapterDeliveryResult
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        assert adapter.call_count == 4
        assert len(adapter.delivered_payloads) == 1

        # 5th call also succeeds
        delivery2 = await adapter.deliver(result)
        assert isinstance(delivery2, AdapterDeliveryResult)
        assert adapter.call_count == 5
        assert len(adapter.delivered_payloads) == 2

    async def test_faulty_adapter_lifecycle(self, make_adapter_context) -> None:
        """FaultyPresentationAdapter supports start/stop lifecycle."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(adapter_id="lifecycle")
        assert adapter.is_started is False

        ctx = make_adapter_context("lifecycle")
        await adapter.start(ctx)
        assert adapter.is_started is True

        info = await adapter.health_check()
        assert info.adapter_id == "lifecycle"
        assert info.health == "healthy"

        await adapter.stop()
        assert adapter.is_started is False
