"""Tests for FakeMeshtasticAdapter and MeshtasticAdapter: capabilities,
lifecycle (start/stop), delivery contract, inbound simulation, rendering
boundary enforcement, and packet simulation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters import AdapterRole, FakeMeshtasticAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata
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

    def test_role_is_presentation(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.role == AdapterRole.PRESENTATION

    def test_platform_is_fake_meshtastic(self) -> None:
        adapter = FakeMeshtasticAdapter()
        assert adapter.platform == "fake_meshtastic"


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
        assert info.role == AdapterRole.PRESENTATION


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
        # Tranche 1: returns None (scaffolded)
        assert delivery is None

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
