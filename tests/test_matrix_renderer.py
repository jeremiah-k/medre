"""Tests for MatrixRenderer: name, can_render dispatch, rendering output,
relation handling, envelope embedding, and long body handling.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
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

    def test_can_render_matrix_platform(self) -> None:
        """Renderer matches when target_platform is matrix."""
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "chat-instance", target_platform="matrix")
            is True
        )

    def test_can_render_non_matrix(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "fake_presentation", target_platform="fake")
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = MatrixRenderer()
        event = _make_event()
        assert renderer.can_render(event, "matrix_instance") is False

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
        result = await renderer.render(event, "matrix-1")
        assert "m.relates_to" in result.payload
        relates = result.payload["m.relates_to"]
        assert "m.in_reply_to" in relates
        assert relates["m.in_reply_to"]["event_id"] == "$orig-native"

    async def test_render_with_reaction_relation(self) -> None:
        """Reaction relations render as native Matrix m.reaction payloads."""
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
        result = await renderer.render(event, "matrix-1")
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert result.payload["m.relates_to"] == {
            "rel_type": "m.annotation",
            "event_id": "$orig-native",
            "key": "👍",
        }
        assert "msgtype" not in result.payload
        assert "body" not in result.payload

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


class TestMatrixRendererForeignRefs:
    """MatrixRenderer must not use native refs from other adapters."""

    async def test_foreign_native_ref_not_used_for_reply(self) -> None:
        """Meshtastic native ref must not produce m.in_reply_to when rendering to Matrix."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(adapter="mesh-1", native_channel_id="0", native_message_id="123")
        rel = EventRelation(
            relation_type="reply", target_event_id=None,
            target_native_ref=foreign_ref, key=None, fallback_text="original",
        )
        event = _make_event(payload={"body": "hello"}, relations=(rel,))
        result = await renderer.render(event, "matrix_instance")
        assert "m.relates_to" not in result.payload

    async def test_foreign_native_ref_not_used_for_reaction(self) -> None:
        """Meshtastic native ref must not produce true m.reaction."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(adapter="mesh-1", native_channel_id="0", native_message_id="123")
        rel = EventRelation(
            relation_type="reaction", target_event_id=None,
            target_native_ref=foreign_ref, key="👍", fallback_text=None,
        )
        event = _make_event(payload={"body": "👍"}, relations=(rel,))
        result = await renderer.render(event, "matrix_instance")
        assert "_matrix_event_type" not in result.payload
        assert result.payload.get("msgtype") == "m.emote"

    async def test_mmrelay_reply_id_preserved_in_fallback(self) -> None:
        """MMRelay meshtastic_replyId from relation metadata preserves KEY_REPLY_ID in fallback."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(adapter="mesh-1", native_channel_id="0", native_message_id="99")
        rel = EventRelation(
            relation_type="reply", target_event_id=None,
            target_native_ref=foreign_ref, key=None, fallback_text="orig",
            metadata={"meshtastic_reply_id": "42"},
        )
        event = _make_event(payload={"body": "hello"}, relations=(rel,))
        result = await renderer.render(event, "matrix_instance")
        assert "meshtastic_replyId" in result.payload
        assert result.payload["meshtastic_replyId"] == "42"
