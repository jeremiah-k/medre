"""Tests for Matrix formatted_body support: outbound HTML rendering and
inbound formatted_body extraction.

Covers:
- Outbound: all messages include ``format`` and ``formatted_body``
- Outbound: HTML escaping and newline conversion in formatted_body
- Outbound: fallback-text strategy includes formatted_body
- Outbound: reaction emote fallback updates formatted_body
- Outbound: true m.reaction clears formatted_body
- Inbound: formatted_body extracted from Matrix event content into native_data
- Inbound: events without formatted_body do not pollute native_data
- Inbound: format field also extracted when present
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Inbound helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MatrixConfig:
    defaults = dict(
        adapter_id="matrix-1",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_native_event(
    body: str = "hello",
    sender: str = "@alice:example.com",
    event_id: str = "$evt-001",
    content: dict | None = None,
) -> Any:
    class _Fake:
        pass

    evt = _Fake()
    evt.body = body
    evt.sender = sender
    evt.event_id = event_id
    evt.source = {
        "content": content or {"msgtype": "m.text", "body": body},
        "event_id": event_id,
        "sender": sender,
        "type": "m.room.message",
    }
    return evt


# ===================================================================
# Outbound formatted body tests
# ===================================================================


class TestOutboundFormattedBody:
    """Verify outbound Matrix messages include formatted_body."""

    async def test_plain_text_message_has_formatted_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello matrix"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert result.payload["format"] == "org.matrix.custom.html"
        fb = result.payload["formatted_body"]
        assert "<p>" in fb
        assert "</p>" in fb
        assert "hello matrix" in fb

    async def test_formatted_body_escapes_html(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "<b>bold</b>"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        fb = result.payload["formatted_body"]
        assert "&lt;b&gt;bold&lt;/b&gt;" in fb
        # Raw HTML must NOT appear
        assert "<b>" not in fb.replace("<p>", "").replace("</p>", "").replace(
            "<br/>", ""
        )

    async def test_formatted_body_converts_newlines(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "line1\nline2"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        fb = result.payload["formatted_body"]
        assert "line1<br/>line2" in fb
        # Raw newline should not be present inside <p>
        assert "\n" not in fb.replace("<br/>", "")

    async def test_fallback_text_has_formatted_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "fallback msg"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance",
                delivery_strategy="fallback_text",
            ),
        )
        assert result.fallback_applied == "strategy_fallback_text"
        assert result.payload["format"] == "org.matrix.custom.html"
        fb = result.payload["formatted_body"]
        assert "<p>" in fb
        assert "fallback msg" in fb

    async def test_reaction_emote_fallback_has_formatted_body(self) -> None:
        """Emote fallback for reactions updates formatted_body to match."""
        renderer = MatrixRenderer()
        # Use a foreign adapter ref so it falls back to m.emote
        foreign_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert result.payload["msgtype"] == "m.emote"
        assert "format" in result.payload
        assert "formatted_body" in result.payload
        fb = result.payload["formatted_body"]
        assert "<p>" in fb
        # The emote body contains the reaction text
        assert "reacted" in fb

    async def test_true_m_reaction_clears_formatted_body(self) -> None:
        """True m.reaction events must not carry format or formatted_body."""
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
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert "format" not in result.payload
        assert "formatted_body" not in result.payload
        assert "msgtype" not in result.payload
        assert "body" not in result.payload


# ===================================================================
# Inbound formatted body tests
# ===================================================================


class TestInboundFormattedBody:
    """Verify inbound Matrix events have formatted_body extracted into native_data."""

    def test_inbound_formatted_body_extracted(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "hello",
            "format": "org.matrix.custom.html",
            "formatted_body": "<p>hello</p>",
        }
        native = _make_native_event(body="hello", content=content)
        event = codec.decode(native, room_id="!room:server")

        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert data["formatted_body"] == "<p>hello</p>"

    def test_inbound_no_formatted_body(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {"msgtype": "m.text", "body": "hello"}
        native = _make_native_event(body="hello", content=content)
        event = codec.decode(native, room_id="!room:server")

        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert "formatted_body" not in data

    def test_inbound_format_field_extracted(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "hello",
            "format": "org.matrix.custom.html",
            "formatted_body": "<p>hello</p>",
        }
        native = _make_native_event(body="hello", content=content)
        event = codec.decode(native, room_id="!room:server")

        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert data["format"] == "org.matrix.custom.html"
        assert data["formatted_body"] == "<p>hello</p>"
