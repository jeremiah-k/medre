"""Bridge tests: Meshtastic adapter wrapper <-> fake adapter via PipelineRunner.

These tests prove the real MeshtasticAdapter wrapper can bridge through the
MEDRE runtime pipeline without live meshtasticd or Docker.  They exercise
the actual codec, classifier, queue, renderer selection, session boundary,
and delivery receipt / native-ref semantics.

All tests use ``MeshtasticAdapter(connection_type="fake")`` or
monkeypatched session boundaries.  No network access required.

Test categories
---------------
1. **Meshtastic inbound -> fake outbound**: Real MeshtasticAdapter receives
   a simulated inbound packet, decodes it through the real codec/classifier,
   publishes through PipelineRunner, and the resulting RenderingResult is
   delivered to a FakeMeshtasticAdapter via MeshtasticRenderer.

2. **Fake inbound -> Meshtastic outbound/local enqueue**: A
   FakeMeshtasticAdapter publishes an inbound event through PipelineRunner;
   the pipeline renders via MeshtasticRenderer and calls the real
   MeshtasticAdapter.deliver(), which enqueues locally and returns
   ``AdapterDeliveryResult(native_message_id=None)``.  Tests verify
   the "locally enqueued" receipt is recorded but no outbound native ref
   is stored (because the queue-based adapter has no native message ID yet).

3. **Error mapping**: Verifies transient/permanent error propagation through
   the bridge when the Meshtastic adapter's queue or session fails.

4. **Session callback bridge**: Exercises the sync _on_packet -> asyncio
   task -> publish_inbound path with the real adapter.

5. **send_one bridge**: Exercises queue.process_one with a monkeypatched
   session client to prove the full outbound send path.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.adapters.base import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.errors import (
    MeshtasticConnectionError,
    MeshtasticSendError,
)
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.core.events import CanonicalEvent, EventMetadata, NativeMessageRef
from medre.core.events.bus import EventBus
from medre.core.planning.delivery_plan import DeliveryFailureKind, DeliveryOutcome
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_packet(
    text: str = "hello bridge",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Minimal Meshtastic text packet for bridge tests."""
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


