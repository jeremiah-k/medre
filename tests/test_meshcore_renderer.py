"""Tests for MeshCoreRenderer: name, can_render dispatch, rendering output,
target channel propagation, truncation, metadata, and edge cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext, RenderingResult


def _make_config(
    adapter_id: str = "meshcore_node",
    *,
    max_text_bytes: int = 512,
    default_channel: int = 0,
    meshcore_relay_prefix: str = "",
) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
        default_channel=default_channel,
        meshcore_relay_prefix=meshcore_relay_prefix,
    )


def _make_renderer(
    adapter_id: str = "meshcore_node",
    *,
    max_text_bytes: int = 512,
) -> MeshCoreRenderer:
    cfg = _make_config(adapter_id, max_text_bytes=max_text_bytes)
    return MeshCoreRenderer(configs={adapter_id: cfg})


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="meshcore-1",
        source_transport_id="abc123",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello meshcore"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Constructor validation
# ===================================================================


class TestMeshCoreRendererConstruction:
    """Constructor rejects empty configs; accepts valid mapping."""

    def test_empty_configs_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-empty configs mapping"):
            MeshCoreRenderer(configs={})

    def test_valid_configs_accepted(self) -> None:
        cfg = _make_config("mc-1")
        renderer = MeshCoreRenderer(configs={"mc-1": cfg})
        assert renderer.name == "meshcore"

    def test_multiple_configs_accepted(self) -> None:
        r = MeshCoreRenderer(
            configs={
                "mc-a": _make_config("mc-a"),
                "mc-b": _make_config("mc-b"),
            }
        )
        assert r.name == "meshcore"


# ===================================================================
# Name / identity
# ===================================================================


class TestMeshCoreRendererName:
    def test_name_is_meshcore(self) -> None:
        renderer = _make_renderer()
        assert renderer.name == "meshcore"


# ===================================================================
# can_render dispatch
# ===================================================================


class TestMeshCoreRendererCanRender:
    """can_render is target-aware: requires platform + registered adapter."""

    def test_can_render_meshcore_platform_with_registered_adapter(self) -> None:
        renderer = _make_renderer("local-radio")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="local-radio",
                    delivery_strategy="direct",
                    target_platform="meshcore",
                ),
            )
            is True
        )

    def test_can_render_rejects_unknown_adapter(self) -> None:
        """Adapter not in configs mapping → False, even with correct platform."""
        renderer = _make_renderer("local-radio")
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="unknown-adapter",
                    delivery_strategy="direct",
                    target_platform="meshcore",
                ),
            )
            is False
        )

    def test_can_render_non_meshcore(self) -> None:
        renderer = _make_renderer()
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
        renderer = _make_renderer()
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
        """Without platform info, renderer cannot match."""
        renderer = _make_renderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="meshcore_node", delivery_strategy="direct"
                ),
            )
            is False
        )


# ===================================================================
# Rendering basics
# ===================================================================


class TestMeshCoreRendererBasic:
    """Basic rendering output shape and content."""

    pytestmark = pytest.mark.asyncio

    async def test_render_basic_text(self) -> None:
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello meshcore"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert isinstance(result, RenderingResult)
        assert result.payload["text"] == "hello meshcore"
        assert result.payload["channel_index"] == 0

    async def test_render_empty_text(self) -> None:
        renderer = _make_renderer()
        event = _make_event(payload={"body": ""})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = _make_renderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert "body" not in result.payload
        assert result.payload["text"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = _make_renderer()
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "fallback text"

    async def test_render_target_channel_propagation(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="direct",
                target_channel="3",
            ),
        )
        assert result.target_channel == "3"
        assert result.payload["channel_index"] == 3

    async def test_render_default_channel_when_no_target(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        # Default config has default_channel=0
        assert result.payload["channel_index"] == 0

    async def test_render_non_numeric_channel_falls_back_to_config(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="direct",
                target_channel="abc",
            ),
        )
        # Default config has default_channel=0
        assert result.payload["channel_index"] == 0

    async def test_render_nonzero_default_channel_with_none_target(self) -> None:
        """None target_channel falls back to config.default_channel."""
        cfg = _make_config("mc-chan", default_channel=7)
        renderer = MeshCoreRenderer(configs={"mc-chan": cfg})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-chan", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 7

    async def test_render_nonzero_default_channel_with_invalid_target(self) -> None:
        """Non-numeric target_channel falls back to config.default_channel."""
        cfg = _make_config("mc-chan", default_channel=5)
        renderer = MeshCoreRenderer(configs={"mc-chan": cfg})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mc-chan",
                delivery_strategy="direct",
                target_channel="invalid",
            ),
        )
        assert result.payload["channel_index"] == 5

    async def test_render_returns_rendering_result(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "meshcore_node"


# ===================================================================
# Target config resolution
# ===================================================================


class TestMeshCoreRendererTargetResolution:
    """Missing target adapter raises KeyError."""

    pytestmark = pytest.mark.asyncio

    async def test_render_unknown_adapter_raises_key_error(self) -> None:
        renderer = _make_renderer("meshcore_node")
        event = _make_event()
        with pytest.raises(KeyError, match="unknown_adapter"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="unknown_adapter", delivery_strategy="direct"
                ),
            )

    async def test_render_selects_correct_config(self) -> None:
        """Multi-config renderer resolves correct adapter."""
        r = MeshCoreRenderer(
            configs={
                "mc-a": _make_config("mc-a", max_text_bytes=100),
                "mc-b": _make_config("mc-b", max_text_bytes=10),
            }
        )
        event = _make_event(payload={"body": "hello world"})
        result_a = await r.render(
            event, RenderingContext(target_adapter="mc-a", delivery_strategy="direct")
        )
        assert result_a.payload["text"] == "hello world"  # 11 bytes, under 100

        result_b = await r.render(
            event, RenderingContext(target_adapter="mc-b", delivery_strategy="direct")
        )
        assert result_b.payload["text"] == "hello worl"  # truncated to 10 bytes


# ===================================================================
# Metadata
# ===================================================================


class TestMeshCoreRendererMetadata:
    pytestmark = pytest.mark.asyncio
    """Metadata includes all required fields."""

    async def test_metadata_includes_renderer(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["renderer"] == "meshcore"

    async def test_metadata_includes_length_fields(self) -> None:
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["original_length"] == 5
        assert result.metadata["rendered_length"] == 5
        assert result.metadata["original_text_bytes"] == 5
        assert result.metadata["rendered_text_bytes"] == 5

    async def test_metadata_includes_max_text_bytes(self) -> None:
        renderer = _make_renderer(max_text_bytes=256)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["max_text_bytes"] == 256

    async def test_metadata_includes_truncated_flag(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["truncated"] is False

    async def test_metadata_truncated_when_text_exceeds_budget(self) -> None:
        renderer = _make_renderer(max_text_bytes=5)
        event = _make_event(payload={"body": "hello world"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["truncated"] is True
        assert result.metadata["original_length"] == 11
        assert result.metadata["original_text_bytes"] == 11

    async def test_metadata_only_primitives(self) -> None:
        """All metadata values are JSON-safe primitives."""
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        for key, value in result.metadata.items():
            assert isinstance(
                value, (str, int, bool, float, type(None))
            ), f"metadata[{key!r}] = {value!r} is not a primitive"


# ===================================================================
# UTF-8 byte-budget truncation
# ===================================================================


class TestMeshCoreRendererTruncation:
    """UTF-8-safe byte-budget truncation."""

    pytestmark = pytest.mark.asyncio

    async def test_default_512_byte_budget_no_truncation(self) -> None:
        """Default max_text_bytes=512; text under budget passes through."""
        renderer = _make_renderer()
        text = "x" * 500
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == text
        assert result.truncated is False

    async def test_default_512_byte_budget_truncates(self) -> None:
        """Text over 512 bytes is truncated."""
        renderer = _make_renderer()
        text = "x" * 600
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        rendered_text = result.payload["text"]
        assert isinstance(rendered_text, str)
        assert len(rendered_text.encode("utf-8")) <= 512
        assert result.truncated is True

    async def test_custom_byte_limit(self) -> None:
        """Custom max_text_bytes is respected."""
        renderer = _make_renderer(max_text_bytes=10)
        event = _make_event(payload={"body": "hello world"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "hello worl"
        assert result.truncated is True

    async def test_zero_byte_budget_produces_empty_text(self) -> None:
        """max_text_bytes=0 produces empty string."""
        renderer = _make_renderer(max_text_bytes=0)
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == ""
        assert result.truncated is True

    async def test_zero_byte_budget_empty_input(self) -> None:
        """max_text_bytes=0 with empty input: not truncated (0→0)."""
        renderer = _make_renderer(max_text_bytes=0)
        event = _make_event(payload={"body": ""})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == ""
        assert result.truncated is False

    async def test_utf8_multibyte_no_split(self) -> None:
        """Truncation never splits a multi-byte UTF-8 codepoint."""
        # "é" is 2 bytes in UTF-8. "aaaaaé" = 5+2 = 7 bytes.
        # Truncate to 6 bytes → should produce "aaaaa" (drop the é).
        renderer = _make_renderer(max_text_bytes=6)
        event = _make_event(payload={"body": "aaaaaé"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "aaaaa"
        rendered_text = result.payload["text"]
        assert isinstance(rendered_text, str)
        assert len(rendered_text.encode("utf-8")) == 5

    async def test_utf8_emoji_no_split(self) -> None:
        """Emoji (4-byte UTF-8) never split."""
        # "😀" is 4 bytes. "ab😀cd" = 2+4+2 = 8 bytes.
        # Truncate to 5 bytes → "ab" (drop the emoji).
        renderer = _make_renderer(max_text_bytes=5)
        event = _make_event(payload={"body": "ab😀cd"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "ab"
        rendered_text = result.payload["text"]
        assert isinstance(rendered_text, str)
        assert len(rendered_text.encode("utf-8")) == 2

    async def test_utf8_3byte_char_no_split(self) -> None:
        """3-byte UTF-8 chars (CJK) never split."""
        # "中" is 3 bytes. "aa中bb" = 2+3+2 = 7 bytes.
        # Truncate to 4 bytes → "aa" (drop the CJK char).
        renderer = _make_renderer(max_text_bytes=4)
        event = _make_event(payload={"body": "aa中bb"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "aa"

    async def test_exact_budget_not_truncated(self) -> None:
        """Text exactly at budget is not truncated."""
        renderer = _make_renderer(max_text_bytes=5)
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "hello"
        assert result.truncated is False

    async def test_truncation_metadata_byte_counts(self) -> None:
        """Metadata byte counts are accurate after truncation."""
        renderer = _make_renderer(max_text_bytes=3)
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.metadata["original_text_bytes"] == 5
        assert result.metadata["rendered_text_bytes"] == 3
        assert result.metadata["truncated"] is True

    async def test_mixed_multibyte_truncation(self) -> None:
        """Mixed ASCII + 2-byte + 3-byte + 4-byte chars truncated safely."""
        # "a" (1B) + "é" (2B) + "中" (3B) + "😀" (4B) = 10 bytes total
        # Truncate to 5 bytes → "a" (1B) + "é" (2B) = 3 bytes (drop 中, 😀)
        renderer = _make_renderer(max_text_bytes=5)
        event = _make_event(payload={"body": "aé中😀"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        rendered = str(result.payload["text"])
        assert rendered.encode("utf-8") == b"a\xc3\xa9"  # "aé"
        assert len(rendered.encode("utf-8")) <= 5
        assert result.truncated is True

    async def test_all_multibyte_string_budget_too_small(self) -> None:
        """Budget of 1 byte with 4-byte emoji produces empty string."""
        renderer = _make_renderer(max_text_bytes=1)
        event = _make_event(payload={"body": "😀hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == ""
        assert result.truncated is True

    async def test_single_multibyte_fits_exactly(self) -> None:
        """A single 3-byte CJK char fits exactly in a 3-byte budget."""
        renderer = _make_renderer(max_text_bytes=3)
        event = _make_event(payload={"body": "中"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "中"
        assert result.truncated is False

    async def test_truncation_preserves_ascii_prefix(self) -> None:
        """ASCII text before the truncation boundary is preserved exactly."""
        renderer = _make_renderer(max_text_bytes=10)
        event = _make_event(payload={"body": "1234567890X"})  # 11 bytes
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "1234567890"
        assert result.truncated is True

    async def test_large_text_truncated_to_small_budget(self) -> None:
        """1000-char ASCII text truncated to 50-byte budget."""
        renderer = _make_renderer(max_text_bytes=50)
        event = _make_event(payload={"body": "A" * 1000})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == "A" * 50
        assert len(str(result.payload["text"]).encode("utf-8")) == 50
        assert result.truncated is True
        assert result.metadata["original_text_bytes"] == 1000
        assert result.metadata["rendered_text_bytes"] == 50


# ===================================================================
# Prefix formatting and config propagation
# ===================================================================


class TestMeshCoreRendererPrefixFormatting:
    """Verify prefix formatting and channel_index propagate correctly in output."""

    pytestmark = pytest.mark.asyncio

    async def test_channel_index_from_target_channel(self) -> None:
        """target_channel is parsed as channel_index in payload."""
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="direct",
                target_channel="7",
            ),
        )
        assert result.payload["channel_index"] == 7

    async def test_default_channel_used_when_no_target(self) -> None:
        """default_channel from config used when no target_channel provided."""
        cfg = _make_config("mc-ch", default_channel=3)
        renderer = MeshCoreRenderer(configs={"mc-ch": cfg})
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mc-ch", delivery_strategy="direct")
        )
        assert result.payload["channel_index"] == 3

    async def test_payload_has_exactly_two_keys(self) -> None:
        """Rendered payload has text and channel_index only."""
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert set(result.payload.keys()) == {"text", "channel_index"}

    async def test_max_text_bytes_zero_with_multibyte(self) -> None:
        """max_text_bytes=0 produces empty text even with multibyte input."""
        renderer = _make_renderer(max_text_bytes=0)
        event = _make_event(payload={"body": "中é😀abc"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["text"] == ""
        assert result.truncated is True
        assert result.metadata["original_text_bytes"] == len("中é😀abc".encode("utf-8"))
        assert result.metadata["rendered_text_bytes"] == 0


# ===================================================================
# Reaction emoji fallback resolution in _degrade_relations_inline
# ===================================================================


class TestMeshCoreRendererReactionEmojiFallback:
    """Verify the reaction emoji resolution chain:
    rel.key → payload["key"] → payload["emoji"] → "∟".
    """

    pytestmark = pytest.mark.asyncio

    def _make_event_with_reaction(
        self,
        *,
        rel_key: str | None,
        payload: dict | None = None,
        target_event_id: str = "evt-target",
    ) -> CanonicalEvent:
        """Build an event with a single reaction relation."""
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=target_event_id,
            target_native_ref=None,
            key=rel_key,
            fallback_text=None,
        )
        return CanonicalEvent(
            event_id="evt-react",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload=payload or {"body": "reacted"},
            metadata=EventMetadata(),
        )

    async def test_reaction_emoji_from_rel_key(self) -> None:
        """rel.key is the first choice for the emoji."""
        renderer = _make_renderer()
        event = self._make_event_with_reaction(
            rel_key="👍",
            payload={"body": "reacted", "key": "💛", "emoji": "❤️"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        assert "[reaction 👍 to:" in result.payload["text"]

    async def test_reaction_emoji_from_payload_key(self) -> None:
        """When rel.key is None, payload['key'] is used."""
        renderer = _make_renderer()
        event = self._make_event_with_reaction(
            rel_key=None,
            payload={"body": "reacted", "key": "💛"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        assert "[reaction 💛 to:" in result.payload["text"]

    async def test_reaction_emoji_from_payload_emoji(self) -> None:
        """When rel.key and payload['key'] are absent, payload['emoji'] is used."""
        renderer = _make_renderer()
        event = self._make_event_with_reaction(
            rel_key=None,
            payload={"body": "reacted", "emoji": "❤️"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        assert "[reaction ❤️ to:" in result.payload["text"]

    async def test_reaction_emoji_hardcoded_fallback(self) -> None:
        """When nothing provides an emoji, hardcoded '∟' is used."""
        renderer = _make_renderer()
        event = self._make_event_with_reaction(
            rel_key=None,
            payload={"body": "reacted"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        assert "[reaction ∟ to:" in result.payload["text"]

    async def test_reaction_emoji_empty_rel_key_falls_through(self) -> None:
        """Empty string rel.key is falsy and falls through to payload."""
        renderer = _make_renderer()
        event = self._make_event_with_reaction(
            rel_key="",
            payload={"body": "reacted", "key": "🔥"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        assert "[reaction 🔥 to:" in result.payload["text"]


# ===================================================================
# Native-target selection rules: fallback-only, all relations iterated
# ===================================================================


class TestMeshCoreTargetSelectionRules:
    """Lock the target-selection contracts for MeshCoreRenderer.

    These tests assert the *current* behaviour — guards against accidental
    changes, not aspirational specifications.

    Key contracts:
    - MeshCoreRenderer has **no** native relation support.  No reply_id,
      emoji, or any transport-specific relation field is ever emitted.
    - All relations are degraded to inline text via
      ``degrade_relations_inline`` (iterates all relations, not just
      ``relations[0]``).
    - Both ``direct`` and ``fallback_text`` strategies produce the same
      relation-free payload for MeshCore (no native rendering path).
    - The payload always contains exactly ``text``, ``channel_index``,
      ``text`` and ``channel_index`` — never ``reply_id``, ``emoji``, or
      ``m.relates_to``.
    """

    async def test_all_relations_degraded_inline(self) -> None:
        """All relations appear in degraded inline text, not just relations[0].

        MeshCore's ``degrade_relations_inline`` iterates every relation,
        unlike Matrix/Meshtastic which only inspect ``relations[0]``.
        """
        renderer = _make_renderer()
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-reply-target",
            target_native_ref=None,
            key=None,
            fallback_text="original msg",
        )
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-react-target",
            target_native_ref=None,
            key="👍",
            fallback_text="reacted msg",
        )
        event = CanonicalEvent(
            event_id="evt-multi-rel",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(reply_rel, reaction_rel),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="fallback_text",
            ),
        )
        text = result.payload["text"]
        # Both relations must appear in degraded inline text
        assert "[reply to:" in text
        assert "[reaction 👍 to:" in text

    async def test_no_native_target_fields_ever_emitted(self) -> None:
        """MeshCore payload never contains native relation fields.

        Regardless of relation type or native ref presence, the payload
        contains only text and channel_index.
        """
        renderer = _make_renderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-001",
            target_native_ref=None,
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-no-native",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="direct",
            ),
        )
        payload_keys = set(result.payload.keys())
        # Only these keys are ever emitted
        assert payload_keys == {"text", "channel_index"}
        # Explicitly no native relation fields
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert "m.relates_to" not in result.payload

    async def test_native_ref_on_relation_is_ignored_for_direct(self) -> None:
        """Even when a native ref is present on the relation, MeshCore
        does not use it — no native rendering path exists."""
        from medre.core.events import NativeRef

        renderer = _make_renderer()
        native_ref = NativeRef(
            adapter="meshcore_node",
            native_channel_id="0",
            native_message_id="pkt-123",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-001",
            target_native_ref=native_ref,
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-has-ref",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply text"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node",
                delivery_strategy="direct",
            ),
        )
        # No native relation fields — MeshCore ignores the native ref
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        # Just plain text (no degraded inline in direct mode)
        assert result.payload["text"] == "reply text"


# ===================================================================
# Relay prefix: outbound prefix formatting and truncation
# ===================================================================


def _make_event_with_native(
    source_adapter: str = "matrix-bridge",
    *,
    payload: dict | None = None,
    native_data: dict[str, object] | None = None,
    source_channel_id: str = "!room:matrix.org",
) -> CanonicalEvent:
    """Build an event with optional native metadata for attribution extraction."""
    native = NativeMetadata(data=native_data) if native_data else None
    return CanonicalEvent(
        event_id="evt-prefix-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-abc",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello world"},
        metadata=EventMetadata(native=native),
    )


class TestMeshCoreRendererRelayPrefix:
    """Relay prefix: prepended before truncation, counts toward budget.

    These tests verify that:
    - A non-empty ``meshcore_relay_prefix`` template triggers prefix
      formatting via the shared attribution formatter.
    - The rendered prefix is prepended to the body text before
      byte-budget truncation, so the prefix counts toward
      ``max_text_bytes``.
    - Metadata records the template, rendered prefix, and formatting
      errors when applicable.
    - Default empty prefix preserves existing plain-text behaviour.
    """

    pytestmark = pytest.mark.asyncio

    async def test_matrix_prefix_with_display_name(self) -> None:
        """Matrix source: prefix template resolves {sender}."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{sender}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"sender": "@alice:matrix.org", "displayname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.payload["text"] == "[Alice] hello world"
        assert result.metadata["relay_prefix_rendered"] == "[Alice] "
        assert result.metadata["relay_prefix_template"] == "[{sender}] "

    async def test_meshtastic_prefix_with_sender_short(self) -> None:
        """Meshtastic source: prefix template resolves {sender_short}."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="<{sender_short}> ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "Base Station",
                "shortname": "BS",
                "from_id": "!aabbcc",
            },
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.payload["text"] == "<BS> hello world"
        assert result.metadata["relay_prefix_rendered"] == "<BS> "

    async def test_missing_vars_produce_empty_not_none(self) -> None:
        """Missing attribution variables produce empty strings, not 'None'."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{sender}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        # Event with no native data — display_name will be empty.
        event = _make_event_with_native(
            source_adapter="unknown-source",
            native_data=None,
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert "None" not in str(result.payload["text"])
        assert result.payload["text"] == "[] hello world"
        assert result.metadata["relay_prefix_rendered"] == "[] "

    async def test_prefix_counts_toward_max_text_bytes(self) -> None:
        """Prefix bytes count toward max_text_bytes — body is truncated."""
        # Prefix "[Alice] " = 8 bytes, budget = 15 bytes.
        # "hello world" = 11 bytes. Total = 19 > 15, so truncation occurs.
        cfg = _make_config(
            "mc-relay",
            max_text_bytes=15,
            meshcore_relay_prefix="[{sender}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"sender": "@alice:matrix.org", "displayname": "Alice"},
            payload={"body": "hello world"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        text = str(result.payload["text"])
        assert text.startswith("[Alice] ")
        # 15 bytes total: "[Alice] " (8) + "hello wo" (8) -> only 15 bytes
        assert len(text.encode("utf-8")) <= 15
        assert result.truncated is True
        assert result.metadata["truncated"] is True
        assert result.metadata["original_text_bytes"] == len(
            "[Alice] hello world".encode("utf-8")
        )

    async def test_metadata_includes_prefix_fields(self) -> None:
        """Metadata records template and rendered prefix when configured."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{source_platform}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(source_adapter="matrix-bridge")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.metadata["relay_prefix_template"] == "[{source_platform}] "
        assert result.metadata["relay_prefix_rendered"] == "[matrix] "
        assert result.metadata["relay_prefix_formatting_error"] is None

    async def test_metadata_records_formatting_error_for_unknown_placeholder(
        self,
    ) -> None:
        """Unknown placeholder produces formatting_error in metadata."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{bogus_var}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(source_adapter="matrix-bridge")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.metadata["relay_prefix_formatting_error"] is not None
        assert "unknown placeholder" in str(
            result.metadata["relay_prefix_formatting_error"]
        )

    async def test_default_empty_prefix_no_metadata_keys(self) -> None:
        """Default empty prefix: no prefix metadata keys in output."""
        cfg = _make_config("mc-relay")
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(source_adapter="matrix-bridge")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert "relay_prefix_template" not in result.metadata
        assert "relay_prefix_rendered" not in result.metadata
        assert "relay_prefix_formatting_error" not in result.metadata
        assert result.payload["text"] == "hello world"

    async def test_default_empty_prefix_preserves_current_behavior(self) -> None:
        """Empty prefix preserves plain text output unchanged."""
        cfg = _make_config("mc-relay", max_text_bytes=512)
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="meshcore-1",
            payload={"body": "plain message"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.payload["text"] == "plain message"
        assert result.truncated is False

    async def test_prefix_with_zero_budget_produces_empty(self) -> None:
        """Non-empty prefix with zero budget produces empty text."""
        cfg = _make_config(
            "mc-relay",
            max_text_bytes=0,
            meshcore_relay_prefix="[{sender}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"sender": "@alice:matrix.org", "displayname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.payload["text"] == ""
        assert result.truncated is True
        assert result.metadata["relay_prefix_rendered"] == "[Alice] "

    async def test_prefix_exact_budget_fits(self) -> None:
        """Prefix + body exactly at budget: not truncated."""
        # "[A] " = 4 bytes, "hi" = 2 bytes, total = 6 bytes.
        cfg = _make_config(
            "mc-relay",
            max_text_bytes=6,
            meshcore_relay_prefix="[{sender}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"sender": "@a:matrix.org", "displayname": "A"},
            payload={"body": "hi"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert result.payload["text"] == "[A] hi"
        assert result.truncated is False


# ===================================================================
# Source origin_label from source_attribution registry
# ===================================================================


@dataclass(slots=True)
class _StubSourceAttribution:
    """Minimal duck-typed SourceAttributionConfig for tests."""

    adapter_id: str = ""
    origin_label: str = ""


class TestMeshCoreSourceOriginLabel:
    """MeshCore target prefix uses source origin_label from registry."""

    pytestmark = pytest.mark.asyncio

    async def test_source_origin_label_in_prefix(self) -> None:
        """MeshCore target prefix uses source origin_label from registry."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{origin_label}]: ",
        )
        renderer = MeshCoreRenderer(
            configs={"mc-relay": cfg},
            source_attribution={
                "meshcore-1": _StubSourceAttribution(
                    adapter_id="meshcore-1",
                    origin_label="Remote Node",
                ),
            },
        )
        event = _make_event_with_native(
            source_adapter="meshcore-1",
            native_data={"meshcore.pubkey_prefix": "a1b2c3"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert "[Remote Node]: " in result.payload["text"]

    async def test_lxmf_to_meshcore_origin_label(self) -> None:
        """LXMF→MeshCore: source origin_label appears in prefix."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{origin_label}] ",
        )
        renderer = MeshCoreRenderer(
            configs={"mc-relay": cfg},
            source_attribution={
                "lxmf-1": _StubSourceAttribution(
                    adapter_id="lxmf-1",
                    origin_label="LXMF Relay",
                ),
            },
        )
        event = _make_event_with_native(
            source_adapter="lxmf-1",
            native_data={"source_hash": "feedface"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert "[LXMF Relay] " in result.payload["text"]

    async def test_no_registry_entry_uses_empty_origin_label(self) -> None:
        """Missing source_attribution entry: origin_label is empty, not 'None'."""
        cfg = _make_config(
            "mc-relay",
            meshcore_relay_prefix="[{origin_label}] ",
        )
        renderer = MeshCoreRenderer(configs={"mc-relay": cfg})
        event = _make_event_with_native(
            source_adapter="unknown-source",
            native_data=None,
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-relay", delivery_strategy="direct"),
        )
        assert "None" not in result.payload["text"]
        assert "[] " in result.payload["text"]
