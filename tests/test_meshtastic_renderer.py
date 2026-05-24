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
