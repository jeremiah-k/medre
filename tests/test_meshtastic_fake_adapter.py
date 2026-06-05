"""Tests for FakeMeshtasticAdapter: capabilities, lifecycle (start/stop),
delivery contract, inbound simulation, rendering boundary enforcement,
packet simulation, and make_text_event helper.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_rendering_result,
    make_meshtastic_text_packet,
)

# ===================================================================
# Capabilities
# ===================================================================


class TestMeshtasticAdapterCapabilities:
    """FakeMeshtasticAdapter declares the correct role and platform."""

    def test_role_is_transport(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.role == AdapterRole.TRANSPORT

    def test_platform_is_meshtastic(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.platform == "meshtastic"

    def test_capabilities_text_true(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.text is True

    def test_capabilities_replies_native(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.replies == "native"

    def test_capabilities_reactions_native(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.reactions == "native"

    def test_capabilities_edits_unsupported(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.edits == "unsupported"

    def test_capabilities_deletes_unsupported(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.deletes == "unsupported"

    def test_capabilities_attachments_false(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.attachments is False

    def test_capabilities_direct_messages_false(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.direct_messages is False

    def test_capabilities_max_text_bytes_227(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.max_text_bytes == 227

    def test_capabilities_max_text_chars_none(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        assert _FAKE_MESHTASTIC_CAPABILITIES.max_text_chars is None


class TestRealMeshtasticCapabilities:
    """Real MeshtasticAdapter capabilities match spec."""

    def test_real_adapter_role_is_transport(self) -> None:
        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)
        assert adapter.role == AdapterRole.TRANSPORT

    def test_real_adapter_capabilities_match_fake(self) -> None:
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)
        real_caps = adapter._capabilities
        assert real_caps.text == _FAKE_MESHTASTIC_CAPABILITIES.text
        assert real_caps.replies == _FAKE_MESHTASTIC_CAPABILITIES.replies
        assert real_caps.reactions == _FAKE_MESHTASTIC_CAPABILITIES.reactions
        assert real_caps.edits == _FAKE_MESHTASTIC_CAPABILITIES.edits
        assert real_caps.deletes == _FAKE_MESHTASTIC_CAPABILITIES.deletes
        assert real_caps.attachments == _FAKE_MESHTASTIC_CAPABILITIES.attachments
        assert (
            real_caps.direct_messages == _FAKE_MESHTASTIC_CAPABILITIES.direct_messages
        )
        assert real_caps.max_text_bytes == _FAKE_MESHTASTIC_CAPABILITIES.max_text_bytes
        assert real_caps.max_text_chars == _FAKE_MESHTASTIC_CAPABILITIES.max_text_chars


# ===================================================================
# Lifecycle
# ===================================================================


class TestFakeMeshtasticAdapterLifecycle:
    """Start / stop / health-check transitions."""

    async def test_initial_started_state_is_false(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.is_started is False

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_health_check_after_start(self, make_adapter_context) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        assert info.adapter_id == "fake_meshtastic"
        assert info.role == AdapterRole.TRANSPORT


# ===================================================================
# Delivery contract
# ===================================================================


class TestFakeMeshtasticAdapterDeliver:
    """deliver() stores RenderingResult payloads correctly."""

    async def test_deliver_stores_rendering_result(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = make_meshtastic_rendering_result()
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] is result
        # Fake adapter returns AdapterDeliveryResult with deterministic ID
        assert delivery is not None
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None
        assert delivery.native_channel_id == "0"

    async def test_deliver_returns_deterministic_packet_id(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result1 = make_meshtastic_rendering_result()
        result2 = make_meshtastic_rendering_result()
        delivery1 = await adapter.deliver(result1)
        delivery2 = await adapter.deliver(result2)
        assert delivery1.native_message_id == "1"
        assert delivery2.native_message_id == "2"
        assert delivery1.native_channel_id == delivery2.native_channel_id

    async def test_deliver_does_not_reformat(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = make_meshtastic_rendering_result(
            payload={
                "text": "original",
                "channel_index": 0,
                "meshnet_name": "",
            }
        )
        await adapter.deliver(result)
        assert adapter.delivered_payloads[0] is result

    async def test_deliver_rejects_canonical_event(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)

    async def test_deliver_failure_raises_send_error(self) -> None:
        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = make_meshtastic_rendering_result()
        with pytest.raises(AdapterSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = make_meshtastic_rendering_result()
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0

    async def test_fake_client_tracks_sent_packets(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = make_meshtastic_rendering_result()
        await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 1
        assert adapter.fake_client.sent_packets[0]["text"] == "hello mesh"
        assert adapter.fake_client.sent_packets[0]["channel_index"] == 0


# ===================================================================
# Rendering boundary
# ===================================================================


class TestFakeMeshtasticRenderingBoundary:
    """Adapter consumes RenderingResult, never performs its own formatting."""

    async def test_adapter_receives_rendering_result_not_raw_event(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = make_meshtastic_rendering_result()
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert isinstance(adapter.delivered_payloads[0], RenderingResult)

    async def test_adapter_does_not_perform_kind_specific_formatting(self) -> None:
        adapter = FakeMeshtasticAdapter()
        for kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            result = make_meshtastic_rendering_result(event_id=f"evt-{kind}")
            await adapter.deliver(result)

        assert len(adapter.delivered_payloads) == 2
        for stored in adapter.delivered_payloads:
            assert isinstance(stored, RenderingResult)


# ===================================================================
# Inbound simulation
# ===================================================================


class TestFakeMeshtasticAdapterSimulateInbound:
    """simulate_inbound processes packets through classifier + codec."""

    async def test_simulate_inbound_text_packet(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="hello mesh")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert len(adapter.inbound_events) == 1
        event = inbound_collector.events[0]
        assert event.payload["body"] == "hello mesh"

    async def test_simulate_inbound_accepts_text_message_app(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="real symbolic text")
        packet["decoded"]["portnum"] = "TEXT_MESSAGE_APP"
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "real symbolic text"
        assert inbound_collector.events[0].payload["portnum"] == "text_message"

    async def test_simulate_inbound_without_start_raises(self) -> None:
        adapter = FakeMeshtasticAdapter()
        packet = make_meshtastic_text_packet()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(packet)

    async def test_simulate_inbound_ignores_non_text(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0

    @pytest.mark.parametrize(
        "portnum",
        ["TELEMETRY_APP", "POSITION_APP", "NODEINFO_APP", "ADMIN_APP"],
    )
    async def test_simulate_inbound_ignores_symbolic_unsupported(
        self, make_adapter_context, portnum
    ) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": portnum},
        }
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0

    async def test_simulate_inbound_ignores_ack(self, make_adapter_context) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "text_message_ack"},
        }
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0

    async def test_simulate_inbound_ignores_routing_ack(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NONE"},
            },
        }
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0


# ===================================================================
# make_text_event helper
# ===================================================================


class TestFakeMeshtasticAdapterMakeTextEvent:
    """make_text_event creates valid canonical events from packet data."""

    def test_make_text_event_creates_canonical_event(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = adapter.make_text_event(body="ping")
        assert isinstance(event, CanonicalEvent)
        assert event.payload["body"] == "ping"

    def test_make_text_event_sets_source_adapter(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = adapter.make_text_event()
        assert event.source_adapter == "fake_meshtastic"

    def test_make_text_event_populates_native_ref(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = adapter.make_text_event(packet_id=999)
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "999"

    def test_make_text_event_with_sender(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = adapter.make_text_event(sender="!custom_node")
        assert event.source_transport_id == "!custom_node"

    def test_make_text_event_with_channel(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = adapter.make_text_event(channel=3)
        assert event.source_channel_id == "3"


class TestFakeMeshtasticStructuredDelivery:
    """FakeMeshtasticAdapter structured reply/reaction delivery."""

    async def test_deliver_preserves_reply_id(self) -> None:
        """Fake delivery preserves reply_id in sent packets and metadata."""
        adapter = FakeMeshtasticAdapter()
        result = RenderingResult(
            event_id="evt-r1",
            target_adapter="fake_mesh",
            target_channel="0",
            payload={"text": "reply", "channel_index": 0, "reply_id": 99},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        packet = adapter.fake_client.sent_packets[-1]
        assert packet.get("reply_id") == 99
        assert delivery.metadata["meshtastic"].get("reply_id") == 99
        assert delivery.metadata["meshtastic"]["packet_id"] == packet["packet_id"]
        assert delivery.metadata["meshtastic"]["channel"] == 0

    async def test_deliver_preserves_emoji(self) -> None:
        """Fake delivery preserves emoji=1 in sent packets and metadata."""
        adapter = FakeMeshtasticAdapter()
        result = RenderingResult(
            event_id="evt-r2",
            target_adapter="fake_mesh",
            target_channel="0",
            payload={"text": "🔥", "channel_index": 0, "reply_id": 10, "emoji": 1},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        packet = adapter.fake_client.sent_packets[-1]
        assert packet.get("reply_id") == 10
        assert packet.get("emoji") == 1
        assert delivery.metadata["meshtastic"].get("reply_id") == 10
        assert delivery.metadata["meshtastic"].get("emoji") == 1
        assert delivery.metadata["meshtastic"]["packet_id"] == packet["packet_id"]
        assert delivery.metadata["meshtastic"]["channel"] == 0

    async def test_plain_deliver_unchanged(self) -> None:
        """Plain text delivery without reply_id/emoji remains unchanged."""
        adapter = FakeMeshtasticAdapter()
        result = RenderingResult(
            event_id="evt-r3",
            target_adapter="fake_mesh",
            target_channel="0",
            payload={"text": "plain hello", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        packet = adapter.fake_client.sent_packets[-1]
        assert "reply_id" not in packet
        assert "emoji" not in packet
        assert delivery.metadata["meshtastic"]["packet_id"] == packet["packet_id"]
        assert delivery.metadata["meshtastic"]["channel"] == 0
        assert "reply_id" not in delivery.metadata["meshtastic"]
        assert "emoji" not in delivery.metadata
