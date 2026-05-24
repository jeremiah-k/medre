"""Bidirectional fake_matrix <-> fake_meshtastic bridge handling N messages
without loops, with exact accounting and clean shutdown.

Proves that 10 messages (5 from each side) flow through the pipeline without
echo loops, with deterministic accounting counters and clean adapter shutdown.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)


class TestLongRunBidirectionalCallbackBridge:
    """Bidirectional fake_matrix <-> fake_meshtastic bridge handling N=10
    messages without loops, with exact accounting and clean shutdown."""

    async def test_ten_messages_bidirectional_no_loops(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject 5 messages from each side. Assert exact counts, no loops."""
        fake_matrix = FakeMatrixAdapter("lr-matrix", channel="!lr-room:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="lr-mesh"))

        route_mx_to_mesh = Route(
            id="lr-mx-to-mesh",
            source=RouteSource(
                adapter="lr-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lr-mesh", channel="0")],
        )
        route_mesh_to_mx = Route(
            id="lr-mesh-to-mx",
            source=RouteSource(
                adapter="lr-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lr-matrix", channel="!lr-room:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"lr-matrix": fake_matrix, "lr-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(make_adapter_context("lr-matrix", runner))
        await fake_mesh.start(make_adapter_context("lr-mesh", runner))

        # Inject 5 from Matrix side
        for i in range(5):
            event = fake_matrix.make_event(
                text=f"lr matrix msg {i}",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            await fake_matrix.simulate_inbound(event)

        # Inject 5 from Meshtastic side
        for i in range(5):
            packet = make_text_packet(text=f"lr mesh msg {i}", packet_id=2000 + i)
            await fake_mesh.simulate_inbound(packet)

        # Clean stop
        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Exactly 10 canonical events persisted
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 10, f"Expected 10 events, got {len(all_events)}"

        # 5 from each source
        matrix_events = [e for e in all_events if e["source_adapter"] == "lr-matrix"]
        mesh_events = [e for e in all_events if e["source_adapter"] == "lr-mesh"]
        assert len(matrix_events) == 5
        assert len(mesh_events) == 5

        # Exactly 10 delivery receipts (5 matrix->mesh + 5 mesh->matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
        )
        assert len(receipts) == 10, f"Expected 10 receipts, got {len(receipts)}"
        for r in receipts:
            assert r["status"] == "sent"

        mesh_receipts = [r for r in receipts if r["target_adapter"] == "lr-mesh"]
        mx_receipts = [r for r in receipts if r["target_adapter"] == "lr-matrix"]
        assert len(mesh_receipts) == 5
        assert len(mx_receipts) == 5

        # Accounting: inbound_accepted == 10, outbound_delivered == 10
        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 10
        assert snap["outbound_delivered"] == 10
        assert snap["outbound_attempts"] == 10

        # loop_prevented == 0 (bidirectional routes don't echo)
        assert snap["loop_prevented"] == 0

        # Route stats match expected
        stats = route_stats.snapshot()
        assert stats["lr-mx-to-mesh"]["delivered"] == 5
        assert stats["lr-mx-to-mesh"]["loop_prevented"] == 0
        assert stats["lr-mesh-to-mx"]["delivered"] == 5
        assert stats["lr-mesh-to-mx"]["loop_prevented"] == 0

        # Fake adapters received correct number of deliveries
        assert len(fake_mesh.delivered_payloads) == 5
        assert len(fake_matrix.delivered_payloads) == 5

    async def test_snapshot_reflects_totals(self, temp_storage: SQLiteStorage) -> None:
        """Final accounting snapshot reflects exact totals after bridge run."""
        fake_matrix = FakeMatrixAdapter("snap-mx", channel="!snap:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="snap-mesh"))

        route_a = Route(
            id="snap-mx-mesh",
            source=RouteSource(
                adapter="snap-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="snap-mesh", channel="0")],
        )
        route_b = Route(
            id="snap-mesh-mx",
            source=RouteSource(
                adapter="snap-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="snap-mx", channel="!snap:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"snap-mx": fake_matrix, "snap-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(make_adapter_context("snap-mx", runner))
        await fake_mesh.start(make_adapter_context("snap-mesh", runner))

        # 3 from matrix, 3 from mesh
        for i in range(3):
            evt = fake_matrix.make_event(
                text=f"snap mx {i}", event_kind=EventKind.MESSAGE_CREATED
            )
            await fake_matrix.simulate_inbound(evt)
        for i in range(3):
            pkt = make_text_packet(text=f"snap mesh {i}", packet_id=3000 + i)
            await fake_mesh.simulate_inbound(pkt)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 6
        assert snap["outbound_delivered"] == 6
        assert snap["outbound_attempts"] == 6
        assert snap["outbound_failed"] == 0
        assert snap["loop_prevented"] == 0
        assert snap["capacity_rejections"] == 0

        # All values are deterministic ints (no floats, no None)
        for key, value in snap.items():
            assert isinstance(value, int), f"{key}={value!r} is not int"
