"""Integration tests for delivery_strategy threading through the pipeline.

Verifies that the delivery strategy resolved by ``FallbackResolver`` is
correctly threaded into the rendering pipeline and adapter delivery:

* **fallback_text** — forces the text renderer, skips platform-specific
  renderers, and sets ``fallback_applied="strategy_fallback_text"`` on
  the :class:`~medre.core.rendering.renderer.RenderingResult`.
* **direct** — renders normally through the standard renderer pipeline.
* **skip** — defense-in-depth gate in ``deliver_to_target`` (covered by
  ``test_capability_pipeline_enforcement`` tests; not duplicated here).

These tests exercise the full pipeline with fake adapters and storage,
following the same patterns as ``test_capability_pipeline_enforcement.py``.
"""

from __future__ import annotations

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# Reusable relation for reply-capability tests.
_REPLY_RELATION = EventRelation(
    relation_type="reply",
    target_event_id="evt-parent",
    target_native_ref=NativeRef(
        adapter="test_adapter",
        native_channel_id="ch-0",
        native_message_id="native-001",
    ),
    key=None,
    fallback_text="original message",
)


# ===================================================================
# TestDeliveryStrategyFallbackTextRendering
# ===================================================================


class TestDeliveryStrategyFallbackTextRendering:
    """Verify fallback_text strategy forces the text renderer.

    When an adapter declares a capability as ``"fallback"`` the
    ``FallbackResolver`` produces a ``DeliveryStrategy(method="fallback_text")``.
    The pipeline threads this into the rendering pipeline, which skips
    platform-specific renderers and routes exclusively to ``TextRenderer``.
    The resulting ``RenderingResult`` has ``payload={"text": ...}``,
    ``metadata["renderer"] == "text"``, and
    ``fallback_applied == "strategy_fallback_text"``.
    """

    @pytest.mark.asyncio
    async def test_fallback_reaction_uses_text_renderer(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='fallback' renders message.reacted as text."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="fallback",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="fallback-reaction-route",
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
            event_id="fallback-react-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None
            assert outcome.target_adapter == "dest"

            # Adapter received exactly one rendered payload.
            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]

            # Text renderer format: payload has {"text": ...}.
            assert "text" in rendered.payload
            assert rendered.metadata.get("renderer") == "text"

            # Strategy fallback metadata set.
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()


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
        """A plan with method='skip' returns a suppressed receipt from deliver_to_target."""
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

        # Create a plan with method="skip" — this bypasses the upstream
        # capability_suppressed check which only fires in deliver_to_targets().
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
            assert "delivery_skipped" in receipt.error
            assert "skip" in receipt.error
            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()


# ===================================================================
# TestRenderingPipelineStrategyDispatch
# ===================================================================


