"""Tests for MeshtasticRenderer: name, can_render dispatch, rendering output,
target channel propagation, relation rendering (reply/reaction), and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingResult

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
            await renderer.render(event, "unknown-radio")


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
            renderer.can_render(event, "local-radio", target_platform="meshtastic")
            is True
        )

    def test_can_render_non_meshtastic(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(event, "fake_presentation", target_platform="fake")
            is False
        )

    def test_can_render_rejects_matrix(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert (
            renderer.can_render(event, "matrix_instance", target_platform="matrix")
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        assert renderer.can_render(event, "meshtastic_node") is False

    async def test_render_basic_text(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": "hello mesh"})
        result = await renderer.render(event, "mesh-node")
        assert isinstance(result, RenderingResult)
        assert result.payload["text"] == "hello mesh"
        assert result.payload["channel_index"] == 0

    async def test_render_empty_text(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": ""})
        result = await renderer.render(event, "mesh-node")
        assert result.payload["text"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(event, "mesh-node")
        assert "body" not in result.payload
        assert result.payload["text"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(event, "mesh-node")
        assert result.payload["text"] == "fallback text"

    async def test_render_target_channel_propagation(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node", target_channel="3")
        assert result.target_channel == "3"
        assert result.payload["channel_index"] == 3

    async def test_render_default_channel_when_no_target(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node")
        assert result.payload["channel_index"] == 0

    async def test_render_non_numeric_channel_defaults_to_zero(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node", target_channel="abc")
        assert result.payload["channel_index"] == 0

    async def test_render_returns_rendering_result(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node")
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "mesh-node"

    async def test_render_includes_meshnet_name(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node")
        assert "meshnet_name" in result.payload
        assert result.payload["meshnet_name"] == ""

    async def test_render_metadata_includes_renderer(self) -> None:
        renderer = _make_renderer("mesh-node")
        event = _make_event()
        result = await renderer.render(event, "mesh-node")
        assert result.metadata["renderer"] == "meshtastic"

    async def test_render_long_text_truncated_to_byte_budget(self) -> None:
        renderer = _make_renderer("mesh-node")
        long_text = "x" * 500
        event = _make_event(payload={"body": long_text})
        result = await renderer.render(event, "mesh-node")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1", target_channel="2")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1", target_channel="4")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
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
        result = await renderer.render(event, "mesh-1")
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload


# ===================================================================
# Helper factories for Matrix-originated events
# ===================================================================


def _make_matrix_event(
    event_id: str = "mx-evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
    source_adapter: str = "matrix-1",
    display_name: str = "Display Name",
) -> CanonicalEvent:
    """Create a CanonicalEvent simulating Matrix origin."""
    native_data: dict[str, object] = {
        "longname": display_name,
        "shortname": display_name.split()[0] if display_name else "",
        "from_id": "@user:example.com",
    }
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "👍"},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


def _make_cross_platform_relation(
    key: str = "👍",
    fallback_text: str | None = "original mesh message",
    meshtastic_reply_id: str | None = None,
    mesh_adapter: str = "mesh-1",
) -> EventRelation:
    """Create a reaction relation pointing at a Meshtastic message.

    If *meshtastic_reply_id* is given, sets both the target_native_ref
    (owned by *mesh_adapter*) and the mmrelay metadata fallback.
    """
    metadata: dict[str, object] = {}
    native_ref = None
    if meshtastic_reply_id is not None:
        native_ref = NativeRef(
            adapter=mesh_adapter,
            native_channel_id="0",
            native_message_id=meshtastic_reply_id,
        )
        metadata["meshtastic_reply_id"] = meshtastic_reply_id
    return EventRelation(
        relation_type="reaction",
        target_event_id="mesh-evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata,
    )


# ===================================================================
# Cross-platform (Matrix→Meshtastic) MMRelay descriptive reactions
# ===================================================================


class TestCrossPlatformReactionDescriptive:
    """Matrix-originated reactions render as MMRelay descriptive text."""

    async def test_descriptive_text_with_reply_id(self) -> None:
        """Matrix reaction with Meshtastic mapping → descriptive text + reply_id."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="hello from mesh",
            meshtastic_reply_id="42",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")

        # reply_id is set (mapped Meshtastic packet ID)
        assert result.payload["reply_id"] == 42
        # NO emoji=1 — descriptive, not native tapback
        assert "emoji" not in result.payload
        # Descriptive text pattern
        text = result.payload["text"]
        assert "reacted 👍 to" in text
        assert "hello from mesh" in text

    async def test_descriptive_text_without_reply_id(self) -> None:
        """Matrix reaction without Meshtastic mapping → descriptive text only."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="❤️",
            fallback_text="some original",
            meshtastic_reply_id=None,
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")

        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        text = result.payload["text"]
        assert 'reacted ❤️ to "some original"' in text

    async def test_no_emoji_field_set(self) -> None:
        """Cross-platform reactions never set emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="🔥",
            fallback_text="msg",
            meshtastic_reply_id="99",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        assert result.payload.get("emoji") is None

    async def test_compact_prefix_strips_spaces_preserves_casing(self) -> None:
        """Display name spaces are stripped in the prefix; casing preserved."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
            meshnet_name="testnet",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test msg",
            meshtastic_reply_id="10",
        )
        # "Display Name" → "DisplayName" in prefix
        event = _make_matrix_event(
            display_name="Display Name",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "[DisplayName] reacted" in text
        # NOT lowercased
        assert "[displayname]" not in text

    async def test_compact_prefix_not_lowercased(self) -> None:
        """Casing is preserved: 'MeshUser' stays 'MeshUser', not 'meshuser'."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
        )

        rel = _make_cross_platform_relation(key="👋", fallback_text="hi")
        event = _make_matrix_event(
            display_name="Mesh User",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "[MeshUser] reacted" in text

    async def test_abbreviated_preview_40_chars(self) -> None:
        """Original text preview is abbreviated to 40 chars + '...'."""
        renderer = _make_renderer("mesh-1")
        long_text = "A" * 60
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text=long_text,
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        # Should contain abbreviated text (40 chars + "...")
        assert "A" * 40 + '..."' in text
        # Should NOT contain the full 60 chars
        assert "A" * 60 not in text

    async def test_abbreviated_preview_short_text_unchanged(self) -> None:
        """Short original text is not truncated."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="short msg",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert 'reacted 👍 to "short msg"' in text

    async def test_newlines_normalised_to_spaces(self) -> None:
        """Newlines in original text are replaced with spaces."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="line one\nline two\nline three",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "\n" not in text.split('to "')[1]
        assert "line one line two line three" in text

    async def test_quoted_reply_lines_stripped(self) -> None:
        """Quoted reply lines (> ...) are stripped from preview."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="> quoted line\nactual message",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "> quoted" not in text
        assert "actual message" in text

    async def test_original_text_from_metadata_preferred(self) -> None:
        """relation.metadata['original_text'] takes priority over fallback_text."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="fallback text",
        )
        # Inject original_text into metadata
        meta = dict(rel.metadata)
        meta["original_text"] = "metadata original"
        rel2 = EventRelation(
            relation_type=rel.relation_type,
            target_event_id=rel.target_event_id,
            target_native_ref=rel.target_native_ref,
            key=rel.key,
            fallback_text=rel.fallback_text,
            metadata=meta,
        )
        event = _make_matrix_event(relations=(rel2,))
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "metadata original" in text
        assert "fallback text" not in text

    async def test_falls_back_to_payload_body(self) -> None:
        """When no fallback_text, uses event payload body/text."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text=None,
        )
        event = _make_matrix_event(
            payload={"body": "payload body text"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert "payload body text" in text

    async def test_preserves_channel_and_meshnet(self) -> None:
        """Cross-platform reaction preserves channel_index and meshnet_name."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="😀",
            fallback_text="hi",
            meshtastic_reply_id="7",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1", target_channel="4")
        assert result.payload["channel_index"] == 4
        assert "meshnet_name" in result.payload

    async def test_metadata_includes_descriptive_reaction_flag(self) -> None:
        """Result metadata has descriptive_reaction=True for cross-platform."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        assert result.metadata.get("descriptive_reaction") is True

    async def test_no_radio_relay_prefix_in_metadata_for_descriptive(self) -> None:
        """Descriptive reactions embed their own prefix; no separate prefix metadata."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        assert "radio_relay_prefix" not in result.metadata

    async def test_mmrelay_metadata_reply_id_still_works(self) -> None:
        """Cross-platform reaction with mmrelay metadata gets reply_id."""
        renderer = _make_renderer("mesh-1")
        # No native ref (meshtastic_reply_id=None in helper means no native ref)
        # but we add meshtastic_reply_id via metadata
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="👍",
            fallback_text="mesh msg",
            metadata={"meshtastic_reply_id": "88"},
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(event, "mesh-1")
        assert result.payload["reply_id"] == 88
        assert "emoji" not in result.payload


