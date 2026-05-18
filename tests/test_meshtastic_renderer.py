"""Tests for MeshtasticRenderer: name, can_render dispatch, rendering output,
target channel propagation, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingResult


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
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
        relations=(),
        payload=payload or {"body": "hello mesh"},
        metadata=EventMetadata(),
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
