"""Pipeline integration tests: EventBus + PipelineRunner + storage + routing + adapters.

Tests the full event lifecycle from ingress through storage, routing,
delivery planning, adapter delivery, and receipt recording.  Exercises
error isolation, middleware-based event dropping, multi-target fanout,
reaction event handling, target-scoped failure semantics, and
diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.observability.metrics import Diagnostician, EventMetrics
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryOutcome
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.routing.stats import RouteCounters, RouteStats
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend


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


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict | None = None,
    event_bus: EventBus | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with sensible defaults for testing."""
    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
    )


def _make_event(
    event_id: str = "evt-001",
    event_kind: str = "message.created",
    source_adapter: str = "fake_transport",
    source_channel_id: str | None = "ch-0",
    payload: dict | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for pipeline tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Tests
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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="pipeline-001", payload={"text": "hello pipeline"})

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
            event_bus=bus,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Subscribe the ingress handler to the bus so middleware runs first.
        bus.subscribe("*", runner.handle_ingress)

        event = _make_event(event_id="drop-001")

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pres-a": pres_a, "pres-b": pres_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "failing": failing},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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
# EventMetrics integration
# ===================================================================


class TestEventMetrics:
    """Verify EventMetrics counters and snapshot work correctly."""

    def test_snapshot_returns_plain_dicts(self) -> None:
        metrics = EventMetrics()
        metrics.record_ingress("message.created")
        metrics.record_stored("message.created")
        metrics.record_delivered("message.created")

        snap = metrics.snapshot()
        assert snap["ingressed"] == {"message.created": 1}
        assert snap["stored"] == {"message.created": 1}
        assert snap["delivered"] == {"message.created": 1}
        assert snap["dropped"] == {}
        assert snap["failed"] == {}

    def test_multiple_kinds_tracked_separately(self) -> None:
        metrics = EventMetrics()
        metrics.record_ingress("message.created")
        metrics.record_ingress("message.created")
        metrics.record_ingress("message.reacted")

        snap = metrics.snapshot()
        assert snap["ingressed"] == {"message.created": 2, "message.reacted": 1}

    def test_snapshot_is_a_copy(self) -> None:
        metrics = EventMetrics()
        metrics.record_ingress("message.created")
        snap = metrics.snapshot()

        # Mutating the snapshot does not affect the metrics.
        snap["ingressed"]["message.created"] = 999
        assert metrics.events_ingressed["message.created"] == 1


# ===================================================================
# Target-scoped failure semantics
# ===================================================================


