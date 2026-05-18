"""Tests for MeshtasticRenderer: name, can_render dispatch, rendering output,
target channel propagation, relation rendering (reply/reaction), and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingResult


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
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
) -> EventRelation:
    native_ref = None
    if native_message_id is not None:
        native_ref = NativeRef(
            adapter="mesh-1",
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


class TestMeshtasticRenderer:
    """MeshtasticRenderer output and dispatch tests."""

    def test_name_is_meshtastic(self) -> None:
        renderer = MeshtasticRenderer()
        assert renderer.name == "meshtastic"

    def test_can_render_meshtastic_platform(self) -> None:
        """Renderer matches when target_platform is meshtastic."""
        renderer = MeshtasticRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "local-radio", target_platform="meshtastic")
            is True
        )

    def test_can_render_non_meshtastic(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "fake_presentation", target_platform="fake")
            is False
        )

    def test_can_render_rejects_matrix(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "matrix_instance", target_platform="matrix")
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = MeshtasticRenderer()
        event = _make_event()
        assert renderer.can_render(event, "meshtastic_node") is False

    async def test_render_basic_text(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event(payload={"body": "hello mesh"})
        result = await renderer.render(event, "meshtastic_node")
        assert isinstance(result, RenderingResult)
        assert result.payload["text"] == "hello mesh"
        assert result.payload["channel_index"] == 0

    async def test_render_empty_text(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event(payload={"body": ""})
        result = await renderer.render(event, "meshtastic_node")
        assert result.payload["text"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(event, "meshtastic_node")
        assert "body" not in result.payload
        assert result.payload["text"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(event, "meshtastic_node")
        assert result.payload["text"] == "fallback text"

    async def test_render_target_channel_propagation(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node", target_channel="3")
        assert result.target_channel == "3"
        assert result.payload["channel_index"] == 3

    async def test_render_default_channel_when_no_target(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node")
        assert result.payload["channel_index"] == 0

    async def test_render_non_numeric_channel_defaults_to_zero(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node", target_channel="abc")
        assert result.payload["channel_index"] == 0

    async def test_render_returns_rendering_result(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node")
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "meshtastic_node"

    async def test_render_includes_meshnet_name(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node")
        assert "meshnet_name" in result.payload
        assert result.payload["meshnet_name"] == ""

    async def test_render_metadata_includes_renderer(self) -> None:
        renderer = MeshtasticRenderer()
        event = _make_event()
        result = await renderer.render(event, "meshtastic_node")
        assert result.metadata["renderer"] == "meshtastic"

    async def test_render_very_long_text_no_truncation_in_tranche1(self) -> None:
        renderer = MeshtasticRenderer()
        long_text = "x" * 500
        event = _make_event(payload={"body": long_text})
        result = await renderer.render(event, "meshtastic_node")
        assert result.payload["text"] == long_text
        assert result.truncated is False


# ===================================================================
# _native_reply_id_from_relation
# ===================================================================


class TestNativeReplyIdFromRelation:
    """Tests for MeshtasticRenderer._native_reply_id_from_relation."""

    def test_numeric_native_message_id_returns_int(self) -> None:
        rel = _make_relation(native_message_id="42")
        assert MeshtasticRenderer._native_reply_id_from_relation(rel) == 42

    def test_large_numeric_id(self) -> None:
        rel = _make_relation(native_message_id="8589934592")
        assert MeshtasticRenderer._native_reply_id_from_relation(rel) == 8589934592

    def test_non_numeric_returns_none(self) -> None:
        rel = _make_relation(native_message_id="$event:room.xyz")
        assert MeshtasticRenderer._native_reply_id_from_relation(rel) is None

    def test_no_native_ref_returns_none(self) -> None:
        rel = _make_relation(native_message_id=None)
        assert MeshtasticRenderer._native_reply_id_from_relation(rel) is None

    def test_empty_string_returns_none(self) -> None:
        rel = _make_relation(native_message_id="")
        assert MeshtasticRenderer._native_reply_id_from_relation(rel) is None


# ===================================================================
# Structured reply rendering
# ===================================================================


class TestRendererStructuredReply:
    """Renderer reply rendering with/without native ref."""

    async def test_reply_with_numeric_native_ref_sets_reply_id(self) -> None:
        """Reply with numeric native_message_id → reply_id in payload, plain text."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id="99",
            fallback_text="original msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert result.payload["reply_id"] == 99
        assert result.payload["text"] == "my reply"
        # No fallback prefix when native ref available
        assert "[replying to:" not in result.payload["text"]
        # channel_index / meshnet_name preserved
        assert result.payload["channel_index"] == 0
        assert "meshnet_name" in result.payload

    async def test_reply_without_native_ref_uses_fallback(self) -> None:
        """Reply without numeric native ref → fallback text prefix."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id=None,
            fallback_text="original msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert "reply_id" not in result.payload
        assert "[replying to: original msg]" in result.payload["text"]
        assert "my reply" in result.payload["text"]

    async def test_reply_with_non_numeric_ref_uses_fallback(self) -> None:
        """Reply with non-numeric native_message_id → fallback text."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id="$abc:room.server",
            fallback_text="non-mesh msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert "reply_id" not in result.payload
        assert "[replying to: non-mesh msg]" in result.payload["text"]

    async def test_reply_preserves_channel_index(self) -> None:
        """Reply rendering preserves target channel."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id="10",
        )
        event = _make_event(
            payload={"body": "reply msg"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out", target_channel="2")
        assert result.payload["reply_id"] == 10
        assert result.payload["channel_index"] == 2


# ===================================================================
# Structured reaction rendering
# ===================================================================


class TestRendererStructuredReaction:
    """Renderer reaction rendering with/without native ref."""

    async def test_reaction_with_numeric_ref_sets_reply_id_and_emoji(self) -> None:
        """Reaction with numeric native ref → reply_id + emoji=1."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert result.payload["reply_id"] == 55
        assert result.payload["emoji"] == 1
        assert result.payload["text"] == "👍"

    async def test_reaction_uses_key_from_relation(self) -> None:
        """Reaction text comes from relation.key when present."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="❤️",
        )
        event = _make_event(
            payload={"body": "unused"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert result.payload["text"] == "❤️"

    async def test_reaction_falls_back_to_payload_key(self) -> None:
        """Reaction text falls back to payload key/body when relation.key is None."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key=None,
        )
        event = _make_event(
            payload={"key": "🎉"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert result.payload["text"] == "🎉"

    async def test_reaction_without_native_ref_readable_fallback(self) -> None:
        """Reaction without native ref → readable fallback, no emoji field."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id=None,
            key="👍",
        )
        event = _make_event(
            payload={"body": "unused"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out")
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert "[reacted: 👍]" in result.payload["text"]

    async def test_reaction_preserves_channel_and_meshnet(self) -> None:
        """Reaction rendering preserves channel_index and meshnet_name."""
        renderer = MeshtasticRenderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="7",
            key="😀",
        )
        event = _make_event(
            payload={"body": "😀"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-out", target_channel="4")
        assert result.payload["channel_index"] == 4
        assert "meshnet_name" in result.payload
