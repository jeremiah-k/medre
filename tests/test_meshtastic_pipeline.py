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
from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events import CanonicalEvent, EventMetadata, NativeMessageRef
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


def _make_adapter_context_for_pipeline(
    adapter_id: str, runner: PipelineRunner
) -> Any:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    from medre.adapters.base import AdapterContext
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


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


# ===================================================================
# Native ref persistence tests (Blocker 9)
# ===================================================================


class TestMeshtasticNativeRefPersistence:
    """Pipeline integration tests for native ref persistence."""

    async def test_inbound_native_ref_persisted(
        self, temp_storage
    ) -> None:
        """Inbound Meshtastic event → pipeline store → NativeMessageRef(direction="inbound")."""
        config = MeshtasticConfig(adapter_id="mesh-inbound")
        adapter = FakeMeshtasticAdapter(config)

        route = Route(
            id="mesh-loopback",
            source=RouteSource(
                adapter="mesh-inbound",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-inbound": adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-inbound", runner)
        await adapter.start(ctx)

        packet = _make_text_packet(packet_id=55555, channel=2)
        await adapter.simulate_inbound(packet)

        # Verify native ref persisted via resolve_native_ref
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-inbound",
            native_channel_id="2",
            native_message_id="55555",
        )
        assert resolved is not None
        assert resolved == adapter.inbound_events[0].event_id

    async def test_outbound_native_ref_persisted(
        self, temp_storage
    ) -> None:
        """Outbound FakeMeshtasticAdapter deliver → pipeline store → NativeMessageRef(direction="outbound")."""
        in_config = MeshtasticConfig(adapter_id="mesh-in")
        out_config = MeshtasticConfig(adapter_id="mesh-out")
        in_adapter = FakeMeshtasticAdapter(in_config)
        out_adapter = FakeMeshtasticAdapter(out_config)

        route = Route(
            id="mesh-route",
            source=RouteSource(
                adapter="mesh-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-in": in_adapter, "mesh-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(text="outbound test", packet_id=11111)
        await in_adapter.simulate_inbound(packet)

        # Verify outbound native ref persisted via resolve_native_ref
        # FakeMeshtasticClient first send gets packet_id=1
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-out",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is not None
        assert resolved == in_adapter.inbound_events[0].event_id

    async def test_failed_delivery_no_outbound_native_ref(
        self, temp_storage
    ) -> None:
        """Failed deliver → no outbound native ref in storage."""
        in_config = MeshtasticConfig(adapter_id="mesh-fail-in")
        out_config = MeshtasticConfig(adapter_id="mesh-fail-out")
        in_adapter = FakeMeshtasticAdapter(in_config)
        out_adapter = FakeMeshtasticAdapter(out_config)
        out_adapter.set_deliver_failure(True)

        route = Route(
            id="mesh-fail-route",
            source=RouteSource(
                adapter="mesh-fail-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mesh-fail-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-fail-in": in_adapter, "mesh-fail-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-fail-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(text="fail test", packet_id=22222)
        await in_adapter.simulate_inbound(packet)

        # Verify no outbound native ref from failed delivery
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-fail-out",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is None

        # Inbound ref should still exist
        inbound_resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-fail-in",
            native_channel_id="0",
            native_message_id="22222",
        )
        assert inbound_resolved is not None

    async def test_duplicate_inbound_native_ref_idempotent(
        self, temp_storage
    ) -> None:
        """Duplicate inbound native refs are idempotent (INSERT OR IGNORE)."""
        config = MeshtasticConfig(adapter_id="mesh-dup")
        adapter = FakeMeshtasticAdapter(config)

        route = Route(
            id="mesh-dup-route",
            source=RouteSource(
                adapter="mesh-dup",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-dup": adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-dup", runner)
        await adapter.start(ctx)

        packet = _make_text_packet(packet_id=33333)
        await adapter.simulate_inbound(packet)

        # Manually store a duplicate native ref — should be idempotent
        from medre.core.events.canonical import NativeMessageRef
        import uuid as _uuid
        from datetime import timezone as _tz

        event = adapter.inbound_events[0]
        dup_ref = NativeMessageRef(
            id=f"nref-dup-{_uuid.uuid4()}",
            event_id=event.event_id,
            adapter="mesh-dup",
            native_channel_id="0",
            native_message_id="33333",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(tz=_tz.utc),
        )
        # This should NOT raise despite the same (adapter, channel, msg_id) triple
        await temp_storage.store_native_ref(dup_ref)

        # Should still resolve to the same event
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-dup",
            native_channel_id="0",
            native_message_id="33333",
        )
        assert resolved is not None
        assert resolved == event.event_id
