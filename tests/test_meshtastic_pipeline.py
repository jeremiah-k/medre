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
        targets=[RouteTarget(adapter="mesh-out", channel="0")],
    )
    router = Router(routes=[route])

    # Create an outbound fake adapter to receive deliveries.
    # Use a realistic adapter ID (not starting with "meshtastic") to
    # prove renderer selection works without prefix dependency.
    out_adapter_id = "mesh-out"
    out_config = MeshtasticConfig(adapter_id=out_adapter_id)
    out_adapter = FakeMeshtasticAdapter(out_config)

    rp = rendering_pipeline or RenderingPipeline()
    rp.register(MeshtasticRenderer(known_adapters={out_adapter_id}), priority=50)
    rp.register(TextRenderer(), priority=100)

    config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={
            "fake_meshtastic": mesh_adapter,
            out_adapter_id: out_adapter,
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

    async def test_outbound_delivery_uses_meshtastic_renderer(
        self, temp_storage
    ) -> None:
        """Outbound delivery to realistic Meshtastic IDs uses MeshtasticRenderer,
        not TextRenderer. Proves can_render selection via known_adapters works."""
        in_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="radio-in"))
        out_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="local-radio"))

        route = Route(
            id="mesh-renderer-check",
            source=RouteSource(
                adapter="radio-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="local-radio", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(known_adapters={"local-radio"}), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"radio-in": in_adapter, "local-radio": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("radio-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(text="renderer check 42", packet_id=9999)
        await in_adapter.simulate_inbound(packet)

        # After pipeline delivery, verify the outbound router's delivered_payloads
        assert len(out_adapter.delivered_payloads) == 1
        payload = out_adapter.delivered_payloads[0]

        # CRITICAL: Prove MeshtasticRenderer rendered this, not TextRenderer.
        # TextRenderer produces {"text": ...}; MeshtasticRenderer produces
        # {"text": ..., "channel_index": ..., "meshnet_name": ...}.
        assert payload.metadata["renderer"] == "meshtastic"
        assert "channel_index" in payload.payload
        assert "meshnet_name" in payload.payload

        # Outbound native ref should also persist
        resolved = await temp_storage.resolve_native_ref(
            adapter="local-radio",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is not None

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
        rp.register(MeshtasticRenderer(known_adapters={"mesh-out"}), priority=50)
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
        rp.register(MeshtasticRenderer(known_adapters={"mesh-fail-out"}), priority=50)
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


# ===================================================================
# Reply relation pipeline tests
# ===================================================================


class TestMeshtasticReplyRelation:
    """Reply relation resolution through the pipeline."""

    async def test_inbound_reply_creates_unresolved_relation(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound reply packet goes through adapter → codec → event with unresolved relation.
        The codec creates an EventRelation with target_event_id=None and a
        target_native_ref. The pipeline stores the event with the unresolved relation.
        Relation resolution happens in pipeline Stage 2 but if the target ref doesn't
        exist in storage, the relation stays unresolved (target_event_id=None)."""
        config = MeshtasticConfig(adapter_id="mesh-reply")
        adapter = FakeMeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-reply")
        await adapter.start(ctx)

        # A reply packet: packet_id=200, replyId=100
        packet = {
            "fromId": "!node1",
            "toId": "",
            "channel": 0,
            "id": 200,
            "decoded": {"portnum": "text_message", "text": "reply", "replyId": 100},
        }
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        event = inbound_collector.events[0]
        # Codec should have created a reply EventRelation
        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reply"
        # Target not yet in storage → unresolved
        assert rel.target_event_id is None
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "100"
        assert rel.target_native_ref.adapter == "mesh-reply"

    async def test_inbound_reply_resolved_through_pipeline(
        self, temp_storage
    ) -> None:
        """When the target native ref already exists in storage, the pipeline resolves
        the relation before the event is published (pipeline Stage 2: resolve_relations).

        This test uses a PipelineRunner with a RelationResolver so that the pipeline
        resolves relations during event processing. The relation resolution happens
        in pipeline.handle_event() Stage 2 BEFORE the event is published to inbound."""
        in_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="mesh-source"))

        route = Route(
            id="mesh-reply-route",
            source=RouteSource(
                adapter="mesh-source",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-source": in_adapter},
            event_bus=EventBus(),
            rendering_pipeline=RenderingPipeline(),
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-source", runner)
        await in_adapter.start(ctx)

        # Send a message with packet_id=999 — this will persist a native ref
        first_packet = {
            "fromId": "!node1",
            "toId": "",
            "channel": 0,
            "id": 999,
            "decoded": {"portnum": "text_message", "text": "original"},
        }
        await in_adapter.simulate_inbound(first_packet)

        # Verify the pipeline persisted the inbound native ref
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-source",
            native_channel_id="0",
            native_message_id="999",
        )
        assert resolved is not None, "Pipeline should have persisted inbound native ref"

        # Now send a reply packet referencing the first message
        reply_packet = {
            "fromId": "!node2",
            "toId": "",
            "channel": 0,
            "id": 1001,
            "decoded": {"portnum": "text_message", "text": "reply to 999", "replyId": 999},
        }
        await in_adapter.simulate_inbound(reply_packet)

        # The stored event has resolved relations (pipeline resolves before storing).
        # inbound_events captures pre-pipeline events, so we must check storage.
        reply_event_id = in_adapter.inbound_events[1].event_id
        stored_event = await temp_storage.get(reply_event_id)
        assert stored_event is not None, "Reply event should be stored"

        assert len(stored_event.relations) == 1
        rel = stored_event.relations[0]
        assert rel.relation_type == "reply"
        # The pipeline should have resolved target_event_id
        assert rel.target_event_id == resolved, (
            f"Pipeline should resolve reply's target_native_ref ({rel.target_native_ref}) "
            f"to event_id={resolved}, got target_event_id={rel.target_event_id}"
        )
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "999"

    async def test_reply_relation_without_target_is_unresolved(
        self, temp_storage
    ) -> None:
        """A reply referencing a non-existent native ref stays unresolved.
        The pipeline does not crash, and the relation is preserved."""
        in_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="mesh-orphan"))

        route = Route(
            id="mesh-orphan-route",
            source=RouteSource(
                adapter="mesh-orphan",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"mesh-orphan": in_adapter},
            event_bus=EventBus(),
            rendering_pipeline=RenderingPipeline(),
        ))

        ctx = _make_adapter_context_for_pipeline("mesh-orphan", runner)
        await in_adapter.start(ctx)

        # Reply to a packet ID that was never sent
        packet = {
            "fromId": "!node1",
            "toId": "",
            "channel": 0,
            "id": 500,
            "decoded": {"portnum": "text_message", "text": "orphan reply", "replyId": 99999},
        }
        # Should NOT raise — pipeline handles unresolved relations gracefully
        await in_adapter.simulate_inbound(packet)

        assert len(in_adapter.inbound_events) == 1
        event = in_adapter.inbound_events[0]
        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reply"
        # Target event does not exist → stays unresolved
        assert rel.target_event_id is None
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "99999"


# ===================================================================
# Platform-aware renderer selection tests
# ===================================================================


class TestMeshtasticPlatformRendererSelection:
    """Prove platform-aware renderer selection works for Meshtastic
    without relying on adapter-name prefixes or known_adapters."""

    async def test_platform_aware_renderer_selection(
        self, temp_storage
    ) -> None:
        """A realistic Meshtastic adapter ID that does NOT start with 'meshtastic'
        still selects MeshtasticRenderer through the pipeline's platform registry.

        This proves:
        - FakeMeshtasticAdapter.platform == "meshtastic" drives dispatch
        - The RenderingPipeline platform registry maps adapter_id -> platform
        - MeshtasticRenderer.can_render matches on target_platform == "meshtastic"
        - TextRenderer is NOT selected for Meshtastic routes
        - known_adapters is NOT required
        """
        # 1. Create adapters with realistic IDs that do NOT start with "meshtastic"
        in_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="local-node"))
        in_adapter.platform = "meshtastic"

        out_adapter = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="radio-out"))
        out_adapter.platform = "meshtastic"

        # 2. Route: local-node -> radio-out
        route = Route(
            id="platform-registry-route",
            source=RouteSource(
                adapter="local-node",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="radio-out", channel="0")],
        )
        router = Router(routes=[route])

        # 3. RenderingPipeline with MeshtasticRenderer — NO known_adapters (critical!)
        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        # 4. PipelineRunner — start() calls _populate_renderer_platforms()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"local-node": in_adapter, "radio-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))
        await runner.start()

        # 5. Wire inbound adapter
        ctx = _make_adapter_context_for_pipeline("local-node", runner)
        await in_adapter.start(ctx)

        # 6. Send inbound packet
        packet = _make_text_packet(text="platform dispatch test", packet_id=99999)
        await in_adapter.simulate_inbound(packet)

        # 7. Assertions

        # Outbound adapter received the rendered payload
        assert len(out_adapter.delivered_payloads) == 1
        result = out_adapter.delivered_payloads[0]

        # Proves MeshtasticRenderer was selected (not TextRenderer)
        assert result.metadata["renderer"] == "meshtastic"

        # Proves Meshtastic payload shape (channel_index + meshnet_name)
        assert "channel_index" in result.payload
        assert "meshnet_name" in result.payload

        # Outbound delivery returned a deterministic native_message_id
        assert out_adapter.fake_client.sent_count == 1
        sent_packet_id = out_adapter.fake_client.sent_packets[0]["packet_id"]

        # Native ref was persisted in storage
        resolved = await temp_storage.resolve_native_ref(
            adapter="radio-out",
            native_channel_id="0",
            native_message_id=str(sent_packet_id),
        )
        assert resolved is not None
