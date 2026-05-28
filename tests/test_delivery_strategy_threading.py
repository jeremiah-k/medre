"""Integration and unit tests for delivery_strategy threading through the pipeline.

Verifies that the delivery strategy resolved by ``FallbackResolver`` is
correctly threaded into the rendering pipeline and adapter delivery:

* **fallback_text** — target-native renderer produces its native payload
  shape with degraded relation text (not forcing TextRenderer).  Each
  transport renderer (Matrix, LXMF, Meshtastic, MeshCore) emits its own
  native format while suppressing native relation fields.
* **direct** — renders normally through the standard renderer pipeline.
* **skip** — returns suppressed/skipped outcome, does not record renderer
  failure, and creates no outbox or capacity side effects.

All renderers and mock objects use the strict :class:`RenderingContext`
protocol — no legacy positional-arg signatures.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.rendering.renderer import (
    DeliveryStrategyMethod,
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


# ===================================================================
# Helpers
# ===================================================================


def _ctx(
    *,
    delivery_strategy: DeliveryStrategyMethod = "direct",
    target_adapter: str = "dest",
    target_channel: str | None = None,
    target_platform: str | None = None,
    max_text_chars: int | None = None,
    max_text_bytes: int | None = None,
    capability_level: str = "native",
) -> RenderingContext:
    """Build a frozen RenderingContext for unit tests."""
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform=target_platform,
        max_text_chars=max_text_chars,
        max_text_bytes=max_text_bytes,
        capability_level=capability_level,  # type: ignore[arg-type]
    )


def _make_reaction_event(
    event_id: str = "react-001",
    source_adapter: str = "src",
    emoji: str = "\U0001f44d",
    fallback_text: str | None = None,
    target_event_id: str | None = None,
    payload: dict | None = None,
) -> CanonicalEvent:
    """Build a message.reacted event with a reaction relation."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=emoji if emoji else None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload=payload or {"emoji": emoji},
        metadata=EventMetadata(),
    )


def _make_reply_event(
    event_id: str = "reply-001",
    source_adapter: str = "src",
    body: str = "a reply",
    fallback_text: str = "original message",
    sender_displayname: str | None = None,
    sender: str | None = None,
) -> CanonicalEvent:
    """Build a message.text event with a reply relation."""
    meta: dict[str, object] = {}
    if sender_displayname:
        meta["sender_displayname"] = sender_displayname
    if sender:
        meta["sender"] = sender

    rel = EventRelation(
        relation_type="reply",
        target_event_id="evt-parent",
        target_native_ref=NativeRef(
            adapter="dest",
            native_channel_id="ch-0",
            native_message_id="native-001",
        ),
        key=None,
        fallback_text=fallback_text,
        metadata=meta,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"text": body},
        metadata=EventMetadata(),
    )


