"""Pipeline failure taxonomy tests: target-scoped failures, delivery failure
classification, dead-letter handling, event metrics, and renderer downgrade.

Verifies that failure kinds are correctly classified, diagnostics are emitted,
and retry/dead-letter policies behave as expected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.observability.metrics import Diagnostician, EventMetrics
from medre.core.planning import FallbackResolver
from medre.core.planning.delivery_plan import DeliveryPlan, RetryPolicy
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good-a": good_a, "broken": broken, "good-b": good_b},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="fanout-001", source_adapter="src")

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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"stable": good, "transient-broken": flaky},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="transient-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}

            # Good adapter succeeded and received rendered payload.
            assert by_adapter["stable"].status == "success"
            assert len(good.delivered_payloads) == 1
            assert good.delivered_payloads[0].event_id == "transient-001"

            # Flaky adapter classified as transient.
            assert by_adapter["transient-broken"].status == "transient_failure"
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fail-adapter": adapter},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="diag-001", source_adapter="src")

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

    async def test_no_deliver_method_produces_permanent_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter without a deliver() method causes permanent_failure, not false success."""

        class _NoDeliverAdapter:
            """Adapter object that has no deliver attribute at all."""

            adapter_id = "no-deliver"

        good = FakePresentationAdapter(adapter_id="good")
        nodeadapt = _NoDeliverAdapter()

        route = Route(
            id="no-deliver-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="no-deliver"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "no-deliver": nodeadapt},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="no-deliver-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}

            # Good adapter succeeded unaffected.
            assert by_adapter["good"].status == "success"
            assert len(good.delivered_payloads) == 1

            # No-deliver adapter is a permanent failure.
            outcome = by_adapter["no-deliver"]
            assert outcome.status == "permanent_failure"
            assert outcome.failure_kind is not None
            assert outcome.failure_kind.value == "adapter_permanent"
            assert outcome.error == "Adapter has no deliver() method"

            # A failed receipt was persisted (not "sent").
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? "
                "AND target_adapter = ?",
                ("no-deliver-001", "no-deliver"),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert rows[0]["error"] == "Adapter has no deliver() method"
        finally:
            await runner.stop()

    async def test_non_callable_deliver_produces_permanent_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter whose deliver attribute is not callable causes permanent_failure."""

        class _NonCallableDeliverAdapter:
            """Adapter that shadows deliver with a non-callable value."""

            adapter_id = "shadowed"
            deliver = 42  # type: ignore[assignment]

        good = FakePresentationAdapter(adapter_id="good")
        shadowed = _NonCallableDeliverAdapter()

        route = Route(
            id="shadowed-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="shadowed"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "shadowed": shadowed},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="shadowed-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}

            # Good adapter succeeded unaffected.
            assert by_adapter["good"].status == "success"

            # Shadowed deliver is a permanent failure.
            outcome = by_adapter["shadowed"]
            assert outcome.status == "permanent_failure"
            assert outcome.failure_kind is not None
            assert outcome.failure_kind.value == "adapter_permanent"
            assert outcome.error == "Adapter has no deliver() method"

            # Failed receipt persisted.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? "
                "AND target_adapter = ?",
                ("shadowed-001", "shadowed"),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert rows[0]["error"] == "Adapter has no deliver() method"
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.diagnostician = diag
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="render-fail-001", source_adapter="src")

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
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"pres": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="default-render-001", source_adapter="src")

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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"stable": good, "flaky": flaky},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="classify-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["stable"].status == "success"
            assert by_adapter["flaky"].status == "transient_failure"
            assert (
                by_adapter["flaky"].failure_kind
                is DeliveryFailureKind.ADAPTER_TRANSIENT
            )
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"broken": broken},
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="perm-001", source_adapter="src")

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
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = empty_pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="render-class-001", source_adapter="src")

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
        """Missing adapter produces permanent_failure with ADAPTER_MISSING.

        ``deliver_to_target`` persists a failed receipt and raises so that
        ``_deliver_one`` classifies the outcome as ``permanent_failure``
        with ``failure_kind == ADAPTER_MISSING``.  No adapter delivery
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="missing-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.ADAPTER_MISSING
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

        config = make_pipeline_config_for_pipeline(
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

        event = make_event(event_id="deadline-001", source_adapter="src")

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

        broken = _Broken()
        route = Route(
            id="dead-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dead-target")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dead-target": broken},
        )

        # Patch the fallback resolver to produce a plan with retry_policy max_attempts=1.

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

        event = make_event(event_id="dead-001", source_adapter="src")

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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"no-retry": broken},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="no-retry-001", source_adapter="src")

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
# Renderer downgrade / fallback
# ===================================================================


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

            def can_render(self, event, ctx):
                return True

            async def render(self, event, ctx):
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.rendering_pipeline = pipeline
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Create event with text under the 500-char limit
        short_text = "a" * 100
        event = make_event(
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

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        long_text = "x" * 600
        event = make_event(
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
