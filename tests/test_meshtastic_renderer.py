"""Tests for MeshtasticRenderer: constructor validation, name/can_render
dispatch, basic rendering output, target channel propagation, native reply
ID extraction, structured reply/reaction, and foreign-ref handling.

Cross-platform relation, fallback-text, and relation-degradation tests live
in test_meshtastic_renderer_relations.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext, RenderingResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_renderer(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "",
    meshnet_name: str = "",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with a single-adapter config mapping."""
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        meshnet_name=meshnet_name,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={target_adapter: config})


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="mesh-1",
        source_transport_id="!node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello mesh"},
        metadata=EventMetadata(),
    )


def _make_relation(
    relation_type: str = "reply",
    native_message_id: str | None = "42",
    key: str | None = None,
    fallback_text: str | None = None,
    adapter_id: str = "mesh-1",
) -> EventRelation:
    native_ref = None
    if native_message_id is not None:
        native_ref = NativeRef(
            adapter=adapter_id,
            native_channel_id="0",
            native_message_id=native_message_id,
        )
    return EventRelation(
        relation_type=relation_type,
        target_event_id="evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
    )


# ===================================================================
# Constructor validation
# ===================================================================


class TestMeshtasticRendererConstructor:
    """MeshtasticRenderer constructor validation."""

    def test_empty_configs_raises_value_error(self) -> None:
        """Empty configs mapping raises ValueError."""
        with pytest.raises(ValueError, match="at least one"):
            MeshtasticRenderer(configs={})

    def test_none_configs_raises_type_error(self) -> None:
        """None configs raises TypeError (keyword-only, required)."""
        with pytest.raises(TypeError):
            MeshtasticRenderer()  # type: ignore[call-arg]

    def test_single_config_works(self) -> None:
        """Single-entry configs mapping is valid."""
        config = MeshtasticConfig(adapter_id="radio-1")
        renderer = MeshtasticRenderer(configs={"radio-1": config})
        assert renderer.name == "meshtastic"

    def test_multiple_configs_works(self) -> None:
        """Multiple-entry configs mapping is valid."""
        configs = {
            "radio-a": MeshtasticConfig(adapter_id="radio-a"),
            "radio-b": MeshtasticConfig(adapter_id="radio-b"),
        }
        renderer = MeshtasticRenderer(configs=configs)
        assert renderer.name == "meshtastic"

    async def test_unknown_target_adapter_raises_key_error(self) -> None:
        """Rendering to an unknown target_adapter raises KeyError."""
        config = MeshtasticConfig(adapter_id="radio-a")
        renderer = MeshtasticRenderer(configs={"radio-a": config})
        event = _make_event()
        with pytest.raises(KeyError, match="radio-a"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="unknown-radio", delivery_strategy="direct"
                ),
            )


# ===================================================================
# Basic rendering (target_adapter = "mesh-node")
# ===================================================================


