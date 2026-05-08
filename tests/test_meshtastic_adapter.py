"""Tests for FakeMeshtasticAdapter and MeshtasticAdapter: capabilities,
lifecycle (start/stop), delivery contract, inbound simulation, rendering
boundary enforcement, and packet simulation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from medre.adapters import AdapterRole, FakeMeshtasticAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata
from medre.adapters.meshtastic.errors import MeshtasticSendError
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

    def test_platform_is_fake_meshtastic(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.platform == "fake_meshtastic"

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
        with pytest.raises(TypeError, match="RenderingResult only"):
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

    async def test_deliver_returns_none_in_tranche1(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is None

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
        with pytest.raises(TypeError, match="RenderingResult only"):
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


# ===================================================================
# Task scheduling (Blocker 7)
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

    async def test_no_ensure_future(self) -> None:
        """Verify _on_packet does not use asyncio.ensure_future."""
        import inspect
        source = inspect.getsource(MeshtasticAdapter._on_packet)
        assert "ensure_future" not in source
        assert "create_task" in source
