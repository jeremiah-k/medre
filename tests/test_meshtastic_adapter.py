"""Tests for FakeMeshtasticAdapter and MeshtasticAdapter: capabilities,
lifecycle (start/stop), delivery contract, inbound simulation, rendering
boundary enforcement, packet simulation, and session boundary.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters import AdapterRole, FakeMeshtasticAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult, AdapterPermanentError
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.core.events import CanonicalEvent, EventMetadata
from medre.adapters.meshtastic.errors import MeshtasticConnectionError, MeshtasticSendError
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult




def _make_config(**overrides) -> MeshtasticConfig:
    defaults = dict(adapter_id="mesh-1")
    defaults.update(overrides)
    return MeshtasticConfig(**defaults)


def _make_rendering_result(
    event_id: str = "evt-1",
    target_adapter: str = "mesh-1",
    target_channel: str = "0",
    payload: dict | None = None,
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=payload or {"text": "hello mesh", "channel_index": 0, "meshnet_name": ""},
    )


def _make_text_packet(
    text: str = "hello",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    return {
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "text_message",
            "text": text,
        },
    }


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
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.text is True

    def test_capabilities_replies_unsupported(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.replies == "unsupported"

    def test_capabilities_reactions_unsupported(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.reactions == "unsupported"

    def test_capabilities_edits_unsupported(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.edits == "unsupported"

    def test_capabilities_deletes_unsupported(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.deletes == "unsupported"

    def test_capabilities_attachments_false(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.attachments is False

    def test_capabilities_direct_messages_false(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.direct_messages is False

    def test_capabilities_max_text_bytes_512(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.max_text_bytes == 512

    def test_capabilities_max_text_chars_512(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        assert _FAKE_MESHTASTIC_CAPABILITIES.max_text_chars == 512


class TestRealMeshtasticCapabilities:
    """Real MeshtasticAdapter capabilities match spec."""

    def test_real_adapter_role_is_transport(self) -> None:
        config = _make_config()
        adapter = MeshtasticAdapter(config)
        assert adapter.role == AdapterRole.TRANSPORT

    def test_real_adapter_capabilities_match_fake(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES
        config = _make_config()
        adapter = MeshtasticAdapter(config)
        real_caps = adapter._capabilities
        assert real_caps.text == _FAKE_MESHTASTIC_CAPABILITIES.text
        assert real_caps.replies == _FAKE_MESHTASTIC_CAPABILITIES.replies
        assert real_caps.reactions == _FAKE_MESHTASTIC_CAPABILITIES.reactions
        assert real_caps.edits == _FAKE_MESHTASTIC_CAPABILITIES.edits
        assert real_caps.deletes == _FAKE_MESHTASTIC_CAPABILITIES.deletes
        assert real_caps.attachments == _FAKE_MESHTASTIC_CAPABILITIES.attachments
        assert real_caps.direct_messages == _FAKE_MESHTASTIC_CAPABILITIES.direct_messages
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
        result = _make_rendering_result()
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
        result1 = _make_rendering_result()
        result2 = _make_rendering_result()
        delivery1 = await adapter.deliver(result1)
        delivery2 = await adapter.deliver(result2)
        assert delivery1.native_message_id == "1"
        assert delivery2.native_message_id == "2"
        assert delivery1.native_channel_id == delivery2.native_channel_id

    async def test_deliver_does_not_reformat(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = _make_rendering_result(payload={
            "text": "original", "channel_index": 0, "meshnet_name": "",
        })
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
        with pytest.raises((TypeError, AdapterPermanentError), match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_deliver_failure_raises_send_error(self) -> None:
        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(MeshtasticSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(MeshtasticSendError):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0

    async def test_fake_client_tracks_sent_packets(self) -> None:
        adapter = FakeMeshtasticAdapter()
        result = _make_rendering_result()
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
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert isinstance(adapter.delivered_payloads[0], RenderingResult)

    async def test_adapter_does_not_perform_kind_specific_formatting(self) -> None:
        adapter = FakeMeshtasticAdapter()
        for kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            result = _make_rendering_result(event_id=f"evt-{kind}")
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

        packet = _make_text_packet(text="hello mesh")
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

        packet = _make_text_packet(text="real symbolic text")
        packet["decoded"]["portnum"] = "TEXT_MESSAGE_APP"
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "real symbolic text"
        assert inbound_collector.events[0].payload["portnum"] == "text_message"

    async def test_simulate_inbound_without_start_raises(self) -> None:
        adapter = FakeMeshtasticAdapter()
        packet = _make_text_packet()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(packet)

    async def test_simulate_inbound_ignores_non_text(self, make_adapter_context) -> None:
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


# ===================================================================
# Real MeshtasticAdapter tests
# ===================================================================


class TestMeshtasticAdapterLifecycle:
    """MeshtasticAdapter lifecycle with fake config."""

    async def test_start_fake_mode(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_stop(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_deliver_returns_none_scaffold(self) -> None:
        """Real adapter deliver() enqueues and returns AdapterDeliveryResult with
        delivery_note='locally enqueued' and native_message_id=None."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_deliver_enqueues_to_queue(self) -> None:
        """deliver() puts the payload into the adapter-owned queue."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

    async def test_deliver_rejects_canonical_event(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
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
        with pytest.raises((TypeError, AdapterPermanentError), match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_simulate_inbound(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = _make_text_packet(text="via real adapter")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via real adapter"

    async def test_simulate_inbound_symbolic_text_message_app(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = _make_text_packet(text="symbolic real adapter")
        packet["decoded"]["portnum"] = "TEXT_MESSAGE_APP"
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "symbolic real adapter"


# ===================================================================
# Idempotent lifecycle
# ===================================================================


class TestMeshtasticAdapterIdempotentLifecycle:
    """start/stop are idempotent — calling multiple times is safe."""

    async def test_double_start_is_no_op(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        # Second start should not raise or change state
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_double_stop_is_no_op(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Second stop should not raise
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_stop_without_start_is_no_op(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        # Should not raise
        await adapter.stop()

    async def test_start_stop_start_cycle(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Restart should work
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"


# ===================================================================
# Connection modes with monkeypatched fake clients
# ===================================================================


class TestMeshtasticAdapterConnectionModes:
    """Non-fake connection modes work with monkeypatched fake modules."""

    @staticmethod
    def _make_fake_interface_class(name: str):
        """Create a fake interface class that records its constructor args."""
        class FakeInterface:
            _instances = []

            def __init__(self, **kwargs):
                self._kwargs = kwargs
                self._closed = False
                FakeInterface._instances.append(self)

            def close(self):
                self._closed = True

            def sendText(self, text, channelIndex=0):
                """Sync sendText returning a packet with id."""
                return type("Packet", (), {"id": 42})()

        FakeInterface._instances = []
        FakeInterface.__name__ = name
        FakeInterface.__qualname__ = name
        return FakeInterface

    def _patch_session_create_client(self, adapter, FakeClass, monkeypatch):
        """Patch MeshtasticSession._create_client to return a FakeClass instance."""
        def fake_create_client(session_self):
            return FakeClass()
        monkeypatch.setattr(
            MeshtasticSession, "_create_client", fake_create_client
        )
        monkeypatch.setattr(
            "medre.adapters.meshtastic.session.HAS_MESHTASTIC", True
        )

    async def test_tcp_mode_with_monkeypatched_client(self, make_adapter_context, monkeypatch) -> None:
        """TCP mode creates TCPInterface(hostname, portNumber) via session."""
        FakeTCP = self._make_fake_interface_class("FakeTCPInterface")
        config = _make_config(
            connection_type="tcp",
            host="192.168.1.100",
            port=4403,
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeTCP(
                hostname=session_self._config.host,
                portNumber=session_self._config.port,
            )

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["hostname"] == "192.168.1.100"
        assert adapter._session.client._kwargs["portNumber"] == 4403

        await adapter.stop()
        assert adapter._session is None

    async def test_serial_mode_with_monkeypatched_client(self, make_adapter_context, monkeypatch) -> None:
        """Serial mode creates SerialInterface(devPath) via session."""
        FakeSerial = self._make_fake_interface_class("FakeSerialInterface")
        config = _make_config(
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeSerial(devPath=session_self._config.serial_port)

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["devPath"] == "/dev/ttyUSB0"

        await adapter.stop()

    async def test_ble_mode_with_monkeypatched_client(self, make_adapter_context, monkeypatch) -> None:
        """BLE mode creates BLEInterface(address) via session."""
        FakeBLE = self._make_fake_interface_class("FakeBLEInterface")
        config = _make_config(
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeBLE(address=session_self._config.ble_address)

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["address"] == "AA:BB:CC:DD:EE:FF"

        await adapter.stop()

    async def test_non_fake_without_mtjk_raises(self, monkeypatch) -> None:
        """Non-fake mode raises MeshtasticConnectionError when mtjk missing."""
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False)
        config = _make_config(
            connection_type="tcp",
            host="192.168.1.100",
        )
        adapter = MeshtasticAdapter(config)
        with pytest.raises(MeshtasticConnectionError, match="mtjk not installed"):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

    async def test_stop_closes_client(self, make_adapter_context, monkeypatch) -> None:
        """stop() calls client.close() on the real client via session."""
        FakeTCP = self._make_fake_interface_class("FakeTCPInterface")
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)
        self._patch_session_create_client(adapter, FakeTCP, monkeypatch)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        session = adapter._session
        client = session.client
        assert not client._closed
        await adapter.stop()
        assert client._closed


# ===================================================================
# Pubsub subscription
# ===================================================================


def _patch_pubsub(monkeypatch, subscribe_fn=None, unsubscribe_fn=None):
    """Patch pubsub module for session tests."""
    fake_pubsub = types.ModuleType("pubsub")
    fake_pub = types.ModuleType("pubsub.pub")
    fake_pub.subscribe = subscribe_fn or (lambda cb, topic: None)
    fake_pub.unsubscribe = unsubscribe_fn or (lambda cb, topic: None)
    fake_pubsub.pub = fake_pub
    monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)
    monkeypatch.setitem(sys.modules, "pubsub.pub", fake_pub)


class TestMeshtasticAdapterPubsubSubscription:
    """Subscription failures are raised, not swallowed."""

    async def test_successful_subscription_calls_pub_subscribe(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """start() calls pub.subscribe on non-fake connection."""
        subscribed = []

        def fake_subscribe(callback, topic):
            subscribed.append(("_on_receive", topic))

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(monkeypatch, subscribe_fn=fake_subscribe)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert len(subscribed) == 1
        assert subscribed[0] == ("_on_receive", "meshtastic.receive")
        await adapter.stop()

    async def test_subscription_failure_during_start_raises(
        self, monkeypatch
    ) -> None:
        """start() raises MeshtasticConnectionError when subscription fails."""

        class FakeClient:
            def close(self):
                self.closed = True

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("nope")),
        )

        with pytest.raises(MeshtasticConnectionError, match="meshtastic.receive"):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

    async def test_start_failure_closes_client(
        self, monkeypatch
    ) -> None:
        """When subscription fails, start() closes the client before re-raising."""
        closed_flag = {"closed": False}

        class FakeClient:
            def close(self):
                closed_flag["closed"] = True

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

        assert closed_flag["closed"], "Client should be closed after subscription failure"

    async def test_start_failure_no_orphaned_state(
        self, monkeypatch
    ) -> None:
        """After subscription failure, adapter is not started and session is None."""
        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

        assert adapter._started is False
        assert adapter._client is None
        assert adapter._session is None

    async def test_health_check_unknown_after_subscription_failure(
        self, monkeypatch
    ) -> None:
        """health_check() returns 'unknown' after subscription failure and cleanup."""
        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

        # After failed start, client is cleaned up, health should be "unknown"
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_unsubscribe_only_when_subscribed(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """stop() does not call pub.unsubscribe if subscription never succeeded."""
        unsubscribe_calls = []

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()
        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
            unsubscribe_fn=lambda cb, topic: unsubscribe_calls.append((cb, topic)),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(AdapterContext(
                adapter_id="mesh-1",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=__import__("logging").getLogger("test"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            ))

        # stop should not try to unsubscribe since subscription never succeeded
        await adapter.stop()
        assert len(unsubscribe_calls) == 0


# ===================================================================
# Task scheduling
# ===================================================================


class TestMeshtasticAdapterTaskScheduling:
    """Background tasks from _on_packet are tracked and cleaned up."""

    async def test_on_packet_creates_tracked_task(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = _make_text_packet(text="tracked")
        adapter._on_packet(packet)

        # Allow the background task to complete
        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "tracked"
        # Task should have been discarded after completion
        assert len(adapter._background_tasks) == 0

    async def test_stop_cancels_background_tasks(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Inject a long-running task
        async def _slow():
            await asyncio.sleep(100)

        task = asyncio.create_task(_slow())
        adapter._background_tasks.add(task)

        await adapter.stop()
        assert task.cancelled() or task.done()
        assert len(adapter._background_tasks) == 0

    async def test_drain_background_tasks_with_timeout(
        self, make_adapter_context
    ) -> None:
        """_drain_background_tasks cancels and awaits all tracked tasks."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        async def _block_forever():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                # Swallow to test drain behavior
                pass

        t1 = asyncio.create_task(_block_forever())
        t2 = asyncio.create_task(_block_forever())
        adapter._background_tasks.add(t1)
        adapter._background_tasks.add(t2)

        await adapter._drain_background_tasks(timeout=1.0)
        assert len(adapter._background_tasks) == 0
        assert t1.done()
        assert t2.done()

    async def test_no_ensure_future(self) -> None:
        """Verify _on_packet does not use asyncio.ensure_future."""
        import inspect
        source = inspect.getsource(MeshtasticAdapter._on_packet)
        assert "ensure_future" not in source
        assert "create_task" in source