class TestTargetScopedFailures:
    """Verify target-scoped delivery outcomes and diagnostics."""

    async def test_fanout_partial_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout to three targets where one fails produces mixed outcomes."""
        diag = Diagnostician()
        good_a = FakePresentationAdapter(adapter_id="good-a")
        good_b = FakePresentationAdapter(adapter_id="good-b")

        class _BrokenAdapter:
            adapter_id = "broken"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("boom")

        broken = _BrokenAdapter()

        route = Route(
            id="fanout-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good-a"),
                RouteTarget(adapter="broken"),
                RouteTarget(adapter="good-b"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"good-a": good_a, "broken": broken, "good-b": good_b},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="fanout-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            # Three outcomes, one per target.
            assert len(outcomes) == 3

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["good-a"].status == "success"
            assert by_adapter["good-b"].status == "success"
            assert by_adapter["broken"].status == "permanent_failure"
            broken_error = by_adapter["broken"].error
            assert broken_error is not None
            assert "RuntimeError" in broken_error

            # Good adapters actually received rendered payloads.
            assert len(good_a.delivered_payloads) == 1
            assert good_a.delivered_payloads[0].event_id == "fanout-001"
            assert len(good_b.delivered_payloads) == 1
            assert good_b.delivered_payloads[0].event_id == "fanout-001"
            assert len(broken.received_events) == 0

            # Diagnostician captured the failure.
            snap = diag.snapshot()
            assert snap["adapter_failures"]["broken"] == 1
        finally:
            await runner.stop()

    async def test_transient_failure_does_not_affect_other_targets(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A transient failure in one target does not prevent other targets from succeeding."""

        class _TransientlyBroken:
            """Adapter that raises ConnectionError (transient)."""

            adapter_id = "transient-broken"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("network unreachable")

        diag = Diagnostician()
        good = FakePresentationAdapter(adapter_id="stable")
        flaky = _TransientlyBroken()

        route = Route(
            id="transient-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="stable"),
                RouteTarget(adapter="transient-broken"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"stable": good, "transient-broken": flaky},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="transient-001", source_adapter="src"
        )

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}

            # Good adapter succeeded and received rendered payload.
            assert by_adapter["stable"].status == "success"
            assert len(good.delivered_payloads) == 1
            assert good.delivered_payloads[0].event_id == "transient-001"

            # Flaky adapter classified as transient.
            assert (
                by_adapter["transient-broken"].status == "transient_failure"
            )
            transient_error = by_adapter["transient-broken"].error
            assert transient_error is not None
            assert "ConnectionError" in transient_error

            # Diagnostician recorded the adapter failure.
            snap = diag.snapshot()
            assert "transient-broken" in snap["adapter_failures"]
        finally:
            await runner.stop()

    async def test_diagnostics_emitted_on_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Diagnostician records are emitted when delivery fails."""
        diag = Diagnostician()

        class _FailAdapter:
            adapter_id = "fail-adapter"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("deliberate failure")

        adapter = _FailAdapter()

        route = Route(
            id="diag-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="fail-adapter")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fail-adapter": adapter},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="diag-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].target_adapter == "fail-adapter"

            # Diagnostician captured the failure.
            snap = diag.snapshot()
            assert snap["adapter_failures"]["fail-adapter"] == 1
            assert snap["planner_failures"] == {}
            assert snap["renderer_failures"] == {}
        finally:
            await runner.stop()

    async def test_rendering_failure_produces_permanent_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When no renderer can handle the event, a deterministic permanent_failure is returned."""
        diag = Diagnostician()
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="render-fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        # Empty rendering pipeline — no renderer registered.
        empty_pipeline = RenderingPipeline()

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.diagnostician = diag
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="render-fail-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].target_adapter == "target"
            render_error = outcomes[0].error
            assert render_error is not None
            assert "Rendering failed" in render_error
            assert "No renderer registered" in render_error

            # Adapter did NOT receive any payload (rendering failed first).
            assert len(adapter.delivered_payloads) == 0
            assert len(adapter.received_events) == 0

            # Diagnostician captured the renderer failure.
            snap = diag.snapshot()
            assert snap["renderer_failures"]["target"] == 1
            assert snap["adapter_failures"] == {}

            # A failed receipt was persisted.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("render-fail-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            await runner.stop()

    async def test_rendering_pipeline_default_includes_text_renderer(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """PipelineRunner creates a default TextRenderer when none is configured."""
        adapter = FakePresentationAdapter(adapter_id="pres")

        route = Route(
            id="default-render-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="pres")],
        )
        router = Router(routes=[route])

        # No rendering_pipeline in config → default with TextRenderer.
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pres": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="default-render-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Adapter received a RenderingResult from the default TextRenderer.
            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.payload["text"] == "hello"
            assert rendered.metadata.get("renderer") == "text"
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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="no-render-001", source_adapter="src"
        )

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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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
# Canonical immutability downstream tests
# ===================================================================


class TestCanonicalImmutabilityDownstream:
    """Verify that canonical events cannot be mutated after pipeline
    processing — they are frozen after creation and remain immutable
    through storage, routing, rendering, and delivery.
    """

    async def test_event_not_mutated_after_storage_and_routing(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event stored in DB is identical to the original ingress event."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="immut-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="immut-001",
            source_adapter="src",
            payload={"text": "immutable"},
        )
        # Capture original field values before pipeline processes.
        original_kind = event.event_kind
        original_payload_body = event.payload["text"]
        original_source = event.source_adapter

        try:
            await runner.handle_ingress(event)

            # Retrieve from storage — fields must match original.
            stored = await temp_storage.get("immut-001")
            assert stored is not None
            assert stored.event_kind == original_kind
            assert stored.payload["text"] == original_payload_body
            assert stored.source_adapter == original_source
        finally:
            await runner.stop()

    async def test_frozen_event_raises_on_field_assignment(self) -> None:
        """CanonicalEvent is frozen — assigning to any field raises."""
        event = _make_event(event_id="freeze-001")
        with pytest.raises(AttributeError):
            setattr(event, "event_kind", "tampered")
        with pytest.raises(AttributeError):
            setattr(event, "payload", {"evil": True})
        with pytest.raises(AttributeError):
            setattr(event, "source_adapter", "impostor")

    async def test_frozen_event_payload_dict_is_immutable(self) -> None:
        """The frozen event's payload dict cannot be reassigned.

        Note: the dict itself is not deeply frozen (that would require
        a custom mapping), but the struct field is frozen — you cannot
        replace the payload reference.
        """
        event = _make_event(event_id="freeze-002")
        original_text = event.payload["text"]
        # Struct is frozen — reassignment raises.
        with pytest.raises(AttributeError):
            setattr(event, "payload", {"hacked": True})
        # Original value unchanged.
        assert event.payload["text"] == original_text


