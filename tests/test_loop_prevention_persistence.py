"""Hardened loop prevention including source==target, route-trace guard,
native-ref cycle detection.

Proves that four independent loop-prevention mechanisms fire correctly:
1. source==target adapter detection blocks self-routing.
2. route-trace guard detects events that already traversed a route.
3. native-ref cycle detection catches outbound refs echoing back inbound.
4. All three guards can fire independently in a single pipeline run.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import RoutingMetadata
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.assertions import snap_value
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
)


class TestLoopPreventionHardening:
    """Hardened loop prevention: source==target, route-trace guard,
    native-ref cycle detection."""

    async def test_source_equals_target_adapter(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event whose source_adapter == target_adapter.
        Assert loop_prevented increments, no delivery."""
        fake_adapter = FakeMatrixAdapter("loop-src-tgt", channel="!loop:fake")

        # Route where source adapter is also the only target
        route = Route(
            id="loop-self-route",
            source=RouteSource(
                adapter="loop-src-tgt",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="loop-src-tgt", channel="!loop:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"loop-src-tgt": fake_adapter},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_adapter.start(make_adapter_context("loop-src-tgt", runner))

        event = fake_adapter.make_event(
            text="self-loop hardening",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        outcomes = await runner.handle_ingress(event)

        await fake_adapter.stop()
        await runner.stop()

        # loop_prevented incremented
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        # Route stats show loop_prevented
        stats = route_stats.snapshot()
        assert stats["loop-self-route"]["loop_prevented"] == 1
        assert stats["loop-self-route"]["delivered"] == 0

        # Outcome status is "skipped"
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert "loop_prevented" in (outcomes[0].error or "")

        # No delivery to the adapter
        assert len(fake_adapter.delivered_payloads) == 0

        # No delivery receipt persisted
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 0

    async def test_route_trace_guard_fires(self, temp_storage: SQLiteStorage) -> None:
        """Inject event with route-trace showing it already traversed a route.
        Assert route-trace guard fires."""
        fake_matrix = FakeMatrixAdapter("trace-mx", channel="!trace:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="trace-mesh"))

        route = Route(
            id="trace-route",
            source=RouteSource(
                adapter="trace-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="trace-mesh", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"trace-mx": fake_matrix, "trace-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Create event with a pre-existing route_trace showing it already
        # traversed this route (simulating re-routing from a multi-hop)
        from medre.core.events.metadata import RoutingMetadata

        event = CanonicalEvent(
            event_id=f"trace-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="trace-mx",
            source_transport_id="trace-mx",
            source_channel_id="!trace:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "trace guard test"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    matched_routes=("trace-route",),
                    route_trace=("trace-route", "other-route", "trace-route"),
                ),
            ),
        )
        outcomes = await runner.handle_ingress(event)

        await runner.stop()

        # Route-trace guard fired
        snap = accounting.snapshot()
        assert (
            snap["loop_prevented"] == 1
        ), "Route-trace guard should increment loop_prevented"

        stats = route_stats.snapshot()
        assert stats["trace-route"]["loop_prevented"] == 1

        # Outcome is skipped
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert "route already traversed" in (outcomes[0].error or "")

        # No delivery
        assert len(fake_mesh.delivered_payloads) == 0

    async def test_route_trace_single_occurrence_passes_through(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """route_trace with count=1 (first occurrence) does NOT trigger guard."""
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="pass-mesh"))

        route = Route(
            id="pass-route",
            source=RouteSource(
                adapter="pass-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="pass-mesh", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pass-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Event with no prior route_trace; pipeline will set
        # route_trace=("pass-route",) — single occurrence, count=1.
        event = CanonicalEvent(
            event_id=f"pass-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="pass-src",
            source_transport_id="pass-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "pass-through test"},
            metadata=EventMetadata(
                routing=RoutingMetadata(route_trace=()),
            ),
        )
        outcomes = await runner.handle_ingress(event)

        await runner.stop()

        # Event is delivered (not skipped)
        assert len(outcomes) == 1
        assert outcomes[0].status == "success"

        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 0
        assert snap["inbound_accepted"] == 1

        # Delivery happened
        assert len(fake_mesh.delivered_payloads) == 1

        # Route stats show delivered, not loop_prevented
        stats = route_stats.snapshot()
        assert stats["pass-route"]["delivered"] == 1
        assert stats["pass-route"]["loop_prevented"] == 0

    async def test_native_ref_cycle_detection(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event from adapter A whose native ref points back to
        adapter A's previously stored outbound ref -> cycle detected via
        dedup."""
        fake_matrix = FakeMatrixAdapter("cycle-mx", channel="!cycle:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="cycle-mesh"))

        route_a = Route(
            id="cycle-mx-mesh",
            source=RouteSource(
                adapter="cycle-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="cycle-mesh", channel="0")],
        )
        route_b = Route(
            id="cycle-mesh-mx",
            source=RouteSource(
                adapter="cycle-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="cycle-mx", channel="!cycle:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"cycle-mx": fake_matrix, "cycle-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(make_adapter_context("cycle-mx", runner))
        await fake_mesh.start(make_adapter_context("cycle-mesh", runner))

        # Step 1: Message from matrix -> delivered to mesh, creating outbound
        # native ref for cycle-mesh adapter
        event_1 = fake_matrix.make_event(
            text="cycle test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event_1)

        # After delivery, fake_mesh creates an outbound native ref:
        #   adapter="cycle-mesh", native_message_id="1", native_channel_id="0"
        # Step 2: Simulate that native ref coming back inbound from mesh side
        # with a native_ref pointing back to adapter A (cycle-mesh)
        cycle_event = CanonicalEvent(
            event_id=f"cycle-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="cycle-mesh",
            source_transport_id="!meshnode",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "cycle echo"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="cycle-mesh",
                native_channel_id="0",
                native_message_id="1",  # matches outbound from fake_mesh delivery
            ),
        )
        outcomes = await runner.handle_ingress(cycle_event)

        # Cycle detected -- event suppressed
        assert outcomes == []

        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1
        assert snap["inbound_accepted"] == 1, "Only the first message accepted"

        # Only one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "cycle-mx"

        # Only one delivery receipt (to mesh)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "cycle-mesh"

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

    async def test_all_three_guards_independent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fire all three guards independently, verify each increments
        loop_prevented correctly."""
        fake_a = FakeMatrixAdapter("guard-a", channel="!ga:fake")
        fake_b = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="guard-b"))

        route = Route(
            id="guard-route",
            source=RouteSource(
                adapter="guard-a",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="guard-a", channel="!ga:fake"),  # self-loop
                RouteTarget(adapter="guard-b", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"guard-a": fake_a, "guard-b": fake_b},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_a.start(make_adapter_context("guard-a", runner))
        await fake_b.start(make_adapter_context("guard-b", runner))

        # Guard 1: source==target (self-loop)
        event_1 = fake_a.make_event(
            text="guard 1 self-loop",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        outcomes_1 = await runner.handle_ingress(event_1)
        # One target is self-loop (skipped), one is normal (delivered)
        assert len(outcomes_1) == 2
        skipped = [o for o in outcomes_1 if o.status == "skipped"]
        succeeded = [o for o in outcomes_1 if o.status == "success"]
        assert len(skipped) == 1
        assert len(succeeded) == 1
        assert snap_value(accounting, "loop_prevented") == 1

        # Guard 2: route-trace guard (event already traversed this route)
        from medre.core.events.metadata import RoutingMetadata

        event_2 = CanonicalEvent(
            event_id=f"guard2-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 2 trace"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    route_trace=("guard-route", "guard-route"),
                ),
            ),
        )
        outcomes_2 = await runner.handle_ingress(event_2)
        # Both targets skipped by route-trace guard
        assert len(outcomes_2) == 2
        for o in outcomes_2:
            assert o.status == "skipped"
        assert snap_value(accounting, "loop_prevented") == 3  # +2 from route-trace

        # Guard 3: native-ref dedup (same native ref seen before)
        # First, create an event with a known native ref
        event_3 = CanonicalEvent(
            event_id=f"guard3a-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 3 first"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="guard-a",
                native_channel_id="!ga:fake",
                native_message_id="guard3-native-001",
            ),
        )
        await runner.handle_ingress(event_3)
        lp_after_first = snap_value(accounting, "loop_prevented")

        # Now send the duplicate
        event_3_dup = CanonicalEvent(
            event_id=f"guard3b-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 3 dup"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="guard-a",
                native_channel_id="!ga:fake",
                native_message_id="guard3-native-001",
            ),
        )
        outcomes_3 = await runner.handle_ingress(event_3_dup)
        assert outcomes_3 == []
        assert snap_value(accounting, "loop_prevented") == lp_after_first + 1

        await fake_a.stop()
        await fake_b.stop()
        await runner.stop()