class TestRenderingPipelineStrategyDispatch:
    """Unit tests for RenderingPipeline delivery_strategy dispatch logic."""

    @pytest.mark.asyncio
    async def test_fallback_text_skips_non_text_renderer(self) -> None:
        """fallback_text strategy skips a high-priority fake renderer that
        would otherwise handle the event, forcing the text renderer."""
        # Create a fake high-priority renderer that accepts everything.
        class _FakeNativeRenderer:
            name = "fake_native"
            def can_render(self, event, adapter, platform=None):
                return True
            async def render(self, event, adapter, channel=None, *, max_text_chars=None, delivery_strategy=None):
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=adapter,
                    target_channel=channel,
                    payload={"native": True},
                )

        pipeline = RenderingPipeline()
        pipeline.register(_FakeNativeRenderer(), priority=10)  # Higher priority
        pipeline.register(TextRenderer(), priority=100)

        event = make_event(event_kind="message.created")
        # Normal rendering uses the fake native renderer.
        result_normal = await pipeline.render(event, "adapter-x")
        assert result_normal.payload.get("native") is True

        # With fallback_text, the fake native renderer is skipped.
        result_fallback = await pipeline.render(
            event, "adapter-x", delivery_strategy="fallback_text",
        )
        assert "text" in result_fallback.payload
        assert result_fallback.metadata.get("renderer") == "text"
        assert result_fallback.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_no_matching_renderer_raises_valueerror(self) -> None:
        """Empty pipeline raises ValueError."""
        pipeline = RenderingPipeline()
        event = make_event(event_kind="message.created")
        with pytest.raises(ValueError, match="No renderer registered"):
            await pipeline.render(event, "adapter-x")

    @pytest.mark.asyncio
    async def test_renderer_with_delivery_strategy_only(self) -> None:
        """A renderer accepting only delivery_strategy (no max_text_chars) is
        dispatched correctly — covers the sup_ds-only branch."""
        class _StrategyOnlyRenderer:
            name = "strategy_only"
            def can_render(self, event, adapter, platform=None):
                return True
            # NOTE: no max_text_chars param — only delivery_strategy
            async def render(self, event, adapter, channel=None, *, delivery_strategy=None):
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=adapter,
                    target_channel=channel,
                    payload={"strategy": delivery_strategy},
                )

        pipeline = RenderingPipeline()
        pipeline.register(_StrategyOnlyRenderer(), priority=10)
        event = make_event(event_kind="message.created")
        result = await pipeline.render(
            event, "adapter-x", delivery_strategy="direct",
        )
        assert result.payload.get("strategy") == "direct"


    @pytest.mark.asyncio
    async def test_fallback_delete_uses_text_renderer(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with deletes='fallback' renders message.deleted as text."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="fallback",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="fallback-delete-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.deleted",),
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
            event_id="fallback-delete-001",
            event_kind="message.deleted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "original message"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None

            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]

            # Text renderer format.
            assert "text" in rendered.payload
            assert rendered.metadata.get("renderer") == "text"

            # Strategy fallback applied.
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_fallback_reply_uses_text_renderer(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with replies='fallback' renders message.text with reply
        relation as text."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="fallback",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="fallback-reply-route",
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
            event_id="fallback-reply-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "a reply message"},
            relations=(_REPLY_RELATION,),
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None

            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]

            # Text renderer format.
            assert "text" in rendered.payload
            assert rendered.metadata.get("renderer") == "text"

            # Strategy fallback takes precedence over relation fallback.
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()


# ===================================================================
# TestDeliveryStrategyDirectRendering
# ===================================================================


class TestDeliveryStrategyDirectRendering:
    """Verify direct strategy renders normally through standard pipeline.

    When an adapter declares native support for a capability, the
    ``FallbackResolver`` produces ``DeliveryStrategy(method="direct")``.
    The rendering pipeline proceeds normally without forcing any specific
    renderer.
    """

    @pytest.mark.asyncio
    async def test_direct_reaction_renders_normally(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='native' renders message.reacted normally."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="direct-reaction-route",
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
            event_id="direct-react-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None
            assert outcome.target_adapter == "dest"

            # Adapter received exactly one rendered payload.
            assert len(adapter.delivered_payloads) == 1

            rendered = adapter.delivered_payloads[0]

            # No strategy_fallback_text — the direct strategy was used.
            assert rendered.fallback_applied != "strategy_fallback_text"
        finally:
            await runner.stop()


# ===================================================================
# TestDeliveryStrategyFallbackAppliedOnRenderingResult
# ===================================================================


class TestDeliveryStrategyFallbackAppliedOnRenderingResult:
    """Verify fallback_applied metadata on the RenderingResult.

    Ensures that the ``fallback_applied`` field on
    :class:`~medre.core.rendering.renderer.RenderingResult` is set to
    ``"strategy_fallback_text"`` when the delivery strategy is
    ``fallback_text``, and is ``None`` (or relation-based only) when
    the strategy is ``direct``.
    """

    @pytest.mark.asyncio
    async def test_fallback_text_strategy_sets_fallback_applied(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """reactions='fallback' → fallback_applied='strategy_fallback_text'."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="fallback",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="fallback-applied-route",
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
            event_id="fallback-applied-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_direct_strategy_no_fallback_applied(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """reactions='native' → fallback_applied is None (no strategy override)."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="direct-no-fallback-route",
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
            event_id="direct-no-fallback-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            # Direct strategy: no strategy_fallback_text, and no relations
            # on this event, so fallback_applied should be None.
            assert rendered.fallback_applied is None
        finally:
            await runner.stop()
