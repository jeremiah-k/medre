"""Real MeshtasticAdapter wrapper callback ingress tests.

Proves that the real ``MeshtasticAdapter.simulate_inbound`` decodes packets,
publishes them through the pipeline, and delivers to fake targets.  Uses a
fake connection type — no live Meshtastic hardware required.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)


class TestMeshtasticWrapperCallbackPath:
    """Real MeshtasticAdapter.simulate_inbound → codec → publish_inbound →
    pipeline → fake target."""

    async def test_simulate_inbound_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshtasticAdapter.simulate_inbound decodes packet, publishes
        through pipeline, delivers to fake target."""
        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-cb-src", connection_type="fake")
        )
        fake_target = FakeMatrixAdapter("fake-mx-dst", channel="!dst:fake")

        route = Route(
            id="mesh-cb-route",
            source=RouteSource(
                adapter="mesh-cb-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="fake-mx-dst", channel="!dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mesh-cb-src": mesh_adapter, "fake-mx-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(make_adapter_context("mesh-cb-src", runner))
        await fake_target.start(make_adapter_context("fake-mx-dst", runner))

        packet = make_text_packet(text="mesh callback test", packet_id=44444, channel=0)
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await fake_target.stop()
        await runner.stop()

        # Fake target received
        assert len(fake_target.delivered_payloads) == 1
        rendered = fake_target.delivered_payloads[0]
        assert isinstance(rendered, RenderingResult)

        # Delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["status"] == "sent"

    async def test_packet_channel_metadata_maps_to_canonical(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Packet sender, channel, packet_id map correctly to canonical
        event fields."""
        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-meta-src", connection_type="fake")
        )

        route = Route(
            id="mesh-meta-route",
            source=RouteSource(
                adapter="mesh-meta-src",
                event_kinds=("message.created",),
                channel="3",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mesh-meta-src": mesh_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(make_adapter_context("mesh-meta-src", runner))

        packet = make_text_packet(
            text="metadata test",
            sender="!deadbeef",
            channel=3,
            packet_id=12345,
        )
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await runner.stop()

        # Native ref persisted with correct metadata
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-meta-src",
            native_channel_id="3",
            native_message_id="12345",
        )
        assert resolved is not None

        # Stored event has correct source metadata
        stored = await temp_storage.get(resolved)
        assert stored is not None
        assert stored.source_transport_id == "!deadbeef"
        assert stored.source_channel_id == "3"
        assert stored.source_native_ref is not None
        assert stored.source_native_ref.native_message_id == "12345"
        assert stored.source_adapter == "mesh-meta-src"

    async def test_meshtastic_inbound_reaches_fake_matrix(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: Meshtastic simulate_inbound → pipeline → fake
        Matrix adapter delivery."""
        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-bridge-src", connection_type="fake")
        )
        fake_mx = FakeMatrixAdapter("mx-bridge-dst", channel="!bridge-dst:fake")

        route = Route(
            id="mesh-to-mx-bridge",
            source=RouteSource(
                adapter="mesh-bridge-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mx-bridge-dst", channel="!bridge-dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mesh-bridge-src": mesh_adapter, "mx-bridge-dst": fake_mx},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(make_adapter_context("mesh-bridge-src", runner))
        await fake_mx.start(make_adapter_context("mx-bridge-dst", runner))

        packet = make_text_packet(text="mesh to matrix", packet_id=66666)
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await fake_mx.stop()
        await runner.stop()

        # Fake matrix adapter received delivery
        assert len(fake_mx.delivered_payloads) == 1
        rendered = fake_mx.delivered_payloads[0]
        assert isinstance(rendered, RenderingResult)

        # Receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "mx-bridge-dst"
        assert receipts[0]["status"] == "sent"

        # Canonical event has meshtastic source
        events = await temp_storage._read_all(
            "SELECT source_adapter FROM canonical_events"
        )
        assert len(events) == 1
        assert events[0]["source_adapter"] == "mesh-bridge-src"