# ===================================================================
# Test D: Matrix→Meshtastic comprehensive reaction rendering
# ===================================================================


class TestMatrixToMeshtasticReactionComprehensive:
    """Test D: Matrix→Meshtastic reaction with generic display name.

    Verifies: compact prefix with space before 'reacted', casing preserved,
    display-name spaces removed, reply_id == 2728143522 when mapping exists,
    no emoji=1 field.
    """

    async def test_comprehensive_descriptive_reaction(self) -> None:
        """All Test D requirements in one test: spaces, casing, reply_id, no emoji."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
            meshnet_name="mynet",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="original mesh message text",
            meshtastic_reply_id="2728143522",
        )
        event = _make_matrix_event(
            display_name="Alpha Bravo",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        payload = result.payload
        text = payload["text"]

        # Display-name spaces removed: "Alpha Bravo" → "AlphaBravo"
        assert "[AlphaBravo]" in text
        assert "[Alpha Bravo]" not in text

        # Casing preserved (not lowercased)
        assert "[AlphaBravo]" in text  # exact casing match
        assert "[alphabravo]" not in text  # would appear if lowercased

        # Space after compact prefix before 'reacted'
        assert "AlphaBravo] reacted" in text
        assert "AlphaBravo]reacted" not in text

        # Descriptive reaction pattern
        assert 'reacted 👍 to "original mesh message text"' in text

        # reply_id == 2728143522 when mapping exists
        assert payload["reply_id"] == 2728143522

        # No emoji=1 field (descriptive, not native tapback)
        assert "emoji" not in payload

    async def test_no_prefix_space_before_reacted(self) -> None:
        """Without a prefix template, text starts with 'reacted'."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(
            display_name="Some User",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        # No prefix → text starts directly with "reacted"
        assert text.startswith("reacted 👍 to")

    async def test_compact_prefix_no_trailing_space_adds_separator(self) -> None:
        """Prefix without trailing space gets separator space before 'reacted'."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}]",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="hi",
        )
        event = _make_matrix_event(
            display_name="Test User",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        # "[TestUser]" (no trailing space) + separator " " + "reacted"
        assert "[TestUser] reacted" in text
        assert "[TestUser]reacted" not in text


# ===================================================================
# Matrix→Meshtastic no-mapping fallback
# ===================================================================


class TestMatrixToMeshtasticNoMapping:
    """Missing mapping fallback: Matrix→Meshtastic reaction with no mapping.

    Sends descriptive text with no reply_id. No crash.
    """

    async def test_no_mapping_descriptive_text_no_reply_id(self) -> None:
        """Matrix reaction with no Meshtastic mapping → descriptive text, no reply_id."""
        renderer = _make_renderer("mesh-1")
        # No native ref, no meshtastic_reply_id
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="👍",
            fallback_text="a message from Matrix",
        )
        event = _make_matrix_event(
            display_name="Generic User",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")

        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        text = result.payload["text"]
        assert "reacted 👍 to" in text
        assert "a message from Matrix" in text

    async def test_no_mapping_minimal_metadata_no_crash(self) -> None:
        """Matrix reaction with minimal metadata still renders without crash."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="evt-minimal",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "🔥"},
            metadata=EventMetadata(native=NativeMetadata(data={})),
        )
        result = await renderer.render(event, "mesh-1")
        assert "reacted" in result.payload["text"]
        assert "reply_id" not in result.payload