def _make_text_event(
    event_id: str = "evt-001",
    source_adapter: str = "src",
    body: str = "hello",
    event_kind: str = "message.text",
    relations: tuple | None = None,
) -> CanonicalEvent:
    """Build a simple text event."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload={"body": body},
        metadata=EventMetadata(),
    )


# ===================================================================
# TestMatrixFallbackText
# ===================================================================


class TestMatrixFallbackText:
    """Verify Matrix fallback_text emits Matrix msgtype/body and suppresses
    native relation fields.

    When delivery_strategy is ``"fallback_text"``, MatrixRenderer produces
    a valid Matrix content payload (``msgtype``, ``body``, MEDRE envelope)
    without ``m.relates_to``, ``_matrix_event_type``, or other native
    relation fields.
    """

    @pytest.mark.asyncio
    async def test_fallback_reaction_emits_msgtype_body_no_native_relations(
        self,
    ) -> None:
        """Reaction fallback: msgtype/body present, no m.relates_to or
        _matrix_event_type."""
        renderer = MatrixRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id="evt-target",
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="matrix")

        result = await renderer.render(event, ctx)

        # Matrix payload shape: msgtype and body always present.
        assert result.payload.get("msgtype") == "m.text"
        assert isinstance(result.payload.get("body"), str)
        assert len(str(result.payload["body"])) > 0

        # No native relation fields emitted.
        assert "m.relates_to" not in result.payload
        assert "_matrix_event_type" not in result.payload

        # Evidence it was fallback, not native.
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_reply_emits_msgtype_body_no_m_relates_to(
        self,
    ) -> None:
        """Reply fallback: msgtype/body present, no m.relates_to."""
        renderer = MatrixRenderer()
        event = _make_reply_event(body="my reply")
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="matrix")

        result = await renderer.render(event, ctx)

        assert result.payload.get("msgtype") == "m.text"
        assert isinstance(result.payload.get("body"), str)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_text_no_relations_is_normal_text(
        self,
    ) -> None:
        """fallback_text without relations: still produces Matrix payload,
        fallback_applied set."""
        renderer = MatrixRenderer()
        event = _make_text_event(body="plain message")
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="matrix")

        result = await renderer.render(event, ctx)

        assert result.payload.get("msgtype") == "m.text"
        assert result.payload["body"] == "plain message"
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_direct_reaction_has_native_relation_fields(
        self,
    ) -> None:
        """Direct strategy: native m.relates_to or _matrix_event_type present
        for Matrix-native target (contrast with fallback)."""
        renderer = MatrixRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id="evt-target",
        )
        # With a native Matrix target ref, direct mode produces m.relates_to.
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-target",
            target_native_ref=NativeRef(
                adapter="dest",
                native_channel_id="!room:matrix.org",
                native_message_id="$mx-event-id",
            ),
            key="\U0001f44d",
            fallback_text="original",
        )

        event = CanonicalEvent(
            event_id="react-direct",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"emoji": "\U0001f44d"},
            metadata=EventMetadata(),
        )
        ctx = _ctx(delivery_strategy="direct", target_adapter="dest", target_platform="matrix")

        result = await renderer.render(event, ctx)

        # Direct mode with native Matrix target: has relation fields.
        has_relates_to = "m.relates_to" in result.payload
        has_matrix_event_type = "_matrix_event_type" in result.payload
        assert has_relates_to or has_matrix_event_type, (
            "Direct strategy with Matrix-native target should produce "
            "native relation fields"
        )
        # No strategy fallback applied.
        assert result.fallback_applied is None


# ===================================================================
# TestLxmflFallbackText
# ===================================================================


class TestLxmflFallbackText:
    """Verify LXMF fallback_text emits content/title/fields and avoids
    empty-content false sent receipts.

    When delivery_strategy is ``"fallback_text"``, LxmfRenderer produces
    a payload with ``content``, ``title``, ``fields``, and
    ``destination_hash``.  When the event carries relations but no body
    text, the degraded inline text ensures ``content`` is non-empty,
    preventing false sent-receipt appearance from empty content.
    """

    @pytest.mark.asyncio
    async def test_fallback_reaction_produces_lxmf_payload(self) -> None:
        """Reaction fallback: content/title/fields present, non-empty content."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id="evt-target",
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        # LXMF payload shape.
        assert "content" in result.payload
        assert "title" in result.payload
        assert "fields" in result.payload
        assert "destination_hash" in result.payload

        # Content is non-empty even for reaction-only events.
        content = str(result.payload["content"])
        assert isinstance(content, str)
        assert len(content) > 0
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_reply_avoids_empty_content(self) -> None:
        """Reply with no body text: inline relation text fills content,
        preventing false sent receipt."""
        renderer = LxmfRenderer()
        event = _make_text_event(
            body="",
            event_kind="message.text",
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="evt-parent",
                    target_native_ref=None,
                    key=None,
                    fallback_text="original message",
                ),
            ),
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        content = str(result.payload["content"])
        # Non-empty: degraded relation text fills the content.
        assert len(content) > 0
        assert "reply" in content.lower()
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_reaction_no_body_uses_inline_text(self) -> None:
        """Reaction event with no body text produces non-empty content
        from inline relation degradation."""
        renderer = LxmfRenderer()
        event = _make_text_event(
            body="",
            event_kind="message.reacted",
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="evt-target",
                    target_native_ref=None,
                    key="\U0001f44d",
                    fallback_text=None,
                ),
            ),
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        content = result.payload["content"]
        assert isinstance(content, str)
        assert len(content) > 0
        # The inline text contains the relation type indicator.
        assert "reaction" in content.lower()

    @pytest.mark.asyncio
    async def test_direct_no_fallback_applied(self) -> None:
        """Direct strategy: no fallback_applied marker."""
        renderer = LxmfRenderer()
        event = _make_text_event(body="hello lxmf")
        ctx = _ctx(delivery_strategy="direct", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        assert result.payload.get("content") == "hello lxmf"
        assert result.fallback_applied is None


# ===================================================================
# TestMeshtasticFallbackText
# ===================================================================


class TestMeshtasticFallbackText:
    """Verify Meshtastic fallback_text preserves channel_index/meshnet_name,
    suppresses native reply_id/emoji, and respects byte-safe truncation.

    When delivery_strategy is ``"fallback_text"``, MeshtasticRenderer
    produces its native payload (text, channel_index, meshnet_name) but
    suppresses native relation fields (reply_id, emoji).  UTF-8
    byte-budget truncation is still applied.
    """

    @pytest.mark.asyncio
    async def test_fallback_reply_preserves_channel_and_meshnet(
        self,
    ) -> None:
        """Reply fallback: channel_index and meshnet_name preserved,
        no reply_id or emoji."""
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            meshnet_name="TestNet",
            default_channel=3,
            max_text_bytes=227,
        )
        renderer = MeshtasticRenderer(configs={"mesh-1": config})
        event = _make_reply_event(body="a reply")
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mesh-1",
            target_channel="3",
            target_platform="meshtastic",
        )

        result = await renderer.render(event, ctx)

        # Meshtastic payload shape preserved.
        assert result.payload["channel_index"] == 3
        assert result.payload["meshnet_name"] == "TestNet"

        # Native relation fields suppressed.
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload

        # Text is present.
        assert isinstance(result.payload.get("text"), str)
        assert len(str(result.payload["text"])) > 0

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_reaction_suppresses_emoji_and_reply_id(
        self,
    ) -> None:
        """Reaction fallback: no emoji=1, no reply_id, readable text instead."""
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            meshnet_name="TestNet",
            max_text_bytes=227,
        )
        renderer = MeshtasticRenderer(configs={"mesh-1": config})
        event = _make_reaction_event(
            emoji="\U0001f44d",
            source_adapter="matrix-src",
            target_event_id="evt-target",
        )
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mesh-1",
            target_platform="meshtastic",
        )

        result = await renderer.render(event, ctx)

        # Native tapback fields suppressed.
        assert "emoji" not in result.payload
        assert "reply_id" not in result.payload

        # Channel/meshnet preserved.
        assert result.payload["channel_index"] == 0
        assert result.payload["meshnet_name"] == "TestNet"

        # Text contains readable reaction info.
        text = str(result.payload["text"])
        assert len(text) > 0

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_respects_byte_truncation(self) -> None:
        """fallback_text still applies UTF-8 byte-budget truncation."""
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            meshnet_name="TestNet",
            max_text_bytes=10,  # Very tight budget
        )
        renderer = MeshtasticRenderer(configs={"mesh-1": config})
        long_body = "A" * 200
        event = _make_text_event(
            body=long_body,
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="evt-parent",
                    target_native_ref=None,
                    key=None,
                    fallback_text="original",
                ),
            ),
        )
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mesh-1",
            target_platform="meshtastic",
        )

        result = await renderer.render(event, ctx)

        # Text truncated to byte budget.
        text = result.payload["text"]
        assert isinstance(text, str)
        assert len(text.encode("utf-8")) <= 10
        assert result.truncated is True
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_direct_reply_has_reply_id_when_native_ref_present(
        self,
    ) -> None:
        """Direct strategy with native Meshtastic ref: reply_id is set
        (contrast with fallback suppression)."""
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            meshnet_name="TestNet",
            max_text_bytes=227,
        )
        renderer = MeshtasticRenderer(configs={"mesh-1": config})
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="12345",
            ),
            key=None,
            fallback_text="original",
        )

        event = CanonicalEvent(
            event_id="reply-direct",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "my reply"},
            metadata=EventMetadata(),
        )
        ctx = _ctx(
            delivery_strategy="direct",
            target_adapter="mesh-1",
            target_channel="0",
            target_platform="meshtastic",
        )

        result = await renderer.render(event, ctx)

        # Direct mode with native ref: reply_id present.
        assert result.payload.get("reply_id") == 12345
        assert result.fallback_applied is None


