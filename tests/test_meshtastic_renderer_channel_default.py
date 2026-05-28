"""Tests for MeshtasticRenderer default_channel resolution.

Verifies that ``render()`` honours the per-adapter ``default_channel``
from ``MeshtasticConfig`` when ``target_channel`` is absent or invalid,
and that a valid numeric ``target_channel`` overrides it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers (mirroring test_meshtastic_renderer style)
# ---------------------------------------------------------------------------


def _make_renderer_multi(
    *,
    radio_a_channel: int = 0,
    radio_b_channel: int = 1,
    radio_b_max_bytes: int = 227,
    radio_b_prefix: str = "",
    radio_b_meshnet: str = "RadioB",
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with two adapter configs (radio-a, radio-b)."""
    cfg_a = MeshtasticConfig(
        adapter_id="radio-a",
        default_channel=radio_a_channel,
    )
    cfg_b = MeshtasticConfig(
        adapter_id="radio-b",
        default_channel=radio_b_channel,
        max_text_bytes=radio_b_max_bytes,
        radio_relay_prefix=radio_b_prefix,
        meshnet_name=radio_b_meshnet,
    )
    return MeshtasticRenderer(configs={"radio-a": cfg_a, "radio-b": cfg_b})


def _make_event(
    source_adapter: str = "matrix-1",
    body: str = "hello",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(),
    )


# ===================================================================
# Single-adapter default_channel
# ===================================================================


class TestSingleAdapterDefaultChannel:
    """Single-adapter renderer honours default_channel from config."""

    async def test_absent_target_channel_uses_default_channel(self) -> None:
        """When target_channel is None, channel_index == default_channel."""
        config = MeshtasticConfig(adapter_id="radio-x", default_channel=3)
        renderer = MeshtasticRenderer(configs={"radio-x": config})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-x", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 3

    async def test_absent_target_channel_default_zero(self) -> None:
        """Default default_channel (0) still works."""
        config = MeshtasticConfig(adapter_id="radio-x")
        renderer = MeshtasticRenderer(configs={"radio-x": config})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-x", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 0

    async def test_valid_numeric_target_channel_overrides(self) -> None:
        """Valid numeric target_channel overrides default_channel."""
        config = MeshtasticConfig(adapter_id="radio-x", default_channel=3)
        renderer = MeshtasticRenderer(configs={"radio-x": config})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-x", delivery_strategy="direct", target_channel="5"
            ),
        )
        assert result.payload["channel_index"] == 5

    async def test_non_numeric_target_channel_falls_back_to_default(self) -> None:
        """Non-numeric target_channel falls back to default_channel, not 0."""
        config = MeshtasticConfig(adapter_id="radio-x", default_channel=2)
        renderer = MeshtasticRenderer(configs={"radio-x": config})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-x",
                delivery_strategy="direct",
                target_channel="abc",
            ),
        )
        assert result.payload["channel_index"] == 2

    async def test_none_target_channel_non_zero_default(self) -> None:
        """target_channel=None with default_channel=7 → channel_index=7."""
        config = MeshtasticConfig(adapter_id="radio-x", default_channel=7)
        renderer = MeshtasticRenderer(configs={"radio-x": config})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-x",
                delivery_strategy="direct",
                target_channel=None,
            ),
        )
        assert result.payload["channel_index"] == 7


# ===================================================================
# Multi-adapter default_channel
# ===================================================================


class TestMultiAdapterDefaultChannel:
    """Multi-radio renderer routes to correct default_channel per adapter."""

    async def test_radio_a_default_channel_0(self) -> None:
        """Rendering to radio-a without target_channel → channel_index=0."""
        renderer = _make_renderer_multi(radio_a_channel=0, radio_b_channel=1)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-a", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 0

    async def test_radio_b_default_channel_1(self) -> None:
        """Rendering to radio-b without target_channel → channel_index=1."""
        renderer = _make_renderer_multi(radio_a_channel=0, radio_b_channel=1)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 1

    async def test_radio_b_higher_channel(self) -> None:
        """radio-b with default_channel=4 → channel_index=4."""
        renderer = _make_renderer_multi(radio_a_channel=0, radio_b_channel=4)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result.payload["channel_index"] == 4

    async def test_explicit_target_channel_overrides_radio_b(self) -> None:
        """Explicit numeric target_channel overrides radio-b's default_channel."""
        renderer = _make_renderer_multi(radio_b_channel=1)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-b", delivery_strategy="direct", target_channel="3"
            ),
        )
        assert result.payload["channel_index"] == 3

    async def test_invalid_target_channel_uses_radio_b_default(self) -> None:
        """Invalid target_channel falls back to radio-b's default_channel."""
        renderer = _make_renderer_multi(radio_b_channel=1)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-b",
                delivery_strategy="direct",
                target_channel="not-a-number",
            ),
        )
        assert result.payload["channel_index"] == 1

    async def test_radio_b_meshnet_name_preserved(self) -> None:
        """radio-b config meshnet_name is used when rendering to radio-b."""
        renderer = _make_renderer_multi(radio_b_meshnet="TestNet")
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result.payload["meshnet_name"] == "TestNet"

    async def test_radio_b_max_text_bytes_enforced(self) -> None:
        """radio-b config max_text_bytes truncates output."""
        renderer = _make_renderer_multi(radio_b_max_bytes=10)
        event = _make_event(body="A" * 50)
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result.metadata["max_text_bytes"] == 10
        assert len(result.payload["text"].encode("utf-8")) <= 10
        assert result.truncated is True

    async def test_radio_b_prefix_applied(self) -> None:
        """radio-b config radio_relay_prefix is applied."""
        renderer = _make_renderer_multi(
            radio_b_prefix="[{meshnet_name}] ",
            radio_b_meshnet="NetB",
        )
        event = _make_event(body="msg")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result.payload["text"].startswith("[NetB] ")

    async def test_different_defaults_per_adapter(self) -> None:
        """radio-a and radio-b render with different channel_index values."""
        renderer = _make_renderer_multi(radio_a_channel=0, radio_b_channel=3)
        event = _make_event()
        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-a", delivery_strategy="direct"),
        )
        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result_a.payload["channel_index"] == 0
        assert result_b.payload["channel_index"] == 3

    async def test_empty_string_target_channel_falls_back(self) -> None:
        """Empty string target_channel is non-numeric → fallback to default."""
        renderer = _make_renderer_multi(radio_b_channel=2)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-b", delivery_strategy="direct", target_channel=""
            ),
        )
        assert result.payload["channel_index"] == 2

    async def test_whitespace_target_channel_falls_back(self) -> None:
        """Whitespace-only target_channel is non-numeric → fallback to default."""
        renderer = _make_renderer_multi(radio_b_channel=2)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="radio-b",
                delivery_strategy="direct",
                target_channel="  ",
            ),
        )
        # int("  ") raises ValueError
        assert result.payload["channel_index"] == 2