def _make_adapter_context(
    adapter_id: str, runner: PipelineRunner
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ===================================================================
# 1. Meshtastic inbound -> fake outbound
# ===================================================================


class TestMeshtasticInboundToFakeOutbound:
    """Real MeshtasticAdapter inbound -> FakeMeshtasticAdapter outbound
    through PipelineRunner.

    Proves: real codec/classifier inbound path, MeshtasticRenderer
    selection, delivery to fake adapter, native ref persistence.
    """

    async def test_meshtastic_inbound_delivers_to_fake_outbound(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: MeshtasticAdapter.simulate_inbound -> pipeline ->
        FakeMeshtasticAdapter.deliver."""
        mesh_config = MeshtasticConfig(
            adapter_id="mesh-real-in", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="fake-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-mesh-to-fake",
            source=RouteSource(
                adapter="mesh-real-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="fake-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("fake-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"mesh-real-in": mesh_adapter, "fake-out": fake_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("mesh-real-in", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(text="bridge payload", packet_id=9999)
        await mesh_adapter.simulate_inbound(packet)

        # Fake adapter received the rendered payload via the pipeline.
        assert len(fake_adapter.delivered_payloads) == 1
        result = fake_adapter.delivered_payloads[0]
        assert isinstance(result, RenderingResult)
        assert result.event_id is not None
        assert result.payload["text"] == "bridge payload"
        # MeshtasticRenderer was selected, not TextRenderer.
        assert result.metadata["renderer"] == "meshtastic"
        assert "channel_index" in result.payload
        assert "meshnet_name" in result.payload

        await mesh_adapter.stop()
        await runner.stop()

    async def test_inbound_native_ref_persisted_through_bridge(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inbound native ref from real MeshtasticAdapter is persisted in
        storage through the pipeline."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-mesh-in2", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="bridge-fake-out2")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-native-ref",
            source=RouteSource(
                adapter="bridge-mesh-in2",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-fake-out2", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-fake-out2", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-mesh-in2": mesh_adapter,
                    "bridge-fake-out2": fake_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-mesh-in2", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(packet_id=77777, channel=2)
        await mesh_adapter.simulate_inbound(packet)

        # Inbound native ref persisted.
        resolved = await temp_storage.resolve_native_ref(
            adapter="bridge-mesh-in2",
            native_channel_id="2",
            native_message_id="77777",
        )
        assert resolved is not None

        await mesh_adapter.stop()
        await runner.stop()

    async def test_outbound_fake_native_ref_persisted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Outbound native ref from FakeMeshtasticAdapter (which generates
        deterministic IDs) is persisted after delivery."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-mesh-in3", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="bridge-fake-out3")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-out-nref",
            source=RouteSource(
                adapter="bridge-mesh-in3",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-fake-out3", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-fake-out3", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-mesh-in3": mesh_adapter,
                    "bridge-fake-out3": fake_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-mesh-in3", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(text="outbound nref test", packet_id=11111)
        await mesh_adapter.simulate_inbound(packet)

        # FakeMeshtasticClient generates sequential IDs starting at 1.
        resolved = await temp_storage.resolve_native_ref(
            adapter="bridge-fake-out3",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is not None

        await mesh_adapter.stop()
        await runner.stop()

    async def test_delivery_receipt_persisted_as_sent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Pipeline records a 'sent' delivery receipt for successful bridge
        delivery to the fake adapter."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-mesh-receipt", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="bridge-fake-receipt")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-receipt",
            source=RouteSource(
                adapter="bridge-mesh-receipt",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-fake-receipt", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-fake-receipt", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-mesh-receipt": mesh_adapter,
                    "bridge-fake-receipt": fake_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-mesh-receipt", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(text="receipt check")
        await mesh_adapter.simulate_inbound(packet)

        # Verify receipt stored in database.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("bridge-fake-receipt",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"

        await mesh_adapter.stop()
        await runner.stop()

    async def test_packet_metadata_maps_consistently(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inbound packet metadata (sender, channel, packet_id) maps
        consistently from raw packet through codec to stored event."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-meta-in", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        route = Route(
            id="bridge-meta",
            source=RouteSource(
                adapter="bridge-meta-in",
                event_kinds=("message.created",),
                channel="3",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"bridge-meta-in": mesh_adapter},
                event_bus=EventBus(),
                rendering_pipeline=RenderingPipeline(),
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-meta-in", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(
            text="metadata test", sender="!deadbeef", channel=3, packet_id=55555
        )
        await mesh_adapter.simulate_inbound(packet)

        # Verify native ref persisted with correct metadata.
        resolved = await temp_storage.resolve_native_ref(
            adapter="bridge-meta-in",
            native_channel_id="3",
            native_message_id="55555",
        )
        assert resolved is not None

        # Verify stored event has correct source metadata.
        stored = await temp_storage.get(resolved)
        assert stored is not None
        assert stored.source_transport_id == "!deadbeef"
        assert stored.source_channel_id == "3"
        assert stored.source_native_ref is not None
        assert stored.source_native_ref.native_message_id == "55555"
        assert stored.source_native_ref.adapter == "bridge-meta-in"
        assert stored.payload["body"] == "metadata test"

        await mesh_adapter.stop()
        await runner.stop()

    async def test_channel_message_routes_correctly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A message on channel 5 is routed and its channel metadata
        preserved through the bridge."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-ch-in", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="bridge-ch-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-channel-route",
            source=RouteSource(
                adapter="bridge-ch-in",
                event_kinds=("message.created",),
                channel="5",
            ),
            targets=[RouteTarget(adapter="bridge-ch-out", channel="5")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-ch-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"bridge-ch-in": mesh_adapter, "bridge-ch-out": fake_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-ch-in", runner)
        await mesh_adapter.start(ctx)

        packet = _make_text_packet(text="channel 5 msg", channel=5, packet_id=33333)
        await mesh_adapter.simulate_inbound(packet)

        assert len(fake_adapter.delivered_payloads) == 1
        result = fake_adapter.delivered_payloads[0]
        # MeshtasticRenderer sets channel_index from target_channel.
        assert result.payload["channel_index"] == 5

        # Inbound native ref on channel 5.
        resolved = await temp_storage.resolve_native_ref(
            adapter="bridge-ch-in",
            native_channel_id="5",
            native_message_id="33333",
        )
        assert resolved is not None

        await mesh_adapter.stop()
        await runner.stop()

    async def test_reply_relation_flows_through_bridge(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Reply relations from Meshtastic replyId flow through the bridge
        and are stored in the event."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-reply-in", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="bridge-reply-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="bridge-reply-route",
            source=RouteSource(
                adapter="bridge-reply-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-reply-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-reply-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-reply-in": mesh_adapter,
                    "bridge-reply-out": fake_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-reply-in", runner)
        await mesh_adapter.start(ctx)

        # Send original message first to establish native ref.
        original_packet = _make_text_packet(
            text="original", packet_id=100, channel=0
        )
        await mesh_adapter.simulate_inbound(original_packet)

        # Send reply referencing the original.
        reply_packet = _make_text_packet(
            text="reply", packet_id=200, channel=0
        )
        reply_packet["decoded"]["replyId"] = 100
        await mesh_adapter.simulate_inbound(reply_packet)

        # Both events were delivered to fake adapter.
        assert len(fake_adapter.delivered_payloads) == 2

        # Reply's inbound native ref persisted.
        reply_resolved = await temp_storage.resolve_native_ref(
            adapter="bridge-reply-in",
            native_channel_id="0",
            native_message_id="200",
        )
        assert reply_resolved is not None

        # Stored reply event has a reply relation.
        stored_reply = await temp_storage.get(reply_resolved)
        assert stored_reply is not None
        assert len(stored_reply.relations) == 1
        rel = stored_reply.relations[0]
        assert rel.relation_type == "reply"

        await mesh_adapter.stop()
        await runner.stop()


# ===================================================================
# 2. Fake inbound -> Meshtastic outbound / local enqueue
# ===================================================================


class TestFakeInboundToMeshtasticOutbound:
    """FakeMeshtasticAdapter inbound -> real MeshtasticAdapter outbound
    (local enqueue) through PipelineRunner.

    The real MeshtasticAdapter.deliver() enqueues to the internal queue
    and returns AdapterDeliveryResult(native_message_id=None).  Tests
    verify the pipeline records a "sent" receipt but does NOT store an
    outbound native ref (because the queue-based adapter has no native
    message ID yet).

    Local enqueue is NOT final delivery — the message may still be
    in-flight through the radio queue.  The receipt records local
    acceptance only.
    """

    async def test_fake_inbound_to_meshtastic_outbound_enqueues_locally(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fake adapter inbound -> pipeline -> MeshtasticAdapter.deliver()
        enqueues payload locally."""
        fake_in_config = MeshtasticConfig(adapter_id="bridge-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="bridge-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="bridge-fake-to-mesh",
            source=RouteSource(
                adapter="bridge-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-fake-in": fake_in_adapter,
                    "bridge-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="bridge-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.bridge-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="local enqueue test", packet_id=88888)
        await fake_in_adapter.simulate_inbound(packet)

        # Real adapter's queue received the payload.
        assert mesh_out_adapter.queue.pending_count == 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_local_enqueue_returns_no_native_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshtasticAdapter.deliver() returns AdapterDeliveryResult with
        native_message_id=None and delivery_note='locally enqueued'."""
        mesh_config = MeshtasticConfig(
            adapter_id="bridge-enqueue-id", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        result = RenderingResult(
            event_id="evt-enqueue",
            target_adapter="bridge-enqueue-id",
            target_channel="0",
            payload={"text": "enqueue test", "channel_index": 0, "meshnet_name": ""},
        )
        delivery = await mesh_adapter.deliver(result)

        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.native_channel_id == "0"
        assert delivery.delivery_note == "locally enqueued"

    async def test_sent_receipt_without_outbound_native_ref(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Pipeline records a 'sent' receipt for MeshtasticAdapter local
        enqueue, but NO outbound native ref is stored (because
        native_message_id is None)."""
        fake_in_config = MeshtasticConfig(adapter_id="bridge-fake-nref")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="bridge-mesh-nref", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="bridge-nref-route",
            source=RouteSource(
                adapter="bridge-fake-nref",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-mesh-nref", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-mesh-nref", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-fake-nref": fake_in_adapter,
                    "bridge-mesh-nref": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-fake-nref", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="bridge-mesh-nref",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.bridge-mesh-nref"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="nref check", packet_id=44444)
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt is 'sent' for local acceptance.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("bridge-mesh-nref",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"

        # NO outbound native ref stored (native_message_id is None).
        # The real adapter returns None for native_message_id, so no
        # native ref mapping is persisted.
        outbound_refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND direction = 'outbound'",
            ("bridge-mesh-nref",),
        )
        assert len(outbound_refs) == 0

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_queue_health_shows_pending_after_deliver(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After pipeline delivery to MeshtasticAdapter, queue_health
        reports the pending item."""
        fake_in_config = MeshtasticConfig(adapter_id="bridge-fake-qh")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="bridge-mesh-qh", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="bridge-qh-route",
            source=RouteSource(
                adapter="bridge-fake-qh",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-mesh-qh", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-mesh-qh", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-fake-qh": fake_in_adapter,
                    "bridge-mesh-qh": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-fake-qh", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="bridge-mesh-qh",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.bridge-mesh-qh"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="queue health", packet_id=66666)
        await fake_in_adapter.simulate_inbound(packet)

        # Queue health reports the pending item.
        health = mesh_out_adapter.queue_health
        assert health["pending_count"] == 1

        # Adapter diagnostics also report queue state.
        diag = mesh_out_adapter.diagnostics()
        assert diag["queue_pending"] == 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_meshtastic_renderer_selected_for_real_adapter(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshtasticRenderer is selected for the real MeshtasticAdapter
        target through the pipeline's platform registry, not TextRenderer."""
        fake_in_config = MeshtasticConfig(adapter_id="bridge-fake-rend")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="bridge-mesh-rend", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="bridge-rend-route",
            source=RouteSource(
                adapter="bridge-fake-rend",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-mesh-rend", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-fake-rend": fake_in_adapter,
                    "bridge-mesh-rend": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        # PipelineRunner.start() populates platform registry from adapters.
        await runner.start()

        ctx = _make_adapter_context("bridge-fake-rend", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="bridge-mesh-rend",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.bridge-mesh-rend"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="renderer selection")
        await fake_in_adapter.simulate_inbound(packet)

        # The payload was enqueued with MeshtasticRenderer output shape.
        assert mesh_out_adapter.queue.pending_count == 1

        # Verify the enqueued payload has Meshtastic shape.
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        # Access the internal queue to inspect the enqueued item.
        item = await mesh_out_adapter.queue.dequeue()
        assert item is not None
        assert "text" in item["payload"]
        assert "channel_index" in item["payload"]
        assert "meshnet_name" in item["payload"]

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_runtime_accounting_increments_on_bridge(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """RuntimeAccounting counters increment when the bridge delivers."""
        fake_in_config = MeshtasticConfig(adapter_id="bridge-fake-acc")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="bridge-mesh-acc", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="bridge-acc-route",
            source=RouteSource(
                adapter="bridge-fake-acc",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="bridge-mesh-acc", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("bridge-mesh-acc", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "bridge-fake-acc": fake_in_adapter,
                    "bridge-mesh-acc": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
                runtime_accounting=accounting,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("bridge-fake-acc", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="bridge-mesh-acc",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.bridge-mesh-acc"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="accounting test")
        await fake_in_adapter.simulate_inbound(packet)

        # RuntimeAccounting should reflect the delivery.
        snap = accounting.snapshot()
        assert snap["inbound_accepted"] >= 1
        assert snap["outbound_attempts"] >= 1
        assert snap["outbound_delivered"] >= 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()


# ===================================================================
# 3. Error mapping bridge
# ===================================================================


class TestMeshtasticBridgeErrorMapping:
    """Error propagation through the bridge when Meshtastic adapter's
    queue or session fails.

    Verifies that Meshtastic-specific errors are mapped to the framework
    AdapterSendError / AdapterPermanentError taxonomy through the pipeline.
    """

    async def test_transient_queue_error_produces_failed_receipt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When MeshtasticAdapter's queue raises a transient error, the
        pipeline records a 'failed' receipt."""
        fake_in_config = MeshtasticConfig(adapter_id="err-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="err-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        # Patch queue.enqueue to raise transient error.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("radio busy", transient=True)
        )

        route = Route(
            id="err-transient-route",
            source=RouteSource(
                adapter="err-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="err-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("err-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "err-fake-in": fake_in_adapter,
                    "err-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("err-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="err-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.err-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="transient error test")
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt should be 'failed'.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("err-mesh-out",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

        # No outbound native ref for failed delivery.
        outbound_refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND direction = 'outbound'",
            ("err-mesh-out",),
        )
        assert len(outbound_refs) == 0

        # Inbound native ref should still exist for the source.
        inbound_refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND direction = 'inbound'",
            ("err-fake-in",),
        )
        assert len(inbound_refs) >= 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_permanent_error_produces_failed_receipt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When MeshtasticAdapter's queue raises a permanent error, the
        pipeline records a 'failed' receipt."""
        fake_in_config = MeshtasticConfig(adapter_id="perm-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="perm-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        # Patch queue.enqueue to raise permanent error.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("encoding failure", transient=False)
        )

        route = Route(
            id="perm-err-route",
            source=RouteSource(
                adapter="perm-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="perm-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("perm-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "perm-fake-in": fake_in_adapter,
                    "perm-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("perm-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="perm-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.perm-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="permanent error test")
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt should be 'failed'.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("perm-mesh-out",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_cancelled_error_propagates_through_deliver(self) -> None:
        """CancelledError propagates through MeshtasticAdapter.deliver()
        without being swallowed."""
        mesh_config = MeshtasticConfig(
            adapter_id="cancel-mesh", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        # Patch queue.enqueue to raise CancelledError.
        mesh_adapter._queue.enqueue = AsyncMock(
            side_effect=asyncio.CancelledError()
        )

        result = RenderingResult(
            event_id="evt-cancel",
            target_adapter="cancel-mesh",
            target_channel="0",
            payload={"text": "cancel test", "channel_index": 0},
        )

        with pytest.raises(asyncio.CancelledError):
            await mesh_adapter.deliver(result)

    async def test_error_in_one_target_does_not_affect_other(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A Meshtastic delivery failure does not prevent delivery to a
        second target in the same route."""
        fake_in_config = MeshtasticConfig(adapter_id="iso-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="iso-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)
        # Inject failure into the Meshtastic adapter.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("radio busy", transient=True)
        )

        good_config = MeshtasticConfig(adapter_id="iso-good-out")
        good_adapter = FakeMeshtasticAdapter(good_config)

        route = Route(
            id="iso-route",
            source=RouteSource(
                adapter="iso-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[
                RouteTarget(adapter="iso-mesh-out", channel="0"),
                RouteTarget(adapter="iso-good-out", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("iso-mesh-out", "meshtastic")
        rp.register_adapter_platform("iso-good-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "iso-fake-in": fake_in_adapter,
                    "iso-mesh-out": mesh_out_adapter,
                    "iso-good-out": good_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("iso-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="iso-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.iso-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )
        await good_adapter.start(
            AdapterContext(
                adapter_id="iso-good-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.iso-good-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = _make_text_packet(text="isolation test")
        await fake_in_adapter.simulate_inbound(packet)

        # Good adapter received its payload despite the other target failing.
        assert len(good_adapter.delivered_payloads) == 1

        # Two receipts: one sent, one failed.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE event_id = ?",
            (fake_in_adapter.inbound_events[0].event_id,),
        )
        assert len(rows) == 2
        by_status = {r["target_adapter"]: r["status"] for r in rows}
        assert by_status["iso-mesh-out"] == "failed"
        assert by_status["iso-good-out"] == "sent"

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await good_adapter.stop()
        await runner.stop()


# ===================================================================
# 4. Session callback bridge
# ===================================================================


class TestMeshtasticSessionCallbackBridge:
    """Exercises the sync _on_packet -> asyncio task -> publish_inbound
    path with the real MeshtasticAdapter connected to PipelineRunner.

    Tests verify that packets arriving through the session callback
    (matching real meshtastic-python pubsub behavior) are correctly
    converted to canonical events and flow through the pipeline.
    """

    async def test_on_packet_creates_background_task_for_inbound(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """_on_packet creates a tracked asyncio task that publishes inbound
        through the pipeline."""
        mesh_config = MeshtasticConfig(
            adapter_id="cb-mesh-in", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="cb-fake-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="cb-route",
            source=RouteSource(
                adapter="cb-mesh-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="cb-fake-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("cb-fake-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"cb-mesh-in": mesh_adapter, "cb-fake-out": fake_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = _make_adapter_context("cb-mesh-in", runner)
        await mesh_adapter.start(ctx)

        # Use _on_packet (sync callback path) instead of simulate_inbound.
        packet = _make_text_packet(text="callback path test", packet_id=55555)
        mesh_adapter._on_packet(packet)

        # Wait for the background task to complete.
        await asyncio.sleep(0.1)

        # Background task should have completed and been discarded.
        assert len(mesh_adapter._background_tasks) == 0

        # Fake adapter received the rendered payload.
        assert len(fake_adapter.delivered_payloads) == 1
        assert fake_adapter.delivered_payloads[0].payload["text"] == "callback path test"

        await mesh_adapter.stop()
        await runner.stop()

    async def test_on_packet_ignores_non_text_packets(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """_on_packet silently drops non-text packets without creating
        background tasks."""
        mesh_config = MeshtasticConfig(
            adapter_id="cb-drop-mesh", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        route = Route(
            id="cb-drop-route",
            source=RouteSource(
                adapter="cb-drop-mesh",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"cb-drop-mesh": mesh_adapter},
                event_bus=EventBus(),
                rendering_pipeline=RenderingPipeline(),
            )
        )
        await runner.start()

        ctx = _make_adapter_context("cb-drop-mesh", runner)
        await mesh_adapter.start(ctx)

        # Non-text packet: should be silently dropped.
        telemetry_packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        mesh_adapter._on_packet(telemetry_packet)
        await asyncio.sleep(0.05)

        # No background tasks created for non-text packets.
        assert len(mesh_adapter._background_tasks) == 0

        await mesh_adapter.stop()
        await runner.stop()


# ===================================================================
# 5. send_one bridge (monkeypatched session)
# ===================================================================


class TestMeshtasticSendOneBridge:
    """Exercises send_one() with monkeypatched session client to prove
    the full outbound send path through the queue.

    These tests use MeshtasticAdapter(connection_type="tcp") with a
    monkeypatched session._create_client to inject a fake client that
    tracks sendText calls.  This exercises the queue.process_one ->
    session.send path with pacing.
    """

    async def test_send_one_with_monkeypatched_client_sends(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """send_one() dequeues and sends via the monkeypatched client."""
        config = MeshtasticConfig(
            adapter_id="sendone-mesh", connection_type="tcp", host="1.2.3.4"
        )
        adapter = MeshtasticAdapter(config)

        class FakeClient:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            def sendText(self, text: str, channelIndex: int = 0) -> Any:
                self.sent.append({"text": text, "channel_index": channelIndex})
                return type("Packet", (), {"id": 42})()

        fake_client = FakeClient()

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self: MeshtasticSession) -> FakeClient:
            return fake_client

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        # Patch pubsub to no-op.
        import types
        import sys

        fake_pubsub = types.ModuleType("pubsub")
        fake_pub = types.ModuleType("pubsub.pub")
        fake_pub.subscribe = lambda cb, topic: None
        fake_pub.unsubscribe = lambda cb, topic: None
        fake_pubsub.pub = fake_pub
        monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)
        monkeypatch.setitem(sys.modules, "pubsub.pub", fake_pub)

        ctx = make_adapter_context("sendone-mesh")
        await adapter.start(ctx)

        # Enqueue via deliver.
        result = RenderingResult(
            event_id="evt-sendone",
            target_adapter="sendone-mesh",
            target_channel="0",
            payload={"text": "send one test", "channel_index": 0, "meshnet_name": ""},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is None  # queue-based
        assert adapter.queue.pending_count == 1

        # send_one processes the queue item via the monkeypatched client.
        send_result = await adapter.send_one()
        assert send_result is not None
        assert send_result.native_message_id == "42"
        assert send_result.native_channel_id == "0"
        assert adapter.queue.pending_count == 0
        assert len(fake_client.sent) == 1
        assert fake_client.sent[0]["text"] == "send one test"
        assert fake_client.sent[0]["channel_index"] == 0

        await adapter.stop()

    async def test_send_one_returns_none_when_no_client(
        self, make_adapter_context
    ) -> None:
        """send_one() returns None in fake mode (no real client)."""
        config = MeshtasticConfig(
            adapter_id="sendone-noclient", connection_type="fake"
        )
        adapter = MeshtasticAdapter(config)

        result = RenderingResult(
            event_id="evt-no-client",
            target_adapter="sendone-noclient",
            target_channel="0",
            payload={"text": "no client", "channel_index": 0, "meshnet_name": ""},
        )
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

        send_result = await adapter.send_one()
        assert send_result is None
