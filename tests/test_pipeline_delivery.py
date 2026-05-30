"""Pipeline delivery tests: ingress, routing, rendering, self-loop guard,
relation resolution, receipt lineage, and route attribution.

Tests the full event lifecycle from ingress through storage, routing,
delivery planning, adapter delivery, and receipt recording.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.routing import Route, RouteDestination, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    """An unstarted FakeTransportAdapter for creating test events."""
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    """A FakePresentationAdapter that records delivered events."""
    return FakePresentationAdapter(adapter_id="fake_presentation")


# ===================================================================
# TestPipeline
# ===================================================================


class TestPipeline:
    """Test the full pipeline: EventBus + PipelineRunner + storage + router + adapters."""

    async def test_ingress_to_delivery(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Full round-trip: publish event -> stored -> routed -> planned -> delivered -> receipt."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="pipeline-001", payload={"text": "hello pipeline"})

        try:
            await runner.handle_ingress(event)

            # Event stored
            stored = await temp_storage.get("pipeline-001")
            assert stored is not None
            assert stored.event_id == "pipeline-001"
            assert stored.payload["text"] == "hello pipeline"

            # Adapter received a rendered payload (not raw CanonicalEvent)
            assert len(fake_presentation.delivered_payloads) == 1
            rendered = fake_presentation.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.event_id == "pipeline-001"
            assert rendered.payload["text"] == "hello pipeline"

            # Receipt stored in database
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("pipeline-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"
            assert rows[0]["target_adapter"] == "fake_presentation"
        finally:
            await runner.stop()

    async def test_middleware_drops_event(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Event bus middleware returning None drops the event."""

        class _DropAll:
            """Middleware that drops every event."""

            async def process(self, event: CanonicalEvent) -> None:
                return None

        bus = EventBus()
        bus.add_middleware(_DropAll(), priority=-100)

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
            event_bus=bus,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Subscribe the ingress handler to the bus so middleware runs first.
        bus.subscribe("*", runner.handle_ingress)

        event = make_event(event_id="drop-001")

        try:
            await bus.publish(event)

            # Event should NOT be stored
            stored = await temp_storage.get("drop-001")
            assert stored is None

            # Adapter should NOT have received anything
            assert len(fake_presentation.delivered_payloads) == 0
            assert len(fake_presentation.received_events) == 0
        finally:
            await runner.stop()

    async def test_multiple_targets(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event matching multiple routes delivers to all targets."""
        pres_a = FakePresentationAdapter(adapter_id="pres-a")
        pres_b = FakePresentationAdapter(adapter_id="pres-b")

        route_a = Route(
            id="route-a",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres-a")],
        )
        route_b = Route(
            id="route-b",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres-b")],
        )
        router = Router(routes=[route_a, route_b])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pres-a": pres_a, "pres-b": pres_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="multi-001",
            source_adapter="src",
            payload={"text": "fanout"},
        )

        try:
            await runner.handle_ingress(event)

            # Each adapter received a rendered payload
            assert len(pres_a.delivered_payloads) == 1
            assert pres_a.delivered_payloads[0].event_id == "multi-001"
            assert len(pres_b.delivered_payloads) == 1
            assert pres_b.delivered_payloads[0].event_id == "multi-001"

            # Both receipts stored
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("multi-001",),
            )
            assert len(rows) == 2
            adapter_names = {r["target_adapter"] for r in rows}
            assert adapter_names == {"pres-a", "pres-b"}
            assert all(r["status"] == "sent" for r in rows)
        finally:
            await runner.stop()

    async def test_route_event_builds_stable_delivery_plan_contract(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route planning records stable route, target, and capability metadata."""

        class _Adapter:
            _capabilities = AdapterCapabilities(reactions="unsupported")

        target_a = RouteTarget(
            adapter="dest",
            channel="room-1",
            destination=RouteDestination(
                kind="matrix_room",
                destination_hash="!room:id",
                destination_name="ops",
                metadata={"via": "example.test"},
            ),
        )
        target_b = RouteTarget(
            adapter="dest",
            channel="room-1",
            destination=RouteDestination(
                kind="matrix_room",
                destination_hash="!room:id",
                destination_name="ops",
                metadata={"via": "example.test"},
            ),
        )
        route_a = Route(
            id="stable-contract-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel="ch-0",
            ),
            targets=[target_a],
        )
        route_b = Route(
            id="stable-contract-route",
            source=route_a.source,
            targets=[target_b],
        )
        event = make_event(
            event_id="stable-contract-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"key": "+1"},
        )

        config_a = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route_a]),
            adapters={"dest": _Adapter()},
        )
        config_b = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route_b]),
            adapters={"dest": _Adapter()},
        )
        _routed_a, deliveries_a = await PipelineRunner(config_a).route_event(event)
        _routed_b, deliveries_b = await PipelineRunner(config_b).route_event(event)

        route, plan = deliveries_a[0]
        _route_b, plan_b = deliveries_b[0]

        assert route.id == "stable-contract-route"
        assert plan.plan_id == plan_b.plan_id
        assert plan.route_id == "stable-contract-route"
        assert plan.event_id == event.event_id
        assert plan.target.adapter == "dest"
        assert plan.target.channel == "room-1"
        assert plan.target_identity == plan_b.target_identity
        assert plan.target_identity != ""
        from medre.core.planning.delivery_plan import delivery_target_identity

        assert plan.target_identity == delivery_target_identity(plan.target)
        assert plan.primary_strategy.method == "skip"
        assert plan.capability_level == "unsupported"
        assert plan.capability_field == "reactions"
        assert plan.capability_reason is not None

    async def test_duplicate_targets_have_distinct_stable_plan_ids(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Repeated equivalent targets are disambiguated by route target index."""
        target = RouteTarget(adapter="dest", channel="room-1")
        route = Route(
            id="duplicate-target-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[target, RouteTarget(adapter="dest", channel="room-1")],
        )
        event = make_event(
            event_id="duplicate-target-001",
            source_adapter="src",
            source_channel_id="ch-0",
        )
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={"dest": FakePresentationAdapter(adapter_id="dest")},
        )

        _routed, deliveries = await PipelineRunner(config).route_event(event)

        plan_ids = [plan.plan_id for _route, plan in deliveries]
        assert len(plan_ids) == 2
        assert len(set(plan_ids)) == 2

    async def test_different_route_attribution_produces_different_plan_ids(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Same event, same target, but different route_id → different plan IDs."""
        event = make_event(
            event_id="diff-route-001",
            source_adapter="src",
            source_channel_id="ch-0",
        )
        target = RouteTarget(adapter="dest", channel="room-1")

        # Route A
        route_a = Route(
            id="route-a",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[target],
        )
        config_a = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route_a]),
            adapters={"dest": FakePresentationAdapter(adapter_id="dest")},
        )
        _, deliveries_a = await PipelineRunner(config_a).route_event(event)
        plan_a = deliveries_a[0][1]

        # Route B — different route_id, same target
        route_b = Route(
            id="route-b",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[target],
        )
        config_b = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route_b]),
            adapters={"dest": FakePresentationAdapter(adapter_id="dest")},
        )
        _, deliveries_b = await PipelineRunner(config_b).route_event(event)
        plan_b = deliveries_b[0][1]

        assert plan_a.event_id == plan_b.event_id
        assert plan_a.route_id == "route-a"
        assert plan_b.route_id == "route-b"
        assert plan_a.plan_id != plan_b.plan_id, (
            f"Different route_id must produce different plan IDs: "
            f"{plan_a.plan_id} == {plan_b.plan_id}"
        )

    async def test_different_target_identity_produces_different_plan_ids(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Same event, same route, but different target → different plan IDs."""
        event = make_event(
            event_id="diff-target-001",
            source_adapter="src",
            source_channel_id="ch-0",
        )
        route = Route(
            id="stability-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[
                RouteTarget(adapter="dest", channel="room-1"),
                RouteTarget(adapter="dest", channel="room-2"),
            ],
        )
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={"dest": FakePresentationAdapter(adapter_id="dest")},
        )
        _, deliveries = await PipelineRunner(config).route_event(event)

        plan_ids = [plan.plan_id for _route, plan in deliveries]
        assert len(plan_ids) == 2
        assert plan_ids[0] != plan_ids[1], (
            f"Different targets must produce different plan IDs: "
            f"{plan_ids[0]} == {plan_ids[1]}"
        )

    async def test_error_isolation(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """One failing target does not affect other targets."""

        class _FailingPresentation:
            """Adapter that always raises on deliver."""

            adapter_id = "failing"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("delivery failed")

        good = FakePresentationAdapter(adapter_id="good")
        failing = _FailingPresentation()

        route = Route(
            id="err-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="failing"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "failing": failing},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="err-001",
            source_adapter="src",
            source_channel_id=None,
        )

        try:
            await runner.handle_ingress(event)

            # Good adapter received rendered payload despite the failure
            assert len(good.delivered_payloads) == 1
            assert good.delivered_payloads[0].event_id == "err-001"

            # Failing adapter raised and did not append
            assert len(failing.received_events) == 0

            # Both receipts stored: one sent, one failed
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("err-001",),
            )
            assert len(rows) == 2
            by_status = {r["target_adapter"]: r["status"] for r in rows}
            assert by_status["good"] == "sent"
            assert by_status["failing"] == "failed"
        finally:
            await runner.stop()

    async def test_pipeline_with_reactions(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Reaction events flow through the pipeline correctly."""
        route = Route(
            id="reaction-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="react-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            await runner.handle_ingress(event)

            # Event stored
            stored = await temp_storage.get("react-001")
            assert stored is not None
            assert stored.event_kind == "message.reacted"

            # Adapter received the rendered reaction payload
            assert len(fake_presentation.delivered_payloads) == 1
            rendered = fake_presentation.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.event_id == "react-001"

            # Receipt stored
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("react-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"
        finally:
            await runner.stop()


# ===================================================================
# Render-before-deliver boundary tests
# ===================================================================


class TestRenderBeforeDeliverBoundary:
    """Prove that adapters cannot bypass planning/rendering in the
    supported path.  PipelineRunner always renders before delivery;
    adapters receive RenderingResult, not raw CanonicalEvent.
    """

    async def test_adapter_receives_rendering_result_not_raw_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline delivers RenderingResult, never a raw CanonicalEvent."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="boundary-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="boundary-001",
            source_adapter="src",
            payload={"text": "boundary test"},
        )

        try:
            await runner.handle_ingress(event)

            # Adapter received a RenderingResult, not a CanonicalEvent.
            assert len(adapter.delivered_payloads) == 1
            assert len(adapter.received_events) == 0
            rendered = adapter.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.event_id == "boundary-001"
        finally:
            await runner.stop()

    async def test_adapter_cannot_bypass_rendering(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When rendering pipeline is empty, adapter receives nothing.

        An empty rendering pipeline means no renderer can process the
        event, resulting in a permanent_failure.  The adapter must not
        receive the raw event as a fallback.
        """
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="no-render-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        # Empty rendering pipeline — no renderer available.
        empty_pipeline = RenderingPipeline()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="no-render-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            # Rendering failed — permanent failure outcome.
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"

            # Adapter received NOTHING — no raw event fallback.
            assert len(adapter.delivered_payloads) == 0
            assert len(adapter.received_events) == 0
        finally:
            await runner.stop()

    async def test_renderer_owns_target_formatting(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """The renderer decides the final payload text, not the adapter.

        PipelineRunner uses TextRenderer to convert the canonical event
        into a target-specific format.  The adapter merely stores the
        result without reformatting.
        """
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="format-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="format-001",
            source_adapter="src",
            payload={"text": "renderer owns this"},
        )

        try:
            await runner.handle_ingress(event)

            # The adapter stores the RenderingResult exactly as rendered.
            assert len(adapter.delivered_payloads) == 1
            result = adapter.delivered_payloads[0]
            assert result.payload["text"] == "renderer owns this"
            assert result.metadata.get("renderer") == "text"
        finally:
            await runner.stop()


# ===================================================================
# Receipt lineage in pipeline
# ===================================================================


class TestReceiptLineageInPipeline:
    """Verify that pipeline produces receipts with correct
    attempt_number and parent_receipt_id.
    """

    async def test_first_attempt_receipt_has_attempt_number_one(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Successful first delivery produces receipt with attempt_number=1."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="attempt-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="attempt-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            await temp_storage.list_receipts_for_plan(
                "attempt-route__target__0", "target"
            )
            # May not match due to plan_id format; query all receipts.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("attempt-001",),
            )
            assert len(rows) >= 1
            assert rows[0]["attempt_number"] == 1
            assert rows[0]["parent_receipt_id"] is None
        finally:
            await runner.stop()

    async def test_failed_delivery_receipt_has_lineage(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Failed delivery produces receipt with attempt_number=1 and no parent."""

        class _Broken:
            adapter_id = "broken"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("boom")

        broken = _Broken()

        route = Route(
            id="lineage-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="broken")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"broken": broken},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="lineage-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
                ("lineage-001",),
            )
            assert len(rows) >= 1
            # First receipt has attempt_number=1, no parent.
            assert rows[0]["attempt_number"] == 1
            assert rows[0]["parent_receipt_id"] is None
        finally:
            await runner.stop()


# ===================================================================
# Relation resolution in pipeline ingress
# ===================================================================


class TestRelationResolutionInPipeline:
    """Pipeline resolves relations during ingress."""

    async def test_reply_resolved_when_storage_has_target(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline resolves reply target_event_id when native ref is in storage."""
        # Pre-store the target event and its native ref
        from medre.core.events import NativeMessageRef

        target_event = make_event(event_id="target-evt-001", source_adapter="src")
        await temp_storage.append(target_event)
        target_nref = NativeMessageRef(
            id="nref-target-1",
            event_id="target-evt-001",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$orig-msg",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(target_nref)

        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="resolve-reply-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Create event with unresolved reply relation
        from medre.core.events import EventRelation

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="matrix",
                native_channel_id="!room:server",
                native_message_id="$orig-msg",
            ),
            key=None,
            fallback_text=None,
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="reply-evt-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(reply_rel,),
            payload={"text": "a reply"},
            metadata=EventMetadata(),
        )

        try:
            await runner.handle_ingress(event)

            # Check stored event has resolved relation
            stored = await temp_storage.get("reply-evt-001")
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id == "target-evt-001"
            assert stored.relations[0].target_native_ref is not None
        finally:
            await runner.stop()

    async def test_reply_unresolved_when_storage_lacks_target(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline preserves unresolved target_native_ref when target not in storage."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="unresolved-reply-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        from medre.core.events import EventRelation

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="matrix",
                native_channel_id="!room:server",
                native_message_id="$unknown-msg",
            ),
            key=None,
            fallback_text=None,
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="unresolved-reply-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(reply_rel,),
            payload={"text": "unresolved reply"},
            metadata=EventMetadata(),
        )

        try:
            await runner.handle_ingress(event)

            # Stored event preserves unresolved native ref
            stored = await temp_storage.get("unresolved-reply-001")
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id is None
            assert stored.relations[0].target_native_ref is not None
            assert (
                stored.relations[0].target_native_ref.native_message_id
                == "$unknown-msg"
            )
        finally:
            await runner.stop()


# ===================================================================
# Self-loop guard
# ===================================================================


class TestSelfLoopGuard:
    """Self-loop guard skips delivery back to source_adapter."""

    async def test_self_loop_skipped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event from adapter_a routed back to adapter_a is skipped."""
        pres_a = FakePresentationAdapter(adapter_id="adapter_a")

        # Route that would create a self-loop: adapter_a -> adapter_a
        route = Route(
            id="loop-route",
            source=RouteSource(
                adapter="adapter_a", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="adapter_a")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"adapter_a": pres_a},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="self-loop-001",
            source_adapter="adapter_a",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            # Outcome should be skipped, not delivered
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].error == "loop_prevented"
            assert outcomes[0].route_id == "loop-route"

            # Adapter should NOT have received anything
            assert len(pres_a.delivered_payloads) == 0
        finally:
            await runner.stop()

    async def test_self_loop_with_other_targets(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Self-loop target is skipped but other targets still deliver."""
        pres_a = FakePresentationAdapter(adapter_id="adapter_a")
        pres_b = FakePresentationAdapter(adapter_id="adapter_b")

        # Route with two targets: one self-loop, one valid
        route = Route(
            id="mixed-route",
            source=RouteSource(
                adapter="adapter_a", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[
                RouteTarget(adapter="adapter_a"),  # self-loop
                RouteTarget(adapter="adapter_b"),  # valid
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"adapter_a": pres_a, "adapter_b": pres_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="mixed-001",
            source_adapter="adapter_a",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 2
            # Self-loop skipped
            skipped = [o for o in outcomes if o.status == "skipped"]
            assert len(skipped) == 1
            assert skipped[0].target_adapter == "adapter_a"

            # Valid target delivered
            success = [o for o in outcomes if o.status == "success"]
            assert len(success) == 1
            assert success[0].target_adapter == "adapter_b"
        finally:
            await runner.stop()


# ===================================================================
# Route attribution
# ===================================================================


class TestRouteAttribution:
    """Route attribution metadata and route_id on receipts."""

    async def test_route_trace_populated(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """route_trace is populated on the event after routing."""
        pres = FakePresentationAdapter(adapter_id="pres")

        route = Route(
            id="attr-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="attr-001", source_adapter="src")

        try:
            # Use route_event directly to check the returned event
            routed_event, deliveries = await runner.route_event(event)
            assert len(deliveries) == 1
            assert routed_event.metadata.routing is not None
            assert routed_event.metadata.routing.route_trace == ("attr-route",)
        finally:
            await runner.stop()

    async def test_route_id_on_receipt(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """DeliveryReceipt carries the route_id of the matched route."""
        pres = FakePresentationAdapter(adapter_id="pres")

        route = Route(
            id="receipt-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="receipt-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"
            assert outcomes[0].receipt is not None
            assert outcomes[0].receipt.route_id == "receipt-route"
        finally:
            await runner.stop()

    async def test_route_stats_delivered(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RouteStats records successful deliveries."""
        pres = FakePresentationAdapter(adapter_id="pres")
        stats = RouteStats()

        route = Route(
            id="stats-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        config.route_stats = stats
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="stats-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)
            snap = stats.snapshot()
            assert "stats-route" in snap
            assert snap["stats-route"]["delivered"] == 1
            assert snap["stats-route"]["failed"] == 0
        finally:
            await runner.stop()

    async def test_route_stats_loop_prevented_counter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RouteStats records loop_prevented when self-loop guard fires."""
        pres = FakePresentationAdapter(adapter_id="a")
        stats = RouteStats()

        route = Route(
            id="loop-stats-route",
            source=RouteSource(
                adapter="a", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="a")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"a": pres},
        )
        config.route_stats = stats
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="loop-stats-001", source_adapter="a")

        try:
            await runner.handle_ingress(event)
            snap = stats.snapshot()
            assert "loop-stats-route" in snap
            assert snap["loop-stats-route"]["loop_prevented"] == 1
            assert snap["loop-stats-route"]["delivered"] == 0
        finally:
            await runner.stop()


# ===================================================================
# TestDeliveryGather
# ===================================================================


class TestDeliveryGather:
    """Verify _deliver_all gather semantics (line 2557).

    Tests that concurrent delivery via asyncio.gather isolates failures
    and handles edge cases correctly.
    """

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_others(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """If one target fails in _deliver_all, other deliveries still complete."""
        good = FakePresentationAdapter(adapter_id="good")
        bad = FakePresentationAdapter(adapter_id="bad")
        # Override deliver to raise on the bad adapter.

        async def _failing_deliver(result: object) -> None:
            raise RuntimeError("forced failure in gather test")

        bad.deliver = _failing_deliver  # type: ignore[assignment]

        route = Route(
            id="gather-fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="bad"),
            ],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "bad": bad},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="gather-fail-001",
            source_adapter="src",
            payload={"text": "gather test"},
        )

        # Persist event to match runtime contract (ingress stores before delivery).
        await temp_storage.append(event)

        try:
            # Build delivery plans manually for _deliver_all.
            plan_good = DeliveryPlan(
                plan_id="gather-fail-route__good__0",
                event_id=event.event_id,
                target=RouteTarget(adapter="good"),
                primary_strategy=DeliveryStrategy(method="direct"),
            )
            plan_bad = DeliveryPlan(
                plan_id="gather-fail-route__bad__1",
                event_id=event.event_id,
                target=RouteTarget(adapter="bad"),
                primary_strategy=DeliveryStrategy(method="direct"),
            )
            deliveries = [
                (route, plan_good),
                (route, plan_bad),
            ]

            results = await runner._deliver_all(event, deliveries)

            assert len(results) == 2
            # Good delivery succeeded — non-None receipt.
            assert results[0] is not None
            assert results[0].status == "sent"
            # Bad delivery failed — None because _safe_deliver caught the exception.
            assert results[1] is None

            # Good adapter actually received the payload.
            assert len(good.delivered_payloads) == 1
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_empty_delivery_list_returns_empty(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Empty deliveries -> empty result list."""
        adapter = FakePresentationAdapter(adapter_id="dest")

        route = Route(
            id="gather-empty-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="gather-empty-001",
            source_adapter="src",
        )

        try:
            results = await runner._deliver_all(event, [])
            assert results == []
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_all_succeed_returns_all_receipts(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """All deliveries succeed -> all non-None receipts."""
        pres_a = FakePresentationAdapter(adapter_id="pa")
        pres_b = FakePresentationAdapter(adapter_id="pb")

        route = Route(
            id="gather-ok-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="pa"),
                RouteTarget(adapter="pb"),
            ],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pa": pres_a, "pb": pres_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="gather-ok-001",
            source_adapter="src",
            payload={"text": "both ok"},
        )

        # Persist event to match runtime contract (ingress stores before delivery).
        await temp_storage.append(event)

        try:
            plan_a = DeliveryPlan(
                plan_id="gather-ok-route__pa__0",
                event_id=event.event_id,
                target=RouteTarget(adapter="pa"),
                primary_strategy=DeliveryStrategy(method="direct"),
            )
            plan_b = DeliveryPlan(
                plan_id="gather-ok-route__pb__1",
                event_id=event.event_id,
                target=RouteTarget(adapter="pb"),
                primary_strategy=DeliveryStrategy(method="direct"),
            )
            deliveries = [
                (route, plan_a),
                (route, plan_b),
            ]

            results = await runner._deliver_all(event, deliveries)

            assert len(results) == 2
            assert all(r is not None for r in results)
            assert all(r.status == "sent" for r in results)

            assert len(pres_a.delivered_payloads) == 1
            assert len(pres_b.delivered_payloads) == 1
        finally:
            await runner.stop()
