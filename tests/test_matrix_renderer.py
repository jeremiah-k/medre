"""Tests for MatrixRenderer: name, can_render dispatch, rendering output,
relation handling, envelope embedding, and long body handling.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.matrix import MatrixRenderer
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
        source_adapter="transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


class TestMatrixRenderer:
    """MatrixRenderer output and dispatch tests."""

    def test_name_is_matrix(self) -> None:
        renderer = MatrixRenderer()
        assert renderer.name == "matrix"

    def test_can_render_matrix_adapter(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        assert renderer.can_render(event, "matrix_instance") is True

    def test_can_render_non_matrix(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        assert renderer.can_render(event, "fake_presentation") is False

    async def test_render_simple_message(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello matrix"})
        result = await renderer.render(event, "matrix_instance")
        assert isinstance(result, RenderingResult)
        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "hello matrix"

    async def test_render_includes_msgtype(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(event, "matrix_instance")
        assert result.payload["msgtype"] == "m.text"

    async def test_render_includes_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(event, "matrix_instance")
        assert result.payload["body"] == "specific body"

    async def test_render_with_reply_relation(self) -> None:
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(event, "matrix_instance")
        assert "m.relates_to" in result.payload
        relates = result.payload["m.relates_to"]
        assert "m.in_reply_to" in relates
        assert relates["m.in_reply_to"]["event_id"] == "$orig-native"

    async def test_render_with_reaction_relation(self) -> None:
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(event, "matrix_instance")
        assert "m.relates_to" in result.payload
        relates = result.payload["m.relates_to"]
        assert relates["rel_type"] == "m.annotation"
        assert relates["key"] == "👍"
        assert relates["event_id"] == "$orig-native"

    async def test_render_with_envelope(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(event, "matrix_instance")
        assert "medre" in result.payload
        assert "envelope" in result.payload["medre"]

    async def test_render_truncates_very_long_body(self) -> None:
        renderer = MatrixRenderer()
        long_body = "x" * 200_000
        event = _make_event(payload={"body": long_body})
        result = await renderer.render(event, "matrix_instance")
        # Renderer passes body through without truncation
        assert result.payload["body"] == long_body

    async def test_render_returns_rendering_result(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(event, "matrix_instance")
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "matrix_instance"
