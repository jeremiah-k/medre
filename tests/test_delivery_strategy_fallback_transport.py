"""Transport-specific fallback_text delivery strategy tests.

Verifies that each transport renderer (Matrix, LXMF, Meshtastic, MeshCore)
produces its native payload shape with degraded relation text when
delivery_strategy is ``"fallback_text"``, and suppresses native relation
fields.

All renderers and mock objects use the strict :class:`RenderingContext`
protocol — no legacy positional-arg signatures.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.rendering.renderer import (
    CapabilityLevel,
    DeliveryStrategyMethod,
    RenderingContext,
)

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
    capability_level: CapabilityLevel = "native",
) -> RenderingContext:
    """Build a frozen RenderingContext for unit tests."""
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform=target_platform,
        max_text_chars=max_text_chars,
        max_text_bytes=max_text_bytes,
        capability_level=capability_level,
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
        ctx = _ctx(
            delivery_strategy="direct", target_adapter="dest", target_platform="matrix"
        )

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
    produces its native payloads (text, channel_index, meshnet_name) with
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
