"""Fanout source-exclusion tests.

Proves that fanout routes (e.g. Matrix → [Meshtastic, MeshCore]) deliver to
all non-source targets without duplication.  The self-loop guard prevents
delivery back to the source adapter even when the source appears in the
target list.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
)


class TestFanoutWithoutSourceDuplication:
    """Fanout routes (Matrix → [Meshtastic, MeshCore]) deliver to all
    targets but never back to the source adapter."""

    async def test_fanout_delivers_to_all_non_source_targets(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fanout: Matrix → [Meshtastic, MeshCore] delivers to both."""
        fake_matrix = FakeMatrixAdapter("fanout-matrix", channel="!fanout:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="fanout-mesh"))
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="fanout-meshcore")
        )

        route = Route(
            id="fanout-route",
            source=RouteSource(
                adapter="fanout-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fanout-mesh", channel="0"),
                RouteTarget(adapter="fanout-meshcore", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={
                "fanout-matrix": fake_matrix,
                "fanout-mesh": fake_mesh,
                "fanout-meshcore": fake_meshcore,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("fanout-matrix", runner)
        await fake_matrix.start(ctx_mx)

        await fake_mesh.start(make_adapter_context("fanout-mesh", runner))
        await fake_meshcore.start(make_adapter_context("fanout-meshcore", runner))

        event = fake_matrix.make_event(
            text="fanout test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await fake_meshcore.stop()
        await runner.stop()

        # Both targets received
        assert len(fake_mesh.delivered_payloads) == 1
        assert len(fake_meshcore.delivered_payloads) == 1

        # Source did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

        # Two delivery receipts
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 2
        targets = {r["target_adapter"] for r in receipts}
        assert targets == {"fanout-mesh", "fanout-meshcore"}

    async def test_fanout_self_loop_guard(self, temp_storage: SQLiteStorage) -> None:
        """Fanout route with source in targets: self-loop guard fires."""
        fake_matrix = FakeMatrixAdapter("fanout-sl-matrix", channel="!sl:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="fanout-sl-mesh"))

        # Route includes source adapter in targets
        route = Route(
            id="fanout-sl-route",
            source=RouteSource(
                adapter="fanout-sl-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(
                    adapter="fanout-sl-matrix", channel="!sl:fake"
                ),  # self-loop
                RouteTarget(adapter="fanout-sl-mesh", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={
                "fanout-sl-matrix": fake_matrix,
                "fanout-sl-mesh": fake_mesh,
            },
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = make_adapter_context("fanout-sl-matrix", runner)
        await fake_matrix.start(ctx_mx)
        await fake_mesh.start(make_adapter_context("fanout-sl-mesh", runner))

        event = fake_matrix.make_event(
            text="fanout self-loop",
            event_kind=EventKind.MESSAGE_CREATED,
        )

        # Capture the outcomes from handle_ingress
        outcomes = await runner.handle_ingress(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Meshtastic received
        assert len(fake_mesh.delivered_payloads) == 1

        # Matrix did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

        # At least one outcome should have LOOP_SUPPRESSED for the self-loop
        loop_suppressed = [
            o for o in outcomes if o.failure_kind == DeliveryFailureKind.LOOP_SUPPRESSED
        ]
        assert len(loop_suppressed) >= 1, (
            f"Expected LOOP_SUPPRESSED failure_kind, got: "
            f"{[(o.status, o.failure_kind) for o in outcomes]}"
        )

        # loop_prevented incremented
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        # Only one receipt (to meshtastic); matrix target was skipped
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "fanout-sl-mesh"
        assert receipts[0]["status"] == "sent"

    async def test_fanout_three_targets_no_duplicates(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fanout to three targets creates exactly three receipts."""
        fake_matrix = FakeMatrixAdapter("fan3-mx", channel="!f3:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="fan3-mesh"))
        fake_meshcore = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id="fan3-mc"))
        fake_matrix_2 = FakeMatrixAdapter("fan3-mx2", channel="!f3-out:fake")

        route = Route(
            id="fan3-route",
            source=RouteSource(
                adapter="fan3-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fan3-mesh", channel="0"),
                RouteTarget(adapter="fan3-mc", channel="0"),
                RouteTarget(adapter="fan3-mx2", channel="!f3-out:fake"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={
                "fan3-mx": fake_matrix,
                "fan3-mesh": fake_mesh,
                "fan3-mc": fake_meshcore,
                "fan3-mx2": fake_matrix_2,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(make_adapter_context("fan3-mx", runner))
        await fake_mesh.start(make_adapter_context("fan3-mesh", runner))
        await fake_meshcore.start(make_adapter_context("fan3-mc", runner))
        await fake_matrix_2.start(make_adapter_context("fan3-mx2", runner))

        event = fake_matrix.make_event(
            text="fanout three",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await fake_meshcore.stop()
        await fake_matrix_2.stop()
        await runner.stop()

        # Each non-source target received exactly one delivery
        assert len(fake_mesh.delivered_payloads) == 1
        assert len(fake_meshcore.delivered_payloads) == 1
        assert len(fake_matrix_2.delivered_payloads) == 1

        # Source did not receive
        assert len(fake_matrix.delivered_payloads) == 0

        # Three receipts
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 3