class TestMeshtasticRenderer:
    """MeshtasticRenderer output and dispatch tests."""

    def test_name_is_meshtastic(self) -> None:
        renderer = _make_renderer("mesh-node")
        assert renderer.name == "meshtastic"

    def test_can_render_meshtastic_platform(self) -> None:
        """Renderer matches when target_platform is meshtastic."""
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="local-radio",
                    delivery_strategy="direct",
                    target_platform="meshtastic",
                ),
            )
            is True
        )

    def test_can_render_non_meshtastic(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="fake_presentation",
                    delivery_strategy="direct",
                    target_platform="fake",
                ),
            )
            is False
        )

    def test_can_render_rejects_matrix(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="matrix_instance",
                    delivery_strategy="direct",
                    target_platform="matrix",
                ),
            )
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="meshtastic_node", delivery_strategy="direct"
                ),
            )
            is False
        )

    async def test_render_basic_text(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": "hello mesh"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert isinstance(result, RenderingResult)
        assert result.payload["text"] == "hello mesh"
        assert result.payload["channel_index"] == 0

    async def test_render_empty_text(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": ""})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert result.payload["text"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert "body" not in result.payload
        assert result.payload["text"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert result.payload["text"] == "fallback text"

    async def test_render_target_channel_propagation(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-node",
                delivery_strategy="direct",
                target_channel="3",
            ),
        )
        assert result.target_channel == "3"
        assert result.payload["channel_index"] == 3

    async def test_render_default_channel_when_no_target(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 0

    async def test_render_non_numeric_channel_defaults_to_zero(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-node",
                delivery_strategy="direct",
                target_channel="abc",
            ),
        )
        assert result.payload["channel_index"] == 0

    async def test_render_returns_rendering_result(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "mesh-node"

    async def test_render_includes_meshnet_name(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert "meshnet_name" in result.payload
        assert result.payload["meshnet_name"] == ""

    async def test_render_metadata_includes_renderer(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        assert result.metadata["renderer"] == "meshtastic"

    async def test_render_long_text_truncated_to_byte_budget(self) -> None:
        renderer = _make_renderer("mesh-node")
        long_text = "x" * 500
        event = _make_event(payload={"body": long_text})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-node", delivery_strategy="direct"),
        )
        # Default max_text_bytes is 227; text should be truncated
        assert len(result.payload["text"].encode("utf-8")) <= 227
        assert result.truncated is True
        assert result.payload["text"] != long_text


# ===================================================================
# _meshtastic_reply_id_from_relation
# ===================================================================


class TestNativeReplyIdFromRelation:
    """Tests for MeshtasticRenderer._meshtastic_reply_id_from_relation."""

    def test_numeric_native_message_id_returns_int(self) -> None:
        rel = _make_relation(native_message_id="42")
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") == 42
        )

    def test_large_numeric_id(self) -> None:
        rel = _make_relation(native_message_id="8589934592")
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1")
            == 8589934592
        )

    def test_non_numeric_returns_none(self) -> None:
        rel = _make_relation(native_message_id="$event:room.xyz")
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") is None
        )

    def test_no_native_ref_returns_none(self) -> None:
        rel = _make_relation(native_message_id=None)
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") is None
        )

    def test_empty_string_returns_none(self) -> None:
        rel = _make_relation(native_message_id="")
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") is None
        )

    def test_foreign_adapter_ignored_but_mmrelay_fallback(self) -> None:
        """Native ref from foreign adapter returns None (no fallback metadata)."""
        foreign_ref = NativeRef(
            adapter="matrix-1", native_channel_id="!r", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-1",
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="original",
        )
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") is None
        )

    def test_mmrelay_metadata_fallback_works(self) -> None:
        """MMRelay meshtastic_reply_id in metadata provides fallback reply ID."""
        foreign_ref = NativeRef(
            adapter="matrix-1", native_channel_id="!r", native_message_id="abc"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-1",
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="original",
            metadata={"meshtastic_reply_id": "77"},
        )
        assert (
            MeshtasticRenderer._meshtastic_reply_id_from_relation(rel, "mesh-1") == 77
        )


# ===================================================================
# Structured reply rendering
# ===================================================================


class TestRendererStructuredReply:
    """Renderer reply rendering with/without native ref."""

    async def test_reply_with_numeric_native_ref_sets_reply_id(self) -> None:
        """Reply with numeric native_message_id → reply_id in payload, plain text."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="99",
            fallback_text="original msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 99
        assert result.payload["text"] == "my reply"
        # No fallback prefix when native ref available
        assert "[replying to:" not in result.payload["text"]
        # channel_index / meshnet_name preserved
        assert result.payload["channel_index"] == 0
        assert "meshnet_name" in result.payload

    async def test_reply_without_native_ref_plain_text(self) -> None:
        """Reply without numeric native ref → plain text, no fallback prefix."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id=None,
            fallback_text="original msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        # No "[replying to: …]" prefix — plain text only.
        assert result.payload["text"] == "my reply"

    async def test_reply_with_non_numeric_ref_plain_text(self) -> None:
        """Reply with non-numeric native_message_id → plain text, no fallback prefix."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="$abc:room.server",
            fallback_text="non-mesh msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        # No "[replying to: …]" prefix — plain text only.
        assert result.payload["text"] == "my reply"

    async def test_reply_preserves_channel_index(self) -> None:
        """Reply rendering preserves target channel."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="10",
        )
        event = _make_event(
            payload={"body": "reply msg"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="direct", target_channel="2"
            ),
        )
        assert result.payload["reply_id"] == 10
        assert result.payload["channel_index"] == 2


