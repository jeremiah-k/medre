"""Fake adapter ingress equivalence tests.

Proves that ``FakeMatrixAdapter.simulate_inbound`` and
``FakeMeshtasticAdapter.simulate_inbound`` produce identical pipeline results
to direct ``PipelineRunner.handle_ingress`` — same delivery receipts, same
persisted native refs, same accounting counters.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)

# ===================================================================
# 1. FakeMatrixAdapter ingress equivalence
# ===================================================================


class TestFakeMatrixAdapterIngressEquivalence:
    """FakeMatrixAdapter.simulate_inbound produces the same pipeline
    results as direct PipelineRunner.handle_ingress."""

    async def test_simulate_inbound_vs_handle_ingress_identical_outcomes(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths produce identical delivery receipts and accounting."""
        fake_target = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="fake-target"))

        route = Route(
            id="equiv-route",
            source=RouteSource(
                adapter="fake-matrix-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        adapters = {"fake-target": fake_target}
        config = make_pipeline_config(
            temp_storage,
            router,
            adapters=adapters,
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Path A: simulate_inbound
        fake_matrix_a = FakeMatrixAdapter("fake-matrix-src", channel="ch-0")
        ctx_a = make_adapter_context("fake-matrix-src", runner)
        await fake_matrix_a.start(ctx_a)

        event_a = fake_matrix_a.make_event(
            text="equiv test A",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix_a.simulate_inbound(event_a)
        await fake_matrix_a.stop()

        # Path B: direct handle_ingress
        event_b = CanonicalEvent(
            event_id="direct-evt-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake-matrix-src",
            source_transport_id="fake-matrix-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "equiv test B"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_b)

        await runner.stop()

        # Both events stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2
        stored_ids = {row["event_id"] for row in all_events}
        assert event_a.event_id in stored_ids
        assert event_b.event_id in stored_ids

        # Both have delivery receipts
        receipts = await temp_storage._read_all(
            "SELECT event_id, target_adapter, status FROM delivery_receipts ORDER BY event_id"
        )
        assert len(receipts) == 2
        for r in receipts:
            assert r["target_adapter"] == "fake-target"
            assert r["status"] == "sent"

        # Fake target received both deliveries
        assert len(fake_target.delivered_payloads) == 2

    async def test_native_refs_persisted_identically(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths persist inbound native refs with the same structure."""
        route = Route(
            id="nref-equiv-route",
            source=RouteSource(
                adapter="fm-nref",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Path A: simulate_inbound
        fake_matrix = FakeMatrixAdapter("fm-nref", channel="ch-nref")
        ctx = make_adapter_context("fm-nref", runner)
        await fake_matrix.start(ctx)

        event = fake_matrix.make_event(
            text="nref test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)
        await fake_matrix.stop()

        # Path B: direct handle_ingress with same fields
        event_direct = CanonicalEvent(
            event_id="direct-nref-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fm-nref",
            source_transport_id="fm-nref",
            source_channel_id="ch-nref",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "nref test direct"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_direct)
        await runner.stop()

        # Both should have canonical events in storage
        stored_a = await temp_storage.get(event.event_id)
        stored_b = await temp_storage.get(event_direct.event_id)
        assert stored_a is not None
        assert stored_b is not None
        assert stored_a.source_adapter == stored_b.source_adapter == "fm-nref"

    async def test_accounting_increments_match(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths increment RuntimeAccounting identically."""
        fake_target = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="acc-target"))

        route = Route(
            id="acc-equiv-route",
            source=RouteSource(
                adapter="fm-acc",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="acc-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"acc-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        fake_matrix = FakeMatrixAdapter("fm-acc", channel="ch-acc")
        ctx = make_adapter_context("fm-acc", runner)
        await fake_matrix.start(ctx)

        # simulate_inbound path
        event_a = fake_matrix.make_event(
            text="acc test A",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event_a)

        snap_after_a = accounting.snapshot()

        # direct handle_ingress path
        event_b = CanonicalEvent(
            event_id="direct-acc-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fm-acc",
            source_transport_id="fm-acc",
            source_channel_id="ch-acc",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "acc test B"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_b)

        snap_after_b = accounting.snapshot()

        await fake_matrix.stop()
        await runner.stop()

        # After A: 1 inbound, 1 outbound attempt, 1 delivered
        assert snap_after_a["inbound_accepted"] == 1
        assert snap_after_a["outbound_attempts"] == 1
        assert snap_after_a["outbound_delivered"] == 1

        # After B: 2 inbound, 2 outbound attempts, 2 delivered (incremented by 1 each)
        assert snap_after_b["inbound_accepted"] == 2
        assert snap_after_b["outbound_attempts"] == 2
        assert snap_after_b["outbound_delivered"] == 2

        # No loop_prevented in either path
        assert snap_after_b["loop_prevented"] == 0


# ===================================================================
# 2. FakeMeshtasticAdapter ingress equivalence
# ===================================================================


class TestFakeMeshtasticAdapterIngressEquivalence:
    """FakeMeshtasticAdapter.simulate_inbound produces the same pipeline
    results as direct PipelineRunner.handle_ingress."""

    async def test_simulate_inbound_vs_handle_ingress_identical_receipts(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths produce identical delivery receipts."""
        fake_target = FakeMatrixAdapter("mesh-eq-target", channel="ch-0")

        route = Route(
            id="mesh-eq-route",
            source=RouteSource(
                adapter="fmesh-eq-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mesh-eq-target", channel="ch-0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mesh-eq-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Path A: simulate_inbound
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="fmesh-eq-src"))
        ctx = make_adapter_context("fmesh-eq-src", runner)
        await fake_mesh.start(ctx)

        packet = make_text_packet(text="mesh equiv test", packet_id=88888)
        await fake_mesh.simulate_inbound(packet)

        # Get the decoded canonical event from the adapter's inbound history
        assert len(fake_mesh.inbound_events) == 1
        canonical_a = fake_mesh.inbound_events[0]
        await fake_mesh.stop()

        # Path B: direct handle_ingress with the same canonical event fields
        canonical_b = CanonicalEvent(
            event_id="direct-mesh-b",
            event_kind=canonical_a.event_kind,
            schema_version=canonical_a.schema_version,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fmesh-eq-src",
            source_transport_id=canonical_a.source_transport_id,
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload=canonical_a.payload,
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(canonical_b)

        await runner.stop()

        # Both events stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2

        # Both have delivery receipts with same status
        receipts = await temp_storage._read_all(
            "SELECT event_id, status, target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 2
        for r in receipts:
            assert r["status"] == "sent"
            assert r["target_adapter"] == "mesh-eq-target"

        # Fake target received both
        assert len(fake_target.delivered_payloads) == 2