# ===================================================================
# Native Meshtastic reactions still work (regression guard)
# ===================================================================


class TestNativeReactionPreserved:
    """Ensure native Meshtastic tapback behavior is unchanged."""

    async def test_native_reaction_emoji_1(self) -> None:
        """Native Meshtastic reaction still sets emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="mesh-1",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        assert result.payload["text"] == "👍"

    async def test_native_reaction_no_reply_id_fallback(self) -> None:
        """Native reaction without reply_id → readable fallback."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id=None,
            key="❤️",
        )
        event = _make_event(
            payload={"body": "❤️"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        assert "emoji" not in result.payload
        assert "[reacted: ❤️]" in result.payload["text"]

    async def test_native_reaction_with_mmrelay_meta(self) -> None:
        """Native reaction with mmrelay metadata still gets emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "77"},
        )
        event = _make_event(
            payload={"body": "🔥"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        assert result.payload["reply_id"] == 77
        assert result.payload["emoji"] == 1


# ===================================================================
# Matrix display name enrichment → prefix rendering
# ===================================================================


class TestMatrixDisplayNameInPrefix:
    """Verify that Matrix display names flow through to the prefix template."""

    async def test_longname_in_prefix_from_matrix_display_name(self) -> None:
        """radio_relay_prefix {longname} uses Matrix display name."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}]: ",
        )

        event = CanonicalEvent(
            event_id="mx-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello from alice"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "Alice Wonderland",
                        "shortname": "Alice",
                        "from_id": "@alice:example.com",
                    }
                )
            ),
        )
        result = await renderer.render(event, "mesh-1")
        assert result.payload["text"].startswith("[Alice Wonderland]: ")
        assert "hello from alice" in result.payload["text"]

    async def test_prefix_uses_display_name_not_mxid(self) -> None:
        """Prefix shows display name, not raw MXID like @user:server."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="{longname}: ",
        )

        event = CanonicalEvent(
            event_id="mx-2",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hi"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "Display Name",
                        "shortname": "Displ",
                        "from_id": "@alice:example.com",
                    }
                )
            ),
        )
        result = await renderer.render(event, "mesh-1")
        assert result.payload["text"].startswith("Display Name: ")
        assert "@alice" not in result.payload["text"].split(": hi")[0]


# ===================================================================
# Byte-budget truncation tests
# ===================================================================


class TestByteBudgetTruncation:
    """UTF-8 byte-budget truncation after final rendering."""

    async def test_under_budget_ascii_unchanged(self) -> None:
        """ASCII text well under the byte budget is unchanged."""
        renderer = _make_renderer("mesh-1")
        text = "hello mesh"
        event = _make_event(payload={"body": text})
        result = await renderer.render(event, "mesh-1")
        assert result.payload["text"] == text
        assert result.truncated is False

    async def test_over_budget_ascii_truncates_after_prefix(self) -> None:
        """ASCII text over budget truncates to fit within max_text_bytes."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}]: ",
            max_text_bytes=20,
        )

        event = CanonicalEvent(
            event_id="evt-trunc",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "A" * 200},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={"longname": "Test", "shortname": "T", "from_id": "1"}
                )
            ),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert text.startswith("[Test]: ")
        assert len(text.encode("utf-8")) <= 20
        assert result.truncated is True

    async def test_utf8_characters_not_split(self) -> None:
        """Multi-byte UTF-8 characters are never split mid-sequence."""
        renderer = _make_renderer("mesh-1")
        # Each emoji is 4 bytes in UTF-8
        emojis = "😀" * 100  # 400 bytes total
        event = _make_event(payload={"body": emojis})
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        # The text should contain only complete emoji characters
        for ch in text:
            assert ch == "😀"
        assert len(text.encode("utf-8")) <= 227

    async def test_max_text_bytes_zero_renders_empty(self) -> None:
        """max_text_bytes == 0 renders empty text."""
        renderer = _make_renderer("mesh-1", max_text_bytes=0)

        event = _make_event(payload={"body": "hello world"})
        result = await renderer.render(event, "mesh-1")
        assert result.payload["text"] == ""
        assert result.truncated is True
        # Metadata reflects the zero-budget truncation.
        assert result.metadata["max_text_bytes"] == 0
        assert result.metadata["truncated"] is True
        assert result.metadata["rendered_text_bytes"] == 0
        assert result.metadata["original_text_bytes"] == 11
        assert result.metadata["rendered_length"] == 0
        assert result.metadata["original_length"] == 11

    async def test_truncation_metadata_keys(self) -> None:
        """Metadata includes byte-budget evidence keys."""
        renderer = _make_renderer("mesh-1")
        text = "A" * 500
        event = _make_event(payload={"body": text})
        result = await renderer.render(event, "mesh-1")
        meta = result.metadata
        assert "original_text_bytes" in meta
        assert "rendered_text_bytes" in meta
        assert "max_text_bytes" in meta
        assert "truncated" in meta
        assert "original_length" in meta
        assert "rendered_length" in meta
        assert meta["max_text_bytes"] == 227
        assert meta["truncated"] is True
        assert isinstance(meta["original_text_bytes"], int)
        assert isinstance(meta["rendered_text_bytes"], int)
        assert isinstance(meta["original_length"], int)
        assert isinstance(meta["rendered_length"], int)

    async def test_metadata_byte_counts_match_final_text(self) -> None:
        """rendered_text_bytes and rendered_length match the actual truncated text."""
        renderer = _make_renderer("mesh-1")
        text = "x" * 300
        event = _make_event(payload={"body": text})
        result = await renderer.render(event, "mesh-1")
        rendered_text = result.payload["text"]
        rendered_bytes = len(rendered_text.encode("utf-8"))
        assert result.metadata["rendered_text_bytes"] == rendered_bytes
        assert result.metadata["original_text_bytes"] == 300
        assert result.metadata["rendered_length"] == len(rendered_text)

    async def test_no_truncation_metadata_when_under_budget(self) -> None:
        """Under budget: truncated is False, byte counts and lengths match."""
        renderer = _make_renderer("mesh-1")
        text = "short"
        event = _make_event(payload={"body": text})
        result = await renderer.render(event, "mesh-1")
        assert result.truncated is False
        assert result.metadata["truncated"] is False
        assert result.metadata["original_text_bytes"] == 5
        assert result.metadata["rendered_text_bytes"] == 5
        assert result.metadata["original_length"] == 5
        assert result.metadata["rendered_length"] == 5