# ===================================================================
# Structured reaction rendering
# ===================================================================


class TestRendererStructuredReaction:
    """Renderer reaction rendering with/without native ref."""

    async def test_reaction_with_numeric_ref_sets_reply_id_and_emoji(self) -> None:
        """Reaction with numeric native ref → reply_id + emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 55
        assert result.payload["emoji"] == 1
        assert result.payload["text"] == "👍"

    async def test_reaction_uses_key_from_relation(self) -> None:
        """Reaction text comes from relation.key when present."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="❤️",
        )
        event = _make_event(
            payload={"body": "unused"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == "❤️"

    async def test_reaction_falls_back_to_payload_key(self) -> None:
        """Reaction text falls back to payload key/body when relation.key is None."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key=None,
        )
        event = _make_event(
            payload={"key": "🎉"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == "🎉"

    async def test_reaction_without_native_ref_readable_fallback(self) -> None:
        """Reaction without native ref → readable fallback, no emoji field."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id=None,
            key="👍",
        )
        event = _make_event(
            payload={"body": "unused"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert "[reacted: 👍]" in result.payload["text"]

    async def test_reaction_preserves_channel_and_meshnet(self) -> None:
        """Reaction rendering preserves channel_index and meshnet_name."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="7",
            key="😀",
        )
        event = _make_event(
            payload={"body": "😀"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="direct", target_channel="4"
            ),
        )
        assert result.payload["channel_index"] == 4
        assert "meshnet_name" in result.payload


class TestMeshtasticRendererForeignRefs:
    """MeshtasticRenderer must not use native refs from other adapters."""

    async def test_foreign_native_ref_not_used_for_reply(self) -> None:
        """Matrix native ref must not produce reply_id when rendering to Meshtastic."""
        renderer = _make_renderer("mesh-1")
        foreign_ref = NativeRef(
            adapter="matrix-1", native_channel_id="!r", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="orig msg",
        )
        event = _make_event(payload={"body": "hello"}, relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        # Plain text only — no "[replying to: …]" prefix for non-native replies.
        assert result.payload["text"] == "hello"

    async def test_foreign_native_ref_not_used_for_reaction(self) -> None:
        """Matrix native ref must not produce reply_id + emoji when rendering reaction to Meshtastic."""
        renderer = _make_renderer("mesh-1")
        foreign_ref = NativeRef(
            adapter="matrix-1", native_channel_id="!r", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(payload={"body": "unused"}, relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert "[reacted: 👍]" in result.payload["text"]

    async def test_mmrelay_metadata_fallback_for_reply(self) -> None:
        """Relation with metadata meshtastic_reply_id renders reply_id=id even without native ref."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="orig msg",
            metadata={"meshtastic_reply_id": "42"},
        )
        event = _make_event(payload={"body": "reply text"}, relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 42

    async def test_mmrelay_metadata_fallback_for_reaction(self) -> None:
        """Relation with metadata meshtastic_reply_id renders reply_id + emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "77"},
        )
        event = _make_event(payload={"body": "🔥"}, relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 77
        assert result.payload["emoji"] == 1

    async def test_non_numeric_mmrelay_id_falls_back(self) -> None:
        """Non-numeric meshtastic_reply_id in metadata falls back to readable text."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "not-a-number"},
        )
        event = _make_event(payload={"body": "🔥"}, relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