# ===================================================================
# TestMeshCoreFallbackText
# ===================================================================


class TestMeshCoreFallbackText:
    """Verify MeshCore fallback_text preserves channel/contact/destination
    semantics.

    When delivery_strategy is ``"fallback_text"``, MeshCoreRenderer
    produces its native payload (text, channel_index, meshnet_name) with
    degraded inline relation text.
    """

    @pytest.mark.asyncio
    async def test_fallback_preserves_channel_and_meshnet(self) -> None:
        """Fallback text preserves channel_index and meshnet_name."""
        config = MeshCoreConfig(
            adapter_id="mc-1",
            meshnet_name="CoreNet",
            default_channel=2,
            max_text_bytes=512,
        )
        renderer = MeshCoreRenderer(configs={"mc-1": config})
        event = _make_reply_event(body="a reply")
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mc-1",
            target_channel="2",
            target_platform="meshcore",
        )

        result = await renderer.render(event, ctx)

        # MeshCore payload shape preserved.
        assert result.payload["channel_index"] == 2
        assert result.payload["meshnet_name"] == "CoreNet"

        # Text present.
        assert isinstance(result.payload.get("text"), str)
        assert len(str(result.payload["text"])) > 0

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_reaction_non_empty_text(self) -> None:
        """Reaction fallback on MeshCore: non-empty text from inline
        relation degradation."""
        config = MeshCoreConfig(
            adapter_id="mc-1",
            meshnet_name="CoreNet",
            max_text_bytes=512,
        )
        renderer = MeshCoreRenderer(configs={"mc-1": config})
        event = _make_text_event(
            body="",
            event_kind="message.reacted",
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="evt-target",
                    target_native_ref=None,
                    key="\U0001f44d",
                    fallback_text=None,
                ),
            ),
        )
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mc-1",
            target_platform="meshcore",
        )

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert isinstance(text, str)
        assert len(text) > 0
        assert "reaction" in text.lower()
        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_fallback_respects_byte_truncation(self) -> None:
        """fallback_text on MeshCore applies byte-budget truncation."""
        config = MeshCoreConfig(
            adapter_id="mc-1",
            meshnet_name="CoreNet",
            max_text_bytes=15,
        )
        renderer = MeshCoreRenderer(configs={"mc-1": config})
        event = _make_text_event(body="A" * 200)
        ctx = _ctx(
            delivery_strategy="fallback_text",
            target_adapter="mc-1",
            target_platform="meshcore",
        )

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert isinstance(text, str)
        assert len(text.encode("utf-8")) <= 15
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_direct_no_fallback_applied(self) -> None:
        """Direct strategy: no fallback_applied marker."""
        config = MeshCoreConfig(
            adapter_id="mc-1",
            meshnet_name="CoreNet",
        )
        renderer = MeshCoreRenderer(configs={"mc-1": config})
        event = _make_text_event(body="hello")
        ctx = _ctx(
            delivery_strategy="direct",
            target_adapter="mc-1",
            target_platform="meshcore",
        )

        result = await renderer.render(event, ctx)

        assert result.fallback_applied is None