# ===================================================================
# Config-driven max_text_bytes tests
# ===================================================================


class TestMeshtasticConfigMaxTextBytes:
    """MeshtasticConfig max_text_bytes field validation."""

    def test_default_max_text_bytes_is_227(self) -> None:
        config = MeshtasticConfig(adapter_id="test")
        assert config.max_text_bytes == 227

    def test_rejects_negative_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=-1)
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_rejects_bool_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=True)  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_rejects_float_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=227.5)  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_zero_max_text_bytes_allowed(self) -> None:
        config = MeshtasticConfig(adapter_id="test", max_text_bytes=0)
        config.validate()  # should not raise


# ===================================================================
# Adapter capabilities reflect configured max_text_bytes
# ===================================================================


class TestAdapterCapabilitiesConfigured:
    """Adapter capabilities report configured max_text_bytes."""

    def test_real_adapter_default_max_text_bytes(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="caps-test")
        adapter = MeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 227

    def test_real_adapter_custom_max_text_bytes(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="caps-test", max_text_bytes=100)
        adapter = MeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 100

    def test_fake_adapter_default_max_text_bytes(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        adapter = FakeMeshtasticAdapter()
        assert adapter._capabilities.max_text_bytes == 227

    def test_fake_adapter_custom_max_text_bytes(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        config = MeshtasticConfig(adapter_id="fake-custom", max_text_bytes=50)
        adapter = FakeMeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 50


# ===================================================================
# Cross-platform descriptive reaction byte-budget
# ===================================================================


class TestDescriptiveReactionByteBudget:
    """Descriptive reaction text is truncated after final assembly."""

    async def test_descriptive_reaction_truncates(self) -> None:
        """Long descriptive reaction text is truncated to byte budget."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
            max_text_bytes=30,
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="A" * 200,
            meshtastic_reply_id="42",
        )
        event = _make_matrix_event(
            display_name="User",
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        text = result.payload["text"]
        assert len(text.encode("utf-8")) <= 30
        assert result.truncated is True
        # reply_id should still be set
        assert result.payload["reply_id"] == 42

    async def test_native_reaction_keeps_reply_id_and_emoji(self) -> None:
        """Native emoji reaction keeps reply_id/emoji while text is byte-budgeted."""
        renderer = _make_renderer("mesh-1")
        # Native reaction from same adapter
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="mesh-1",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(event, "mesh-1")
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        # The emoji text should be within byte budget
        assert len(result.payload["text"].encode("utf-8")) <= 227
        assert result.truncated is False


# ===================================================================
# Target-aware renderer tests
# ===================================================================


class TestTargetAwareMeshtasticRenderer:
    """MeshtasticRenderer resolves per-adapter config at render time."""

    async def test_two_adapters_different_byte_budgets(self) -> None:
        """Rendering to adapter A uses 100-byte budget, adapter B uses 500."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        long_text = "x" * 400
        event = _make_event(payload={"body": long_text})

        # Adapter A: 100-byte budget
        result_a = await renderer.render(event, "radio-a")
        assert len(result_a.payload["text"].encode("utf-8")) <= 100
        assert result_a.truncated is True
        assert result_a.metadata["max_text_bytes"] == 100

        # Adapter B: 500-byte budget
        result_b = await renderer.render(event, "radio-b")
        assert len(result_b.payload["text"].encode("utf-8")) <= 500
        assert result_b.truncated is False
        assert result_b.metadata["max_text_bytes"] == 500

    async def test_two_adapters_different_prefixes(self) -> None:
        """Prefix matches target adapter, not a random one."""
        config_a = MeshtasticConfig(
            adapter_id="radio-a",
            radio_relay_prefix="[A]: ",
            max_text_bytes=227,
        )
        config_b = MeshtasticConfig(
            adapter_id="radio-b",
            radio_relay_prefix="[B]: ",
            max_text_bytes=227,
        )

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "hello"})

        result_a = await renderer.render(event, "radio-a")
        assert result_a.payload["text"].startswith("[A]: ")

        result_b = await renderer.render(event, "radio-b")
        assert result_b.payload["text"].startswith("[B]: ")

    async def test_unknown_target_adapter_raises_key_error(self) -> None:
        """Unknown target_adapter raises KeyError — no fallback."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "fallback test"})
        with pytest.raises(KeyError, match="unknown-radio"):
            await renderer.render(event, "unknown-radio")

    async def test_metadata_reports_target_adapter_budget(self) -> None:
        """Metadata max_text_bytes matches the target adapter's config."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "short"})

        result_a = await renderer.render(event, "radio-a")
        assert result_a.metadata["max_text_bytes"] == 100

        result_b = await renderer.render(event, "radio-b")
        assert result_b.metadata["max_text_bytes"] == 500


