"""Pipeline strategy dispatch and skip-strategy defense-in-depth tests.

Verifies:

* **RenderingPipeline dispatch** — ``fallback_text`` is a context hint,
  not a renderer selector.  The pipeline passes strategy through to the
  matching renderer without bypassing non-text renderers.
* **Skip strategy** — returns suppressed/skipped outcome, does not record
  renderer failure, and creates no outbox or capacity side effects.

All renderers and mock objects use the strict :class:`RenderingContext`
protocol — no legacy positional-arg signatures.
"""

from __future__ import annotations

import dataclasses

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# Helpers
# ===================================================================


class _FakeCtxRenderer:
    """Mock renderer using strict RenderingContext protocol.

    Accepts any event and returns a payload indicating which renderer
    was used and what delivery_strategy was passed.
    """

    name: str = "fake_ctx"

    def can_render(self, event: CanonicalEvent, ctx: RenderingContext) -> bool:
        return True

    async def render(
        self, event: CanonicalEvent, ctx: RenderingContext
    ) -> RenderingResult:
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload={
                "renderer": self.name,
                "delivery_strategy": ctx.delivery_strategy,
            },
        )


# ===================================================================
# TestRenderingPipelineStrategyDispatch
# ===================================================================


class TestRenderingPipelineStrategyDispatch:
    """Unit tests for RenderingPipeline delivery_strategy dispatch logic.

    All mock renderers use the strict RenderingContext protocol.
    """

    @pytest.mark.asyncio
    async def test_fallback_text_is_context_hint_not_renderer_selector(
        self,
    ) -> None:
        """fallback_text strategy does NOT bypass non-text renderers.

        With the RenderingContext protocol, delivery_strategy is a context
        hint passed to the renderer — it does NOT cause the pipeline to
        skip non-text renderers.  The first matching renderer handles the
        event regardless of strategy.
        """
        pipeline = RenderingPipeline()
        pipeline.register(_FakeCtxRenderer(), priority=10)
        pipeline.register(TextRenderer(), priority=100)

        event = make_event(event_kind="message.created")

        # Both direct and fallback_text use the same renderer.
        result_direct = await pipeline.render(event, "adapter-x")
        assert result_direct.payload.get("renderer") == "fake_ctx"

        result_fallback = await pipeline.render(
            event,
            "adapter-x",
            delivery_strategy="fallback_text",
        )
        # Same renderer handles it — NOT forced to TextRenderer.
        assert result_fallback.payload.get("renderer") == "fake_ctx"
        # But the strategy is passed through as context.
        assert result_fallback.payload.get("delivery_strategy") == "fallback_text"

    @pytest.mark.asyncio
    async def test_no_matching_renderer_raises_valueerror(self) -> None:
        """Empty pipeline raises ValueError."""
        pipeline = RenderingPipeline()
        event = make_event(event_kind="message.created")
        with pytest.raises(ValueError, match="No renderer registered"):
            await pipeline.render(event, "adapter-x")

    @pytest.mark.asyncio
    async def test_text_renderer_receives_rendering_context(self) -> None:
        """TextRenderer receives RenderingContext with strategy hint."""
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = make_event(event_kind="message.text")
        result = await pipeline.render(
            event,
            "adapter-x",
            delivery_strategy="fallback_text",
        )

        # TextRenderer still produces text payload.
        assert "text" in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("renderer") == "text"


# ===================================================================
# TestSkipStrategyDefenseInDepth
# ===================================================================


class TestSkipStrategyDefenseInDepth:
    """Verify the skip-strategy defense-in-depth gate in deliver_to_target."""

    @pytest.mark.asyncio
    async def test_skip_plan_returns_suppressed_receipt(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A plan with method='skip' returns a suppressed receipt from
        deliver_to_target."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="skip-defense-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
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
            event_id="skip-defense-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        skip_plan = DeliveryPlan(
            plan_id="plan:skip-defense",
            event_id=event.event_id,
            target=RouteTarget(adapter="dest"),
            primary_strategy=DeliveryStrategy(method="skip"),
        )

        try:
            receipt = await runner.deliver_to_target(event, route, skip_plan)
            assert receipt.status == "suppressed"
            assert receipt.failure_kind == "capability_suppressed"
            assert receipt.error is not None
            assert "delivery_skipped" in receipt.error
            assert "skip" in receipt.error
            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_skip_does_not_record_renderer_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Skip returns suppressed receipt, NOT renderer_failure.

        A skip is a capability suppression — it must not be classified
        as RENDERER_FAILURE because the renderer was never invoked.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="skip-no-renderer-fail-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
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
            event_id="skip-no-renderer-fail-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        skip_plan = DeliveryPlan(
            plan_id="plan:skip-no-renderer-fail",
            event_id=event.event_id,
            target=RouteTarget(adapter="dest"),
            primary_strategy=DeliveryStrategy(method="skip"),
        )

        try:
            receipt = await runner.deliver_to_target(event, route, skip_plan)
            # Suppressed, NOT renderer_failure.
            assert receipt.status == "suppressed"
            assert receipt.failure_kind != "renderer_failure"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_skip_via_deliver_to_targets_returns_skipped_outcome(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """deliver_to_targets with skip plan returns status='skipped', not
        'success'."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="skip-outcome-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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
            event_id="skip-outcome-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            # Skipped/suppressed, not success.
            assert outcome.status == "skipped"
            assert outcome.failure_kind is not None
            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_skip_no_adapter_call_and_no_outbox_for_skipped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Skip in _deliver_one does not create outbox items or call
        the adapter."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="skip-no-outbox-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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
            event_id="skip-no-outbox-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0

            # Check no outbox items created for the skipped event.
            # (Outbox items for skipped events are not created because
            # skip is handled in Phase 2.75, before outbox creation in
            # Phase 3.5.)
            outbox_items = await temp_storage.list_outbox_items()
            skipped_items = [i for i in outbox_items if i.event_id == event.event_id]
            assert len(skipped_items) == 0
        finally:
            await runner.stop()


# ===================================================================
# TestPlanSkipAccounting
# ===================================================================


class TestPlanSkipAccounting:
    """Verify plan-level skip (Phase 2.75) updates RouteStats and
    RuntimeAccounting counters, mirroring Phase 2.5 capability suppression.
    """

    @pytest.mark.asyncio
    async def test_plan_skip_updates_route_stats(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Plan-level skip increments RouteStats.capability_suppressed."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="skip-stats-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])
        stats = RouteStats()

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config = dataclasses.replace(config, route_stats=stats)
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="skip-stats-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            await runner.handle_ingress(event)

            snap = stats.snapshot()
            assert "skip-stats-route" in snap
            assert snap["skip-stats-route"]["capability_suppressed"] == 1
            assert snap["skip-stats-route"]["delivered"] == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_plan_skip_updates_runtime_accounting(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Plan-level skip increments RuntimeAccounting.capability_suppressed."""
        acc = RuntimeAccounting()
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="skip-acc-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config = dataclasses.replace(config, runtime_accounting=acc)
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="skip-acc-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            await runner.handle_ingress(event)

            snap = acc.snapshot()
            assert snap["capability_suppressed"] == 1
        finally:
            await runner.stop()