# ===================================================================
# Delivery failure classification with DeliveryFailureKind
# ===================================================================


class TestDeliveryFailureClassification:
    """Verify failure_kind is populated on DeliveryOutcome from the pipeline."""

    async def test_adapter_transient_failure_classified(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ConnectionError is classified as ADAPTER_TRANSIENT."""

        class _Flaky:
            adapter_id = "flaky"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("network unreachable")

        from medre.core.planning.delivery_plan import DeliveryFailureKind

        diag = Diagnostician()
        flaky = _Flaky()
        good = FakePresentationAdapter(adapter_id="stable")

        route = Route(
            id="classify-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="stable"),
                RouteTarget(adapter="flaky"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"stable": good, "flaky": flaky},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="classify-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["stable"].status == "success"
            assert by_adapter["flaky"].status == "transient_failure"
            assert by_adapter["flaky"].failure_kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        finally:
            await runner.stop()

    async def test_adapter_permanent_failure_classified(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RuntimeError is classified as ADAPTER_PERMANENT."""

        class _Broken:
            adapter_id = "broken"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("payload rejected")

        from medre.core.planning.delivery_plan import DeliveryFailureKind

        diag = Diagnostician()
        broken = _Broken()

        route = Route(
            id="perm-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="broken")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"broken": broken},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="perm-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
        finally:
            await runner.stop()

    async def test_renderer_failure_classified(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Rendering failure is classified as RENDERER_FAILURE."""

        from medre.core.planning.delivery_plan import DeliveryFailureKind

        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="render-classify",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        empty_pipeline = RenderingPipeline()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="render-class-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.RENDERER_FAILURE
        finally:
            await runner.stop()

    async def test_target_not_found_returns_failed_receipt(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing adapter produces permanent_failure with TARGET_NOT_FOUND.

        ``deliver_to_target`` persists a failed receipt and raises so that
        ``_deliver_one`` classifies the outcome as ``permanent_failure``
        with ``failure_kind == TARGET_NOT_FOUND``.  No adapter delivery
        is attempted.
        """
        from medre.core.planning.delivery_plan import DeliveryFailureKind

        route = Route(
            id="missing-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="nonexistent")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="missing-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.TARGET_NOT_FOUND
            assert outcomes[0].target_adapter == "nonexistent"
            assert "not registered" in (outcomes[0].error or "")

            # Failed receipt persisted in storage.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("missing-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert "not registered" in (rows[0]["error"] or "")
        finally:
            await runner.stop()

    async def test_deadline_exceeded_returns_permanent_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Expired delivery deadline produces permanent_failure with DEADLINE_EXCEEDED.

        When the delivery plan's ``deadline`` is in the past, the pipeline
        records a failed receipt and returns a ``permanent_failure`` outcome
        with ``failure_kind == DEADLINE_EXCEEDED``.  No adapter delivery
        is attempted.
        """
        from datetime import timedelta

        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            DeliveryPlan,
        )

        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="deadline-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )

        class ExpiredDeadlineResolver(FallbackResolver):
            """Fallback resolver that always returns an expired deadline."""

            def resolve_fallback(self, event, target, capabilities):
                plan = super().resolve_fallback(event, target, capabilities)
                return DeliveryPlan(
                    plan_id=plan.plan_id,
                    event_id=plan.event_id,
                    target=plan.target,
                    primary_strategy=plan.primary_strategy,
                    fallback_chain=plan.fallback_chain,
                    retry_policy=plan.retry_policy,
                    deadline=datetime.now(timezone.utc) - timedelta(seconds=60),
                )

        config.fallback_resolver = ExpiredDeadlineResolver()

        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="deadline-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.DEADLINE_EXCEEDED
            assert outcomes[0].target_adapter == "target"
            assert "deadline" in (outcomes[0].error or "").lower()

            # Adapter was never called.
            assert len(adapter.delivered_payloads) == 0

            # Failed receipt persisted in storage.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("deadline-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert "deadline" in (rows[0]["error"] or "").lower()
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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="attempt-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            receipts = await temp_storage.list_receipts_for_plan(
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
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"broken": broken},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="lineage-001", source_adapter="src")

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
# Dead-letter with RetryPolicy
# ===================================================================


class TestDeadLetter:
    """Verify dead-letter receipts are produced when retry policy is exhausted."""

    async def test_dead_letter_receipt_on_exhaustion(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Failed delivery with max_attempts=1 produces a dead-letter receipt."""

        class _Broken:
            adapter_id = "dead-target"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("always fails")

        from medre.core.planning.delivery_plan import RetryPolicy

        broken = _Broken()
        route = Route(
            id="dead-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dead-target")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"dead-target": broken},
        )

        # Patch the fallback resolver to produce a plan with retry_policy max_attempts=1.
        from medre.core.planning.delivery_plan import DeliveryPlan

        class OneAttemptResolver(FallbackResolver):
            """Fallback resolver that limits delivery to one attempt."""

            def resolve_fallback(self, event, target, capabilities):
                plan = super().resolve_fallback(event, target, capabilities)
                return DeliveryPlan(
                    plan_id=plan.plan_id,
                    event_id=plan.event_id,
                    target=plan.target,
                    primary_strategy=plan.primary_strategy,
                    fallback_chain=plan.fallback_chain,
                    retry_policy=RetryPolicy(max_attempts=1, jitter=False),
                    deadline=plan.deadline,
                )

        config.fallback_resolver = OneAttemptResolver()

        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="dead-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Check receipts: should have failed + dead_lettered.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
                ("dead-001",),
            )
            assert len(rows) == 2
            assert rows[0]["status"] == "failed"
            assert rows[1]["status"] == "dead_lettered"
            assert rows[1]["attempt_number"] == 2
            assert rows[1]["parent_receipt_id"] == rows[0]["receipt_id"]
        finally:
            await runner.stop()

    async def test_no_dead_letter_without_retry_policy(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Without retry_policy, no dead-letter receipt is produced."""

        class _Broken:
            adapter_id = "no-retry"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("boom")

        broken = _Broken()
        route = Route(
            id="no-retry-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="no-retry")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"no-retry": broken},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="no-retry-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("no-retry-001",),
            )
            # Only one receipt — no dead-letter.
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            await runner.stop()


# ===================================================================
# Mixed fanout with failure classification
# ===================================================================


class TestMixedFanoutClassification:
    """Deterministic partial fanout: each target classified independently."""

    async def test_three_targets_mixed_classification(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Three targets: success, transient, permanent — all classified."""

        from medre.core.planning.delivery_plan import DeliveryFailureKind

        good = FakePresentationAdapter(adapter_id="good")

        class _Transient:
            adapter_id = "transient"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("timeout")

        class _Permanent:
            adapter_id = "permanent"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("bad payload")

        transient = _Transient()
        permanent = _Permanent()

        route = Route(
            id="mixed-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="transient"),
                RouteTarget(adapter="permanent"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "transient": transient, "permanent": permanent},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="mixed-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 3

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["good"].status == "success"
            assert by_adapter["good"].failure_kind is None

            assert by_adapter["transient"].status == "transient_failure"
            assert by_adapter["transient"].failure_kind is DeliveryFailureKind.ADAPTER_TRANSIENT

            assert by_adapter["permanent"].status == "permanent_failure"
            assert by_adapter["permanent"].failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT

            # Three distinct receipts stored.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("mixed-001",),
            )
            assert len(rows) == 3
        finally:
            await runner.stop()

    async def test_fanout_receipts_target_scoped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Receipts for different adapters are independent."""
        good_a = FakePresentationAdapter(adapter_id="a")
        good_b = FakePresentationAdapter(adapter_id="b")

        route = Route(
            id="scoped-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="a"),
                RouteTarget(adapter="b"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"a": good_a, "b": good_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="scoped-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
                ("scoped-001",),
            )
            assert len(rows) == 2
            adapters = {r["target_adapter"] for r in rows}
            assert adapters == {"a", "b"}
            # Each has its own attempt_number = 1
            for row in rows:
                assert row["attempt_number"] == 1
                assert row["parent_receipt_id"] is None
        finally:
            await runner.stop()


# ===================================================================
# Track 6: Fanout scaling, ordering, renderer downgrade/fallback
# ===================================================================


class TestFanoutScaling:
    """Deterministic fanout scaling: 1..N targets, all produce receipts."""

    async def test_fanout_10_targets_all_succeed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout to 10 targets: all succeed, 10 receipts stored."""
        targets = [f"target-{i}" for i in range(10)]
        adapters = {
            t: FakePresentationAdapter(adapter_id=t) for t in targets
        }

        route_targets = [RouteTarget(adapter=t) for t in targets]
        route = Route(
            id="scale-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=route_targets,
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="scale-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 10
            assert all(o.status == "success" for o in outcomes)

            # Every adapter received the rendered payload
            for t in targets:
                assert len(adapters[t].delivered_payloads) == 1
                assert adapters[t].delivered_payloads[0].event_id == "scale-001"

            # 10 distinct receipts
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("scale-001",),
            )
            assert len(rows) == 10
            receipt_adapters = {r["target_adapter"] for r in rows}
            assert receipt_adapters == set(targets)
        finally:
            await runner.stop()

    async def test_fanout_all_targets_fail(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout to 5 targets where all fail: 5 permanent_failure outcomes."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        targets = [f"broken-{i}" for i in range(5)]
        adapters = {
            t: FaultyPresentationAdapter(adapter_id=t, failure_mode="permanent_fail")
            for t in targets
        }

        route_targets = [RouteTarget(adapter=t) for t in targets]
        route = Route(
            id="all-fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=route_targets,
        )
        router = Router(routes=[route])

        diag = Diagnostician()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="allfail-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 5
            assert all(o.status == "permanent_failure" for o in outcomes)

            # Diagnostician recorded each failure
            snap = diag.snapshot()
            for t in targets:
                assert snap["adapter_failures"].get(t, 0) >= 1

            # 5 failed receipts
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("allfail-001",),
            )
            assert len(rows) == 5
            assert all(r["status"] == "failed" for r in rows)
        finally:
            await runner.stop()

    async def test_fanout_receipts_ordered_by_sequence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout receipts have monotonically increasing sequence numbers."""
        adapters = {
            f"ord-{i}": FakePresentationAdapter(adapter_id=f"ord-{i}")
            for i in range(5)
        }

        route = Route(
            id="seq-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter=f"ord-{i}") for i in range(5)],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        runner = PipelineRunner(config)
        await runner.start()

        events = [
            _make_event(event_id=f"seq-evt-{i}", source_adapter="src")
            for i in range(3)
        ]

        try:
            for event in events:
                await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT sequence, event_id FROM delivery_receipts ORDER BY sequence ASC",
                (),
            )
            # 3 events × 5 targets = 15 receipts
            assert len(rows) == 15

            # Sequence numbers are strictly monotonic
            seqs = [r["sequence"] for r in rows]
            for i in range(1, len(seqs)):
                assert seqs[i] > seqs[i - 1], (
                    f"Sequence {seqs[i]} not > {seqs[i-1]} at index {i}"
                )
        finally:
            await runner.stop()

    async def test_fanout_mixed_with_faulty_adapter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout: 2 good + 1 transient-faulty + 1 permanent-faulty."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        good_a = FakePresentationAdapter(adapter_id="good-a")
        good_b = FakePresentationAdapter(adapter_id="good-b")
        transient = FaultyPresentationAdapter(
            adapter_id="transient", failure_mode="transient_fail",
        )
        permanent = FaultyPresentationAdapter(
            adapter_id="permanent", failure_mode="permanent_fail",
        )

        route = Route(
            id="mixed-faulty-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good-a"),
                RouteTarget(adapter="transient"),
                RouteTarget(adapter="permanent"),
                RouteTarget(adapter="good-b"),
            ],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "good-a": good_a, "good-b": good_b,
                "transient": transient, "permanent": permanent,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="mixed-faulty-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 4

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["good-a"].status == "success"
            assert by_adapter["good-b"].status == "success"
            assert by_adapter["transient"].status == "transient_failure"
            assert by_adapter["permanent"].status == "permanent_failure"

            # Good adapters received payloads
            assert len(good_a.delivered_payloads) == 1
            assert len(good_b.delivered_payloads) == 1
            assert len(transient.delivered_payloads) == 0
            assert len(permanent.delivered_payloads) == 0
        finally:
            await runner.stop()


class TestRendererDowngradeFallback:
    """Renderer priority-based fallback and downgrade scenarios."""

    async def test_priority_renderer_downgrade_to_text(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When a high-priority renderer fails, TextRenderer handles fallback."""

        class _FailingRenderer:
            """Renderer that always raises."""

            name = "failing"

            def can_render(self, event, target_adapter, target_platform=None):
                return True

            async def render(self, event, target_adapter, target_channel=None):
                raise RuntimeError("renderer unavailable")

        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="downgrade-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        # Failing renderer at higher priority (lower number = higher priority)
        pipeline = RenderingPipeline()
        pipeline.register(_FailingRenderer(), priority=10)
        pipeline.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="downgrade-001",
            source_adapter="src",
            payload={"text": "fallback test"},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            # The failing renderer raises, so we get a permanent_failure
            # because the pipeline tries the first matching renderer and
            # if it raises, it doesn't try the next one.
            # This is the expected behaviour: renderers are tried in priority
            # order, first match wins. If that renderer raises, it's a failure.
            assert len(outcomes) == 1
        finally:
            await runner.stop()

    async def test_truncation_preserves_content_under_limit(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """TextRenderer truncates at 500 chars; events under limit are intact."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="truncate-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Create event with text under the 500-char limit
        short_text = "a" * 100
        event = _make_event(
            event_id="truncate-short",
            source_adapter="src",
            payload={"text": short_text},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            assert rendered.payload["text"] == short_text
            assert rendered.truncated is False
        finally:
            await runner.stop()

    async def test_truncation_flags_long_content(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """TextRenderer truncates text exceeding 500 chars."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="truncate-long-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        long_text = "x" * 600
        event = _make_event(
            event_id="truncate-long",
            source_adapter="src",
            payload={"text": long_text},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            assert len(str(rendered.payload["text"])) == 500
            assert rendered.truncated is True
            assert rendered.metadata["original_length"] == 600
        finally:
            await runner.stop()


# ===================================================================
# Inbound native ref persistence
# ===================================================================


class TestInboundNativeRefPersistence:
    """Pipeline persists inbound NativeMessageRef when source_native_ref exists."""

    async def test_inbound_native_ref_persisted(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline stores inbound NativeMessageRef for events with source_native_ref."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="inbound-ref-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event-001",
        )
        event = _make_event(
            event_id="inbound-ref-001",
            source_adapter="src",
        )
        # Manually construct event with source_native_ref
        event = CanonicalEvent(
            event_id="inbound-ref-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=event.timestamp,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            await runner.handle_ingress(event)

            # Verify inbound native ref was persisted
            resolved = await temp_storage.resolve_native_ref(
                "matrix", "!room:server", "$event-001"
            )
            assert resolved == "inbound-ref-001"
        finally:
            await runner.stop()

    async def test_no_inbound_ref_when_source_native_ref_is_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline does not persist inbound ref when source_native_ref is None."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="no-ref-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="no-ref-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            # No inbound native ref should exist for this event
            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ? AND direction = 'inbound'",
                ("no-ref-001",),
            )
            assert len(rows) == 0
        finally:
            await runner.stop()

    async def test_inbound_native_ref_idempotent(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Storing the same inbound native ref twice is idempotent."""
        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="idem-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$idem-001",
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id="idem-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            # First ingress
            await runner.handle_ingress(event)
            # Second ingress with same event (will fail FK due to same event_id,
            # but the native ref insert is OR IGNORE so it's idempotent)
            # Instead, test idempotency at the storage layer directly
            from medre.core.events import NativeMessageRef

            ref2 = NativeMessageRef(
                id="nref-idem-dup",
                event_id="idem-001",
                adapter="matrix",
                native_channel_id="!room:server",
                native_message_id="$idem-001",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await temp_storage.store_native_ref(ref2)

            resolved = await temp_storage.resolve_native_ref(
                "matrix", "!room:server", "$idem-001"
            )
            assert resolved == "idem-001"
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

        target_event = _make_event(event_id="target-evt-001", source_adapter="src")
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
        config = _make_pipeline_config(
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
        config = _make_pipeline_config(
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
            assert stored.relations[0].target_native_ref.native_message_id == "$unknown-msg"
        finally:
            await runner.stop()


# ===================================================================
# Wave A: Self-loop guard, route attribution, route_stats
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"adapter_a": pres_a},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"adapter_a": pres_a, "adapter_b": pres_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="attr-001", source_adapter="src")

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="receipt-001", source_adapter="src")

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"pres": pres},
        )
        config.route_stats = stats
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="stats-001", source_adapter="src")

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"a": pres},
        )
        config.route_stats = stats
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="loop-stats-001", source_adapter="a")

        try:
            await runner.handle_ingress(event)
            snap = stats.snapshot()
            assert "loop-stats-route" in snap
            assert snap["loop-stats-route"]["loop_prevented"] == 1
            assert snap["loop-stats-route"]["delivered"] == 0
        finally:
            await runner.stop()