# ===================================================================
# Multi-radio target-aware coverage (radio-alpha / radio-bravo)
# ===================================================================


def _make_multi_radio_renderer() -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with two distinct adapter configs."""
    return MeshtasticRenderer(
        configs={
            "radio-alpha": MeshtasticConfig(
                adapter_id="radio-alpha",
                radio_relay_prefix="[{shortname5}@alpha] ",
                meshnet_name="alpha-mesh",
                max_text_bytes=60,
            ),
            "radio-bravo": MeshtasticConfig(
                adapter_id="radio-bravo",
                radio_relay_prefix="[{shortname5}@bravo] ",
                meshnet_name="bravo-mesh",
                max_text_bytes=200,
            ),
        }
    )


class TestMultiRadioTargetAware:
    """A single MeshtasticRenderer with multiple configs renders differently
    per target_adapter for prefix, meshnet_name, byte budget, replies,
    reactions, and unknown target behavior.
    """

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _event_with_native(body: str = "hello") -> CanonicalEvent:
        """Event with native metadata for prefix template expansion."""
        return CanonicalEvent(
            event_id="evt-multi",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": body},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "TestUser",
                        "shortname": "TestU",
                        "from_id": "42",
                    }
                )
            ),
        )

    # -- distinct prefixes ---------------------------------------------

    async def test_alpha_prefix_contains_alpha(self) -> None:
        """Rendering to radio-alpha uses the alpha prefix template."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(event, "radio-alpha")
        assert result.payload["text"].startswith("[TestU@alpha] ")

    async def test_bravo_prefix_contains_bravo(self) -> None:
        """Rendering to radio-bravo uses the bravo prefix template."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(event, "radio-bravo")
        assert result.payload["text"].startswith("[TestU@bravo] ")

    async def test_same_event_different_prefixes(self) -> None:
        """Same event rendered to both adapters produces different prefixes."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result_a = await renderer.render(event, "radio-alpha")
        result_b = await renderer.render(event, "radio-bravo")
        assert result_a.payload["text"] != result_b.payload["text"]
        assert "[TestU@alpha]" in result_a.payload["text"]
        assert "[TestU@bravo]" in result_b.payload["text"]

    # -- distinct meshnet_name -----------------------------------------

    async def test_alpha_meshnet_name(self) -> None:
        """Payload meshnet_name matches alpha config."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(event, "radio-alpha")
        assert result.payload["meshnet_name"] == "alpha-mesh"

    async def test_bravo_meshnet_name(self) -> None:
        """Payload meshnet_name matches bravo config."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(event, "radio-bravo")
        assert result.payload["meshnet_name"] == "bravo-mesh"

    # -- distinct byte budgets -----------------------------------------

    async def test_alpha_truncates_long_text(self) -> None:
        """Alpha (60-byte budget) truncates a 150-char body."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("A" * 150)
        result = await renderer.render(event, "radio-alpha")
        assert result.truncated is True
        assert len(result.payload["text"].encode("utf-8")) <= 60
        assert result.metadata["max_text_bytes"] == 60

    async def test_bravo_keeps_long_text(self) -> None:
        """Bravo (200-byte budget) keeps the same 150-char body untruncated."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("A" * 150)
        result = await renderer.render(event, "radio-bravo")
        assert result.truncated is False
        assert "A" * 150 in result.payload["text"]
        assert result.metadata["max_text_bytes"] == 200

    # -- unknown target ------------------------------------------------

    async def test_unknown_target_raises_key_error(self) -> None:
        """Rendering to an unknown adapter raises KeyError listing known ones."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        with pytest.raises(KeyError, match="unknown-radio"):
            await renderer.render(event, "unknown-radio")

    # -- reply uses target adapter config ------------------------------

    async def test_reply_uses_target_prefix_and_budget(self) -> None:
        """Reply to radio-alpha uses alpha's prefix and byte budget."""
        renderer = _make_multi_radio_renderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id="99",
            fallback_text="original",
            adapter_id="radio-alpha",
        )
        event = CanonicalEvent(
            event_id="evt-reply",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "A" * 150},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "TestUser",
                        "shortname": "TestU",
                        "from_id": "42",
                    }
                )
            ),
        )
        # Alpha: 60-byte budget, should truncate
        result_a = await renderer.render(event, "radio-alpha")
        assert result_a.payload["reply_id"] == 99
        assert len(result_a.payload["text"].encode("utf-8")) <= 60
        assert result_a.truncated is True

        # Bravo: 200-byte budget, should NOT truncate (plain reply text < 200)
        result_b = await renderer.render(event, "radio-bravo")
        # No reply_id — native ref is owned by radio-alpha, not radio-bravo
        assert "reply_id" not in result_b.payload
        # But bravo's prefix and budget are used
        assert "[TestU@bravo]" in result_b.payload["text"]

    # -- native reaction uses target adapter config --------------------

    async def test_native_reaction_targets_correct_adapter(self) -> None:
        """Native reaction to radio-alpha uses alpha config for budget."""
        renderer = _make_multi_radio_renderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="radio-alpha",
        )
        event = CanonicalEvent(
            event_id="evt-react",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="radio-alpha",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "TestUser",
                        "shortname": "TestU",
                        "from_id": "42",
                    }
                )
            ),
        )
        result = await renderer.render(event, "radio-alpha")
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        assert result.payload["text"] == "👍"
        assert result.metadata["max_text_bytes"] == 60

    # -- cross-platform reaction uses target config --------------------

    async def test_cross_platform_reaction_uses_target_prefix(self) -> None:
        """Cross-platform reaction to radio-bravo uses bravo's compact prefix."""
        renderer = _make_multi_radio_renderer()
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="original msg",
            meshtastic_reply_id="42",
            mesh_adapter="radio-bravo",
        )
        event = _make_matrix_event(
            display_name="Cross User",
            relations=(rel,),
        )
        result = await renderer.render(event, "radio-bravo")
        text = result.payload["text"]
        # Compact prefix: shortname5 = "Cross" (first 5 of "Cross"), spaces stripped
        assert "[Cross@bravo]" in text
        assert "reacted 👍 to" in text
        assert result.payload["reply_id"] == 42
        assert "emoji" not in result.payload

    async def test_cross_platform_reaction_truncated_to_alpha_budget(
        self,
    ) -> None:
        """Cross-platform reaction to radio-alpha truncates to 60 bytes."""
        renderer = _make_multi_radio_renderer()
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="A" * 200,
            meshtastic_reply_id="10",
            mesh_adapter="radio-alpha",
        )
        event = _make_matrix_event(
            display_name="User",
            relations=(rel,),
        )
        result = await renderer.render(event, "radio-alpha")
        assert len(result.payload["text"].encode("utf-8")) <= 60
        assert result.truncated is True
        assert result.payload["reply_id"] == 10
