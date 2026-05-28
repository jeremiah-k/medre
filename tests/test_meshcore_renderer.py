"""Tests for MeshCoreRenderer: name, can_render dispatch, rendering output,
target channel propagation, truncation, metadata, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingContext, RenderingResult


def _make_config(
    adapter_id: str = "meshcore_node",
    *,
    max_text_bytes: int = 512,
    meshnet_name: str = "",
    default_channel: int = 0,
) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
        meshnet_name=meshnet_name,
        default_channel=default_channel,
    )


def _make_renderer(
    adapter_id: str = "meshcore_node",
    *,
    max_text_bytes: int = 512,
    meshnet_name: str = "",
) -> MeshCoreRenderer:
    cfg = _make_config(
        adapter_id, max_text_bytes=max_text_bytes, meshnet_name=meshnet_name
    )
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
                "mc-a": _make_config("mc-a", max_text_bytes=100, meshnet_name="net-a"),
                "mc-b": _make_config("mc-b", max_text_bytes=10, meshnet_name="net-b"),
            }
        )
        event = _make_event(payload={"body": "hello world"})
        result_a = await r.render(
            event, RenderingContext(target_adapter="mc-a", delivery_strategy="direct")
        )
        assert result_a.payload["meshnet_name"] == "net-a"
        assert result_a.payload["text"] == "hello world"  # 11 bytes, under 100

        result_b = await r.render(
            event, RenderingContext(target_adapter="mc-b", delivery_strategy="direct")
        )
        assert result_b.payload["meshnet_name"] == "net-b"
        assert result_b.payload["text"] == "hello worl"  # truncated to 10 bytes


# ===================================================================
# meshnet_name propagation
# ===================================================================


class TestMeshCoreRendererMeshnetName:
    """meshnet_name comes from config, not hardcoded."""

    pytestmark = pytest.mark.asyncio

    async def test_render_meshnet_name_from_config(self) -> None:
        renderer = _make_renderer(meshnet_name="testnet")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["meshnet_name"] == "testnet"

    async def test_render_meshnet_name_default_empty(self) -> None:
        renderer = _make_renderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["meshnet_name"] == ""


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
# Tranche 4: Prefix formatting and config propagation
# ===================================================================


class TestMeshCoreRendererPrefixFormatting:
    """Verify meshnet_name and channel_index propagate correctly in output."""

    pytestmark = pytest.mark.asyncio

    async def test_meshnet_name_in_output(self) -> None:
        """meshnet_name from config appears in rendered payload."""
        renderer = _make_renderer(meshnet_name="testnet-alpha")
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["meshnet_name"] == "testnet-alpha"

    async def test_empty_meshnet_name_default(self) -> None:
        """Default meshnet_name is empty string."""
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert result.payload["meshnet_name"] == ""

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

    async def test_payload_has_exactly_three_keys(self) -> None:
        """Rendered payload has text, channel_index, meshnet_name only."""
        renderer = _make_renderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="meshcore_node", delivery_strategy="direct"
            ),
        )
        assert set(result.payload.keys()) == {"text", "channel_index", "meshnet_name"}

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
