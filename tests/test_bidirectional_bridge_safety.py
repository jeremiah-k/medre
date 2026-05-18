"""Bidirectional bridge safety tests.

Proves that bidirectional routes (Matrix ↔ Meshtastic) do not loop:
each message creates exactly one canonical event, and the self-loop guard
prevents source-adapter re-delivery.  Also verifies that ``loop_prevented``
counters remain zero for normal delivery and increment only when the source
adapter appears in the target list.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage

from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)


class TestBidirectionalBridgeSafety:
    """Bidirectional routes (Matrix↔Meshtastic) do not loop.
    Each message creates exactly one canonical event. Self-loop guard
    prevents delivery back to the source adapter."""

    async def test_matrix_to_meshtastic_does_not_echo_back(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Matrix→Meshtastic delivery is not re-routed back to Matrix."""
        fake_matrix = FakeMatrixAdapter("bidir-matrix", channel="!room:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="bidir-mesh")
        )

        # Route 1: matrix -> meshtastic
        route_mx_to_mesh = Route(
            id="bidir-mx-to-mesh",
            source=RouteSource(
                adapter="bidir-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir-mesh", channel="0")],
        )
        # Route 2: meshtastic -> matrix
        route_mesh_to_mx = Route(
            id="bidir-mesh-to-mx",
            source=RouteSource(
                adapter="bidir-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir-matrix", channel="!room:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"bidir-matrix": fake_matrix, "bidir-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("bidir-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("bidir-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Inject from Matrix side
        event = fake_matrix.make_event(
            text="bidir from matrix",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Meshtastic received the delivery
        assert len(fake_mesh.delivered_payloads) == 1
        # TextRenderer extracts from event.payload["text"]; FakeMatrixAdapter
        # uses "body" key, so the rendered text comes from .get("text", "")
        rendered_payload = fake_mesh.delivered_payloads[0].payload
        assert "text" in rendered_payload

        # Matrix did NOT receive any delivery (no echo-back)
        assert len(fake_matrix.delivered_payloads) == 0

        # Exactly one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "bidir-matrix"

        # Exactly one delivery receipt (to meshtastic)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "bidir-mesh"
        assert receipts[0]["status"] == "sent"

    async def test_meshtastic_to_matrix_does_not_echo_back(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Meshtastic→Matrix delivery is not re-routed back to Meshtastic."""
        fake_matrix = FakeMatrixAdapter("bidir2-matrix", channel="!room2:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="bidir2-mesh")
        )

        route_mx_to_mesh = Route(
            id="bidir2-mx-to-mesh",
            source=RouteSource(
                adapter="bidir2-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir2-mesh", channel="0")],
        )
        route_mesh_to_mx = Route(
            id="bidir2-mesh-to-mx",
            source=RouteSource(
                adapter="bidir2-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir2-matrix", channel="!room2:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"bidir2-matrix": fake_matrix, "bidir2-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("bidir2-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("bidir2-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Inject from Meshtastic side
        packet = make_text_packet(text="bidir from mesh", packet_id=77777)
        await fake_mesh.simulate_inbound(packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Matrix received the delivery
        assert len(fake_matrix.delivered_payloads) == 1

        # Meshtastic did NOT receive any delivery (no echo-back)
        assert len(fake_mesh.delivered_payloads) == 0

        # Exactly one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "bidir2-mesh"

        # Exactly one delivery receipt (to matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "bidir2-matrix"

    async def test_each_message_creates_exactly_one_canonical_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple messages in both directions each create exactly one event."""
        fake_matrix = FakeMatrixAdapter("multi-matrix", channel="!multi:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="multi-mesh")
        )

        route_a = Route(
            id="multi-mx-to-mesh",
            source=RouteSource(
                adapter="multi-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="multi-mesh", channel="0")],
        )
        route_b = Route(
            id="multi-mesh-to-mx",
            source=RouteSource(
                adapter="multi-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="multi-matrix", channel="!multi:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"multi-matrix": fake_matrix, "multi-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("multi-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("multi-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Send 3 messages from matrix
        for i in range(3):
            event = fake_matrix.make_event(
                text=f"mx msg {i}",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            await fake_matrix.simulate_inbound(event)

        # Send 3 messages from meshtastic
        for i in range(3):
            packet = make_text_packet(text=f"mesh msg {i}", packet_id=1000 + i)
            await fake_mesh.simulate_inbound(packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Exactly 6 canonical events (3 from each source)
        all_events = await temp_storage._read_all(
            "SELECT source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 6

        matrix_events = [e for e in all_events if e["source_adapter"] == "multi-matrix"]
        mesh_events = [e for e in all_events if e["source_adapter"] == "multi-mesh"]
        assert len(matrix_events) == 3
        assert len(mesh_events) == 3

        # 6 delivery receipts total (3 matrix→mesh + 3 mesh→matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 6

        mesh_receipts = [r for r in receipts if r["target_adapter"] == "multi-mesh"]
        mx_receipts = [r for r in receipts if r["target_adapter"] == "multi-matrix"]
        assert len(mesh_receipts) == 3
        assert len(mx_receipts) == 3

    async def test_loop_prevented_counter_zero_for_normal_delivery(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """loop_prevented counter is 0 for expected bidirectional deliveries."""
        fake_matrix = FakeMatrixAdapter("lp-matrix", channel="!lp:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="lp-mesh")
        )

        route_a = Route(
            id="lp-mx-to-mesh",
            source=RouteSource(
                adapter="lp-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lp-mesh", channel="0")],
        )
        route_b = Route(
            id="lp-mesh-to-mx",
            source=RouteSource(
                adapter="lp-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lp-matrix", channel="!lp:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lp-matrix": fake_matrix, "lp-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("lp-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("lp-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        event = fake_matrix.make_event(
            text="lp test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # No loop prevention triggered for normal bidirectional delivery
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 0

        stats = route_stats.snapshot()
        for route_id, counters in stats.items():
            assert counters["loop_prevented"] == 0

    async def test_self_loop_guard_increments_loop_prevented(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When source adapter is also a target, loop_prevented increments."""
        fake_matrix = FakeMatrixAdapter("loop-matrix", channel="!loop:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="loop-mesh")
        )

        # Route that includes the source adapter in its targets (self-loop)
        route = Route(
            id="self-loop-route",
            source=RouteSource(
                adapter="loop-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="loop-matrix", channel="!loop:fake"),  # self-loop
                RouteTarget(adapter="loop-mesh", channel="0"),  # normal target
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"loop-matrix": fake_matrix, "loop-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("loop-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("loop-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        event = fake_matrix.make_event(
            text="self-loop test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # loop_prevented incremented for the self-loop target
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        stats = route_stats.snapshot()
        assert stats["self-loop-route"]["loop_prevented"] == 1

        # Meshtastic still received its delivery
        assert len(fake_mesh.delivered_payloads) == 1

        # Matrix did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

    async def test_source_adapter_metadata_correct(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """source_adapter on canonical events correctly identifies origin."""
        fake_matrix = FakeMatrixAdapter("meta-matrix", channel="!meta:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="meta-mesh")
        )

        route_a = Route(
            id="meta-mx-to-mesh",
            source=RouteSource(
                adapter="meta-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="meta-mesh", channel="0")],
        )
        route_b = Route(
            id="meta-mesh-to-mx",
            source=RouteSource(
                adapter="meta-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="meta-matrix", channel="!meta:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"meta-matrix": fake_matrix, "meta-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("meta-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = make_adapter_context("meta-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Matrix-sourced event
        mx_event = fake_matrix.make_event(
            text="from matrix",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(mx_event)

        # Meshtastic-sourced event
        mesh_packet = make_text_packet(text="from mesh", packet_id=55555)
        await fake_mesh.simulate_inbound(mesh_packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2

        sources = {row["source_adapter"] for row in all_events}
        assert sources == {"meta-matrix", "meta-mesh"}