# ===================================================================
# TestRenderingPipelineStrategyDispatch
# ===================================================================


class _FakeCtxRenderer:
    """Mock renderer using strict RenderingContext protocol.

    Accepts any event and returns a payload indicating which renderer
    was used and what delivery_strategy was passed.
    """

    name: str = "fake_ctx"

    def can_render(self, event: "CanonicalEvent", ctx: RenderingContext) -> bool:
        return True

    async def render(
        self, event: "CanonicalEvent", ctx: RenderingContext
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
            event, "adapter-x", delivery_strategy="fallback_text",
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
            skipped_items = [
                i for i in outbox_items
                if i.event_id == event.event_id
            ]
            assert len(skipped_items) == 0
        finally:
            await runner.stop()


# ===================================================================
# TestFallbackReactionText
# ===================================================================


class TestFallbackReactionText:
    """Verify reaction fallback produces meaningful text when emoji present
    but no relation target is available.

    When a reaction event has an emoji/key but no target_native_ref
    (no relation target), the fallback text renderer still produces
    meaningful output — not empty or ambiguous text.
    """

    @pytest.mark.asyncio
    async def test_reaction_with_emoji_no_target_yields_meaningful_text(
        self,
    ) -> None:
        """Reaction with emoji but no native target: TextRenderer produces
        '{actor} reacted with {emoji}'."""
        renderer = TextRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id=None,
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Contains the emoji.
        assert "\U0001f44d" in text
        # Contains reaction indicator.
        assert "reacted" in text.lower()

    @pytest.mark.asyncio
    async def test_reaction_no_emoji_no_target_yields_meaningful_text(
        self,
    ) -> None:
        """Reaction with no emoji and no target: still produces meaningful
        text, not empty."""
        renderer = TextRenderer()
        event = _make_text_event(
            event_kind="message.reacted",
            body="",
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id=None,
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Still says "reacted".
        assert "reacted" in text.lower()

    @pytest.mark.asyncio
    async def test_reaction_with_emoji_no_target_matrix_fallback(
        self,
    ) -> None:
        """Matrix fallback_text for reaction with no target: body contains
        reaction info, no m.relates_to."""
        renderer = MatrixRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id=None,
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="matrix")

        result = await renderer.render(event, ctx)

        body = result.payload.get("body", "")
        assert isinstance(body, str)
        assert "\U0001f44d" in body
        assert "m.relates_to" not in result.payload
        assert "_matrix_event_type" not in result.payload


# ===================================================================
# TestFallbackReplySenderMetadata
# ===================================================================


class TestFallbackReplySenderMetadata:
    """Verify reply fallback uses enriched sender/displayname metadata
    from the relation.

    When the pipeline enriches relations with sender_displayname and
    sender metadata, the text fallback for replies includes the sender
    information.
    """

    @pytest.mark.asyncio
    async def test_reply_fallback_uses_sender_displayname(self) -> None:
        """Reply with sender_displayname in metadata: fallback text includes
        'by {displayname}'."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
            sender_displayname="Alice",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        # Sender displayname used in reply context.
        assert "Alice" in text

    @pytest.mark.asyncio
    async def test_reply_fallback_uses_sender_fallback(self) -> None:
        """Reply with sender (no displayname): fallback uses sender field."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
            sender="@user:matrix.org",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        # Sender ID used when no displayname available.
        assert "@user:matrix.org" in text

    @pytest.mark.asyncio
    async def test_reply_fallback_without_sender_no_crash(self) -> None:
        """Reply without sender metadata: no crash, meaningful text still
        produced."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Should still contain the reply target.
        assert "original message" in text


# ===================================================================
# TestFallbackTextEvidenceRecording
# ===================================================================


class TestFallbackTextEvidenceRecording:
    """Verify fallback_text records evidence it was fallback, not native
    relation delivery.

    The RenderingResult.fallback_applied field is set to
    ``"strategy_fallback_text"`` when the delivery strategy is
    fallback_text, regardless of which renderer handles the event.
    """

    @pytest.mark.asyncio
    async def test_matrix_fallback_records_strategy_marker(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Matrix adapter with reactions='fallback' records
        fallback_applied='strategy_fallback_text' on the rendered result."""
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
            id="fallback-evidence-route",
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
            event_id="fallback-evidence-001",
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
            # Evidence marker: strategy_fallback_text, not a native relation.
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_direct_reaction_no_fallback_marker(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='native': no fallback_applied marker."""
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
            assert rendered.fallback_applied != "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_text_renderer_fallback_records_strategy_marker(
        self,
    ) -> None:
        """TextRenderer with fallback_text strategy records
        fallback_applied='strategy_fallback_text'."""
        renderer = TextRenderer()
        event = _make_reaction_event(emoji="\U0001f44d")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_text_renderer_direct_no_strategy_marker(
        self,
    ) -> None:
        """TextRenderer with direct strategy: no strategy_fallback_text."""
        renderer = TextRenderer()
        event = _make_text_event(body="hello")
        ctx = _ctx(delivery_strategy="direct")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied is None

    @pytest.mark.asyncio
    async def test_lxmf_fallback_records_strategy_marker(self) -> None:
        """LxmfRenderer with fallback_text records fallback_applied."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(emoji="\U0001f44d")
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"


# ===================================================================
# TestTextRendererEditDeleteThread
# ===================================================================


def _make_edit_event(
    event_id: str = "edit-001",
    source_adapter: str = "src",
    body: str = "",
    fallback_text: str | None = None,
    target_event_id: str | None = "evt-original",
) -> CanonicalEvent:
    """Build a message.edited event with an edit relation."""
    rel = EventRelation(
        relation_type="edit",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.edited",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body},
        metadata=EventMetadata(),
    )


def _make_delete_event(
    event_id: str = "del-001",
    source_adapter: str = "src",
    fallback_text: str | None = None,
    target_event_id: str | None = None,
    target_native_ref: NativeRef | None = None,
) -> CanonicalEvent:
    """Build a message.deleted event with a delete relation."""
    rel = EventRelation(
        relation_type="delete",
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.deleted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={},
        metadata=EventMetadata(),
    )


def _make_thread_event(
    event_id: str = "thread-001",
    source_adapter: str = "src",
    body: str = "thread reply",
    fallback_text: str | None = "thread root msg",
    target_event_id: str | None = "evt-thread-root",
) -> CanonicalEvent:
    """Build a message.text event with a thread relation."""
    rel = EventRelation(
        relation_type="thread",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body},
        metadata=EventMetadata(),
    )


class TestTextRendererEditDeleteThread:
    """Verify TextRenderer edit, delete, and thread relation fallback paths.

    These relation types have dedicated degraded-text branches in
    :meth:`TextRenderer._extract_text`.  The tests cover each branch
    including edge cases: empty edit body, delete without target context,
    and thread with/without payload text.
    """

    # -- Edit relation fallback ------------------------------------------

    @pytest.mark.asyncio
    async def test_edit_with_body_produces_edited_prefix(self) -> None:
        """Edit relation with body: '[edited] {body}'."""
        renderer = TextRenderer()
        event = _make_edit_event(body="corrected text")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[edited] corrected text"
        assert result.fallback_applied == "relation_edit"

    @pytest.mark.asyncio
    async def test_edit_empty_body_produces_bare_edited(self) -> None:
        """Edit relation with empty body: '[edited]' (no trailing space)."""
        renderer = TextRenderer()
        event = _make_edit_event(body="")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[edited]"
        assert result.fallback_applied == "relation_edit"

    @pytest.mark.asyncio
    async def test_edit_strategy_fallback_preserves_relation_in_metadata(
        self,
    ) -> None:
        """Edit relation under fallback_text strategy: strategy marker
        overrides, but relation type preserved in metadata."""
        renderer = TextRenderer()
        event = _make_edit_event(body="fixed")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("strategy_relation_type") == "edit"
        assert "[edited]" in result.payload["text"]

    # -- Delete relation fallback ----------------------------------------

    @pytest.mark.asyncio
    async def test_delete_with_target_context_includes_target(self) -> None:
        """Delete relation with fallback_text target: '[deleted: {target}]'."""
        renderer = TextRenderer()
        event = _make_delete_event(
            fallback_text="the original message",
            target_event_id="evt-abc123",
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[deleted: the original message]"
        assert result.fallback_applied == "relation_delete"

    @pytest.mark.asyncio
    async def test_delete_with_abbreviated_event_id_target(self) -> None:
        """Delete relation with no fallback_text but target_event_id:
        uses abbreviated event ID."""
        renderer = TextRenderer()
        event = _make_delete_event(
            target_event_id="evt-abcdefghijklmnop",
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        # target_event_id abbreviated to first 8 chars + ellipsis.
        assert "evt-abc" in text
        assert result.fallback_applied == "relation_delete"

    @pytest.mark.asyncio
    async def test_delete_without_target_yields_bare_deleted(self) -> None:
        """Delete relation with no target context at all: '[deleted]'."""
        renderer = TextRenderer()
        event = _make_delete_event(
            target_event_id=None,
            target_native_ref=None,
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[deleted]"
        assert result.fallback_applied == "relation_delete"

    # -- Thread relation fallback ----------------------------------------

    @pytest.mark.asyncio
    async def test_thread_with_body_includes_target_and_text(self) -> None:
        """Thread relation with body: '[thread: {target}] {body}'."""
        renderer = TextRenderer()
        event = _make_thread_event(body="a thread reply")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert "[thread: thread root msg]" in text
        assert "a thread reply" in text
        assert result.fallback_applied == "relation_thread"

    @pytest.mark.asyncio
    async def test_thread_empty_body_includes_target_only(self) -> None:
        """Thread relation with empty body: '[thread: {target}]' (no trailing
        space)."""
        renderer = TextRenderer()
        event = _make_thread_event(body="")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[thread: thread root msg]"
        assert result.fallback_applied == "relation_thread"

    @pytest.mark.asyncio
    async def test_thread_strategy_fallback_preserves_relation_in_metadata(
        self,
    ) -> None:
        """Thread relation under fallback_text strategy: strategy marker
        overrides, but relation type preserved in metadata."""
        renderer = TextRenderer()
        event = _make_thread_event(body="in-thread msg")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("strategy_relation_type") == "thread"
        assert "[thread:" in result.payload["text"]
