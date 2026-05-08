"""Pipeline integration tests: EventBus + PipelineRunner + storage + routing + adapters.

Tests the full event lifecycle from ingress through storage, routing,
delivery planning, adapter delivery, and receipt recording.  Exercises
error isolation, middleware-based event dropping, multi-target fanout,
reaction event handling, target-scoped failure semantics, and
diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.observability.metrics import Diagnostician, EventMetrics
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryOutcome
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import SQLiteStorage


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
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=object()),
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
        lineage=[],
        relations=[],
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

            # Adapter received the event
            assert event in fake_presentation.received_events

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

            # Each adapter received the event
            assert event in pres_a.received_events
            assert event in pres_b.received_events

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
                self.received_events: list[CanonicalEvent] = []

            async def deliver(self, event: CanonicalEvent) -> None:
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

            # Good adapter received event despite the failure
            assert event in good.received_events

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

            # Adapter received the reaction event
            assert event in fake_presentation.received_events

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
                self.received_events: list[CanonicalEvent] = []

            async def deliver(self, event: CanonicalEvent) -> None:
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
            assert "RuntimeError" in by_adapter["broken"].error

            # Good adapters actually received the event.
            assert event in good_a.received_events
            assert event in good_b.received_events
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
                self.received_events: list[CanonicalEvent] = []

            async def deliver(self, event: CanonicalEvent) -> None:
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

            # Good adapter succeeded.
            assert by_adapter["stable"].status == "success"
            assert event in good.received_events

            # Flaky adapter classified as transient.
            assert (
                by_adapter["transient-broken"].status == "transient_failure"
            )
            assert (
                "ConnectionError" in by_adapter["transient-broken"].error
            )

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
                self.received_events: list[CanonicalEvent] = []

            async def deliver(self, event: CanonicalEvent) -> None:
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