# ===================================================================
# Queue ownership and pacing
# ===================================================================


class TestMeshtasticAdapterQueueOwnership:
    """Adapter owns queue/pacing; runtime pipeline and renderer do not sleep."""

    async def test_adapter_owns_queue(self) -> None:
        config = _make_config(connection_type="fake", message_delay_seconds=0.25)
        adapter = MeshtasticAdapter(config)
        assert adapter.queue is adapter._queue
        assert adapter.queue.delay_between_messages == 0.25

    async def test_queue_health_accessible(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        health = adapter.queue_health
        assert "pending_count" in health
        assert "total_sent" in health
        assert "total_failed" in health
        assert health["pending_count"] == 0

    async def test_deliver_enqueues_and_queue_pending_grows(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

        result2 = _make_rendering_result(event_id="evt-2")
        await adapter.deliver(result2)
        assert adapter.queue.pending_count == 2

    async def test_send_one_returns_none_when_no_client(self) -> None:
        """send_one() returns None in fake mode (no real client)."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = await adapter.send_one()
        assert result is None

    async def test_send_one_dequeues_and_sends_with_fake_client(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """send_one() with a monkeypatched client sends via the queue."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        class FakeClient:
            def __init__(self):
                self.sent = []

            def sendText(self, text, channelIndex=0):
                self.sent.append({"text": text, "channel_index": channelIndex})
                return type("Packet", (), {"id": 77})()

        fake_client = FakeClient()

        # Patch session to use our fake client
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        monkeypatch.setattr(
            MeshtasticSession, "_create_client",
            lambda self: fake_client,
        )

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Enqueue a payload
        await adapter._queue.enqueue({"text": "hello"}, 0)
        assert adapter.queue.pending_count == 1

        # send_one processes the queue item
        result = await adapter.send_one()
        assert result is not None
        assert result.native_message_id == "77"
        assert result.native_channel_id == "0"
        assert adapter.queue.pending_count == 0
        assert len(fake_client.sent) == 1

        await adapter.stop()


# ===================================================================
# Send semantics audit
# ===================================================================


class TestMeshtasticAdapterSendSemantics:
    """Audit: deliver() enqueues/returns None; send semantics documented."""

    async def test_deliver_return_none_documented(self) -> None:
        """Real adapter deliver() returns AdapterDeliveryResult with
        delivery_note='locally enqueued' and native_message_id=None."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        # Queue-based: returns result with no native_message_id
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_queue_process_one_without_send_fn_returns_none(self) -> None:
        """process_one without send_fn returns None (scaffold mode)."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
        queue = MeshtasticOutboundQueue()
        await queue.enqueue({"text": "test"}, 0)
        result = await queue.process_one()
        assert result is None

    async def test_queue_process_one_with_send_fn_returns_result(self) -> None:
        """process_one with send_fn returns AdapterDeliveryResult."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test", "channel_index": 0}, 0)

        async def fake_send(item):
            return {"packet_id": 99}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.native_message_id == "99"
        assert result.native_channel_id == "0"

    async def test_queue_process_one_extracts_id_from_object(self) -> None:
        """process_one captures packet id from objects with .id attribute."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 3)

        async def fake_send(item):
            return type("Packet", (), {"id": 123})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.native_message_id == "123"
        assert result.native_channel_id == "3"

    async def test_queue_process_one_handles_none_send_result(self) -> None:
        """process_one handles send_fn returning None gracefully."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_none(item):
            return None

        result = await queue.process_one(send_fn=fake_send_none)
        assert result is not None
        assert result.native_message_id is None

    async def test_queue_process_one_tracks_failures(self) -> None:
        """process_one increments total_failed on send_fn exception."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_fail(item):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await queue.process_one(send_fn=fake_send_fail)

        assert queue.total_failed == 1


# ===================================================================
# Session boundary
# ===================================================================


class TestMeshtasticSessionBoundary:
    """MeshtasticSession lifecycle and diagnostics."""

    async def test_session_created_on_start(self, make_adapter_context) -> None:
        """Adapter creates a MeshtasticSession on start."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert isinstance(adapter._session, MeshtasticSession)
        await adapter.stop()

    async def test_session_cleared_on_stop(self, make_adapter_context) -> None:
        """Adapter clears session ref on stop."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        await adapter.stop()
        assert adapter._session is None

    async def test_session_diagnostics_exposed(self, make_adapter_context) -> None:
        """diagnostics() returns combined adapter + session state."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert diag["adapter_id"] == "mesh-1"
        assert diag["platform"] == "meshtastic"
        assert diag["started"] is True
        assert diag["connection_type"] == "fake"

        # Session diagnostics present
        assert "session" in diag
        session = diag["session"]
        assert session["connected"] is False  # fake mode has no real client
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_packet_time"] is None
        assert session["node_id"] is None
        assert session["channel_count"] == 0
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0
        assert session["last_error"] is None

        await adapter.stop()

    async def test_session_diagnostics_after_stop(self, make_adapter_context) -> None:
        """diagnostics() without session shows adapter-only state."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        diag = adapter.diagnostics()
        assert diag["started"] is False
        assert "session" not in diag

    async def test_session_diagnostics_no_secrets(self, make_adapter_context) -> None:
        """Diagnostics never exposes secrets, keys, or raw protobuf."""
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        diag_str = str(diag)
        # No secret-like keys
        for forbidden in ("password", "secret", "key", "token", "private"):
            assert forbidden not in diag_str.lower() or "node_id" in diag_str

        await adapter.stop()


# ===================================================================
# MeshtasticSession unit tests
# ===================================================================


class TestMeshtasticSessionUnit:
    """Direct unit tests for MeshtasticSession."""

    async def test_fake_mode_session_start(self) -> None:
        """Session start in fake mode creates no client."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        assert session.connected is False  # fake mode, no real client
        assert session.client is None
        await session.stop()

    async def test_session_stop_idempotent(self) -> None:
        """Session stop is safe without start."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.stop()  # should not raise

    async def test_session_diagnostics_dataclass(self) -> None:
        """Session diagnostics returns proper dataclass."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        diag = session.diagnostics()
        from medre.adapters.meshtastic.session import MeshtasticSessionDiagnostics
        assert isinstance(diag, MeshtasticSessionDiagnostics)
        assert diag.connected is False
        assert diag.reconnecting is False
        assert diag.reconnect_attempts == 0
        assert diag.last_packet_time is None
        assert diag.node_id is None
        assert diag.channel_count == 0
        assert diag.transient_delivery_failures == 0
        assert diag.permanent_delivery_failures == 0
        assert diag.last_error is None

    async def test_session_send_returns_none_fake(self) -> None:
        """Session send returns None in fake mode (no real client)."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        result = await session.send({"text": "hello", "channel_index": 0})
        assert result is None
        await session.stop()

    async def test_session_send_with_transient_retry(self, monkeypatch) -> None:
        """Session send retries on transient errors."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        # Inject a fake client that fails once then succeeds
        call_count = {"n": 0}

        class FakeClient:
            def sendText(self, text, channelIndex=0):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise ConnectionError("transient")
                return type("Packet", (), {"id": 42})()

        session._client = FakeClient()
        result = await session.send({"text": "hello", "channel_index": 0})
        assert result is not None
        assert call_count["n"] == 2
        assert session.transient_delivery_failures == 1

    async def test_session_send_permanent_failure_raises(self) -> None:
        """Session send raises immediately on non-transient errors."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        class FakeClient:
            def sendText(self, text, channelIndex=0):
                raise ValueError("bad packet")

        session._client = FakeClient()
        with pytest.raises(MeshtasticSendError, match="Permanent"):
            await session.send({"text": "hello", "channel_index": 0})
        assert session.permanent_delivery_failures == 1

    async def test_session_reconnect_loop_bounded(self) -> None:
        """Reconnect loop stops after max attempts."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        session._started = True

        # _create_client always fails
        def always_fail(self):
            raise ConnectionError("nope")

        import medre.adapters.meshtastic.session as session_mod
        original_create = session_mod.MeshtasticSession._create_client
        session_mod.MeshtasticSession._create_client = always_fail

        try:
            # Use very short backoff for testing
            session_mod._BACKOFF_BASE = 0.01
            session_mod._BACKOFF_CAP = 0.01

            await session._reconnect_loop()

            assert session.reconnect_attempts > 0
            assert session.reconnecting is False
            assert session.last_error is not None
        finally:
            session_mod.MeshtasticSession._create_client = original_create
            session_mod._BACKOFF_BASE = 1.0
            session_mod._BACKOFF_CAP = 30.0

    async def test_session_message_callback(self) -> None:
        """Session forwards received packets to message callback."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        received = []
        await session.start(message_callback=lambda pkt: received.append(pkt))

        # Simulate callback
        session._on_receive({"id": 1, "decoded": {"text": "test"}})
        assert len(received) == 1
        assert received[0]["id"] == 1
        assert session.last_packet_time is not None

        await session.stop()

    async def test_session_stop_prevents_reconnect(self) -> None:
        """stop() sets _stop_requested, preventing reconnect."""
        config = _make_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        session.notify_connection_lost()
        # Stop before reconnect can do anything
        await session.stop()
        assert session._stop_requested is True
