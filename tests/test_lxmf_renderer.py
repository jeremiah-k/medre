"""Tests for LxmfRenderer: name, can_render dispatch, rendering output,
title, fields envelope, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
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
        source_adapter="lxmf-1",
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello lxmf"},
        metadata=EventMetadata(),
    )


class TestLxmfRenderer:
    """LxmfRenderer output and dispatch tests."""

    def test_name_is_lxmf(self) -> None:
        renderer = LxmfRenderer()
        assert renderer.name == "lxmf"

    def test_can_render_lxmf_platform(self) -> None:
        """Renderer matches when target_platform is lxmf."""
        renderer = LxmfRenderer()
        event = _make_event()
        assert renderer.can_render(event, "local-rnode", target_platform="lxmf") is True

    def test_can_render_non_lxmf(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        assert renderer.can_render(event, "fake_presentation", target_platform="fake") is False

    def test_can_render_rejects_matrix(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        assert renderer.can_render(event, "matrix_instance", target_platform="matrix") is False

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = LxmfRenderer()
        event = _make_event()
        assert renderer.can_render(event, "lxmf_node") is False

    async def test_render_basic_content(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "hello lxmf"})
        result = await renderer.render(event, "lxmf_node")
        assert isinstance(result, RenderingResult)
        assert result.payload["content"] == "hello lxmf"

    async def test_render_with_title(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "body", "title": "Subject"})
        result = await renderer.render(event, "lxmf_node")
        assert result.payload["content"] == "body"
        assert result.payload["title"] == "Subject"

    async def test_render_empty_content(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": ""})
        result = await renderer.render(event, "lxmf_node")
        assert result.payload["content"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(event, "lxmf_node")
        assert "body" not in result.payload
        assert result.payload["content"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(event, "lxmf_node")
        assert result.payload["content"] == "fallback text"

    async def test_render_payload_has_content_not_text_key(self) -> None:
        """Payload uses 'content' key, not 'text'."""
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "check keys"})
        result = await renderer.render(event, "lxmf_node")
        assert "content" in result.payload
        assert "text" not in result.payload

    async def test_render_includes_destination_hash(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        assert "destination_hash" in result.payload
        assert result.payload["destination_hash"] == ""

    async def test_render_includes_fields(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        assert "fields" in result.payload
        assert isinstance(result.payload["fields"], dict)

    async def test_render_fields_envelope_embedded(self) -> None:
        renderer = LxmfRenderer(metadata_embedding=True)
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        fields = result.payload["fields"]
        from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE
        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE]
        assert LXMF_NAMESPACE in envelope
        assert envelope[LXMF_NAMESPACE]["event_id"] == "evt-1"

    async def test_render_no_envelope_when_disabled(self) -> None:
        renderer = LxmfRenderer(metadata_embedding=False)
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        fields = result.payload["fields"]
        from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE
        assert FIELD_MEDRE_ENVELOPE not in fields

    async def test_render_returns_rendering_result(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "lxmf_node"

    async def test_render_metadata_includes_renderer(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(event, "lxmf_node")
        assert result.metadata["renderer"] == "lxmf"

    async def test_render_very_long_text_no_truncation_in_tranche1(self) -> None:
        renderer = LxmfRenderer()
        long_text = "x" * 1000
        event = _make_event(payload={"body": long_text})
        result = await renderer.render(event, "lxmf_node")
        assert result.payload["content"] == long_text
        assert result.truncated is False
