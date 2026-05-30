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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterContext,
)
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.meshtastic_bridge import make_adapter_context, make_text_packet

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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "fake-out": MeshtasticConfig(
                        adapter_id="fake-out", radio_relay_prefix=""
                    )
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("mesh-real-in", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(text="bridge payload", packet_id=9999)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-fake-out2": MeshtasticConfig(adapter_id="bridge-fake-out2")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-mesh-in2", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(packet_id=77777, channel=2)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-fake-out3": MeshtasticConfig(adapter_id="bridge-fake-out3")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-mesh-in3", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(text="outbound nref test", packet_id=11111)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-fake-receipt": MeshtasticConfig(
                        adapter_id="bridge-fake-receipt"
                    )
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-mesh-receipt", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(text="receipt check")
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

        ctx = make_adapter_context("bridge-meta-in", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(
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
        rp.register(
            MeshtasticRenderer(
                configs={"bridge-ch-out": MeshtasticConfig(adapter_id="bridge-ch-out")}
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-ch-in", runner)
        await mesh_adapter.start(ctx)

        packet = make_text_packet(text="channel 5 msg", channel=5, packet_id=33333)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-reply-out": MeshtasticConfig(adapter_id="bridge-reply-out")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-reply-in", runner)
        await mesh_adapter.start(ctx)

        # Send original message first to establish native ref.
        original_packet = make_text_packet(text="original", packet_id=100, channel=0)
        await mesh_adapter.simulate_inbound(original_packet)

        # Send reply referencing the original.
        reply_packet = make_text_packet(text="reply", packet_id=200, channel=0)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-mesh-out": MeshtasticConfig(adapter_id="bridge-mesh-out")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-fake-in", runner)
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

        packet = make_text_packet(text="local enqueue test", packet_id=88888)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-mesh-nref": MeshtasticConfig(adapter_id="bridge-mesh-nref")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-fake-nref", runner)
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

        packet = make_text_packet(text="nref check", packet_id=44444)
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt is 'queued' for enqueue-only adapters.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("bridge-mesh-nref",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"

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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-mesh-qh": MeshtasticConfig(adapter_id="bridge-mesh-qh")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-fake-qh", runner)
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

        packet = make_text_packet(text="queue health", packet_id=66666)
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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-mesh-rend": MeshtasticConfig(adapter_id="bridge-mesh-rend")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-fake-rend", runner)
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

        packet = make_text_packet(text="renderer selection")
        await fake_in_adapter.simulate_inbound(packet)

        # The payload was enqueued with MeshtasticRenderer output shape.
        assert mesh_out_adapter.queue.pending_count == 1

        # Verify the enqueued payload has Meshtastic shape.

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
        rp.register(
            MeshtasticRenderer(
                configs={
                    "bridge-mesh-acc": MeshtasticConfig(adapter_id="bridge-mesh-acc")
                }
            ),
            priority=50,
        )
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

        ctx = make_adapter_context("bridge-fake-acc", runner)
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

        packet = make_text_packet(text="accounting test")
        await fake_in_adapter.simulate_inbound(packet)

        # RuntimeAccounting should reflect the delivery.
        snap = accounting.snapshot()
        assert snap["inbound_accepted"] >= 1
        assert snap["outbound_attempts"] >= 1
        assert snap["outbound_delivered"] >= 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()
