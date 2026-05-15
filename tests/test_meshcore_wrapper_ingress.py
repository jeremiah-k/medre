"""Real MeshCore wrapper callback ingress tests.

Proves that ``FakeMeshCoreAdapter.simulate_inbound`` decodes MeshCore packets,
publishes them through the pipeline, and delivers to fake targets.  Exercises
the MeshCore codec/classifier path through the full pipeline.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage.sqlite import SQLiteStorage

from tests.helpers.bridge import (
    make_adapter_context,
    make_meshcore_packet,
    make_pipeline_config,
)


class TestMeshCoreWrapperCallbackPath:
    """FakeMeshCoreAdapter.simulate_inbound → codec → publish_inbound →
    pipeline → fake target.  Proves the MeshCore codec/classifier path
    works correctly through the pipeline."""

    async def test_simulate_inbound_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """FakeMeshCoreAdapter.simulate_inbound delivers to fake target."""
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-cb-src")
        )
        fake_target = FakeMatrixAdapter("mc-fake-dst", channel="!mc-dst:fake")

        route = Route(
            id="mc-cb-route",
            source=RouteSource(
                adapter="mc-cb-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mc-fake-dst", channel="!mc-dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-cb-src": fake_meshcore, "mc-fake-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_meshcore.start(make_adapter_context("mc-cb-src", runner))
        await fake_target.start(make_adapter_context("mc-fake-dst", runner))

        packet = make_meshcore_packet(
            text="meshcore callback", sender="mc_sender", channel=0, packet_id=77777
        )
        await fake_meshcore.simulate_inbound(packet)

        await fake_meshcore.stop()
        await fake_target.stop()
        await runner.stop()

        # Fake target received
        assert len(fake_target.delivered_payloads) == 1

        # Receipt persisted
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["status"] == "sent"

    async def test_meshcore_packet_metadata_maps_correctly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshCore packet metadata (sender, channel) maps to canonical
        event fields."""
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-meta-src")
        )

        route = Route(
            id="mc-meta-route",
            source=RouteSource(
                adapter="mc-meta-src",
                event_kinds=("message.created",),
                channel="2",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-meta-src": fake_meshcore},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_meshcore.start(make_adapter_context("mc-meta-src", runner))

        packet = make_meshcore_packet(
            text="mc metadata",
            sender="pubkey_xyz",
            channel=2,
            packet_id=99999,
        )
        await fake_meshcore.simulate_inbound(packet)

        await fake_meshcore.stop()
        await runner.stop()

        # Verify stored event has correct metadata
        assert len(fake_meshcore.inbound_events) == 1
        canonical = fake_meshcore.inbound_events[0]
        assert canonical.source_adapter == "mc-meta-src"
        assert canonical.source_channel_id == "2"
