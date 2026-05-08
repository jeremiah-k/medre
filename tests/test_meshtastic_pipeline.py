"""Tests for Meshtastic adapter pipeline integration: ingress through the
pipeline with Meshtastic adapters, renderer registration, and end-to-end
event flow.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import os
from datetime import datetime, timezone

import pytest

from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning.delivery_plan import DeliveryPlan
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner


def _make_text_packet(
    text: str = "hello pipeline",
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


async def _make_pipeline(
    mesh_adapter: FakeMeshtasticAdapter,
    rendering_pipeline: RenderingPipeline | None = None,
) -> PipelineRunner:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()

    route = Route(
        id="mesh-route",
        source=RouteSource(
            adapter="fake_meshtastic",
            event_kinds=("message.created",),
            channel="0",
        ),
        targets=[RouteTarget(adapter="fake_meshtastic_out", channel="0")],
    )
    router = Router(routes=[route])

    # Create an outbound fake adapter to receive deliveries
    out_config = MeshtasticConfig(adapter_id="fake_meshtastic_out")
    out_adapter = FakeMeshtasticAdapter(out_config)

    rp = rendering_pipeline or RenderingPipeline()
    rp.register(MeshtasticRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={
            "fake_meshtastic": mesh_adapter,
            "fake_meshtastic_out": out_adapter,
        },
        event_bus=EventBus(),
        rendering_pipeline=rp,
    )

    runner = PipelineRunner(config)
    return runner


class TestMeshtasticPipelineIntegration:
    """Pipeline integration with Meshtastic adapters."""

    async def test_meshtastic_renderer_registered(self) -> None:
        """MeshtasticRenderer can be registered in the rendering pipeline."""
        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

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

        result = await rp.render(event, "meshtastic_node")
        assert result.payload["text"] == "hello"
        assert result.metadata["renderer"] == "meshtastic"

    async def test_text_renderer_fallback_for_non_meshtastic(self) -> None:
        """TextRenderer handles events for non-Meshtastic adapters."""
        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        event = CanonicalEvent(
            event_id="evt-2",
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

        result = await rp.render(event, "some_other_adapter")
        assert result.metadata["renderer"] == "text"

    async def test_inbound_meshtastic_event_has_native_ref(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound Meshtastic events preserve native refs through simulation."""
        config = MeshtasticConfig(adapter_id="mesh-test")
        adapter = FakeMeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-test")
        await adapter.start(ctx)

        packet = _make_text_packet(packet_id=77777)
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "77777"
        assert event.source_native_ref.adapter == "mesh-test"

    async def test_inbound_meshtastic_event_kind(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound Meshtastic text packets decode as message.created."""
        config = MeshtasticConfig(adapter_id="mesh-test")
        adapter = FakeMeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-test")
        await adapter.start(ctx)

        packet = _make_text_packet()
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.event_kind == "message.created"

    async def test_pipeline_does_not_call_meshtastic_sleep(self) -> None:
        """Pipeline does not perform Meshtastic-specific sleeping.

        The queue owns pacing; the pipeline never calls asyncio.sleep
        for Meshtastic-specific delays.
        """
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.5)
        await queue.enqueue({"text": "test"}, 0)
        assert queue.pending_count == 1

        # process_one in tranche 1 is a no-op (no sleep)
        import time
        t0 = time.monotonic()
        result = await queue.process_one()
        elapsed = time.monotonic() - t0

        assert result is None
        # If there were a sleep(0.5), this would take >= 0.5s
        assert elapsed < 0.1
