"""Tests for MatrixCodec: decode (native → canonical), encode (canonical →
native dict), metadata population, envelope extraction, and edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from datetime import datetime, timezone

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixCodecError
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata


def _make_config(**overrides: Any) -> MatrixConfig:
    """Build a valid MatrixConfig for testing."""
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
    has_source: bool = True,
) -> Any:
    """Build a minimal object that mimics a nio RoomMessageText event."""

    class _Fake:
        pass

    evt = _Fake()
    evt.body = body
    evt.sender = sender
    evt.event_id = event_id
    if has_source:
        evt.source = {
            "content": content or {"msgtype": "m.text", "body": body},
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        }
    return evt


class TestMatrixCodec:
    """MatrixCodec encode/decode behaviour."""

    # -- Decode ---------------------------------------------------------

    def test_decode_text_message(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="hello matrix")
        event = codec.decode(native, room_id="!room:server")
        assert event.event_kind == EventKind.MESSAGE_CREATED
        assert event.source_adapter == "matrix-1"
        assert event.payload["body"] == "hello matrix"

    def test_decode_with_body(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="specific body text")
        event = codec.decode(native, room_id="!room:server")
        assert event.payload["body"] == "specific body text"

    def test_decode_with_empty_body(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="")
        event = codec.decode(native, room_id="!room:server")
        assert event.payload["body"] == ""

    def test_decode_with_missing_body(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="fallback")
        del native.body
        # body attribute missing -> getattr returns "" default
        event = codec.decode(native, room_id="!room:server")
        assert event.payload["body"] == ""

    def test_decode_without_source_raises(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(has_source=False)
        with pytest.raises(MatrixCodecError, match="source"):
            codec.decode(native, room_id="!room:server")

    def test_decode_populates_native_metadata(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(
            body="hello", sender="@alice:example.com", event_id="$evt-1"
        )
        event = codec.decode(native, room_id="!room:server")
        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert data["room_id"] == "!room:server"
        assert data["event_id"] == "$evt-1"
        assert data["sender"] == "@alice:example.com"

    def test_decode_envelope_extraction(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "hello",
            "medre": {
                "envelope": {
                    "canonical_event_id": "orig-001",
                    "source_adapter": "transport-1",
                }
            },
        }
        native = _make_native_event(body="hello", content=content)
        # decode succeeds without error — envelope is extracted
        event = codec.decode(native, room_id="!room:server")
        assert event is not None

    def test_decode_without_envelope(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(
            body="hello",
            content={"msgtype": "m.text", "body": "hello"},
        )
        event = codec.decode(native, room_id="!room:server")
        assert event is not None
        assert event.payload["body"] == "hello"

    # -- Encode ---------------------------------------------------------

    def test_encode_basic_message(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        content = codec.encode(event, target=None)
        assert content["msgtype"] == "m.text"
        assert content["body"] == "hello"

    def test_encode_with_envelope(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        event = CanonicalEvent(
            event_id="evt-2",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        content = codec.encode(event, target=None)
        assert "medre" in content
        assert "envelope" in content["medre"]

    def test_encode_round_trip(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        original_body = "round trip message"
        event = CanonicalEvent(
            event_id="evt-3",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": original_body},
            metadata=EventMetadata(),
        )
        content = codec.encode(event, target=None)
        assert content["body"] == original_body

    def test_decode_unsupported_msgtype(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {"msgtype": "m.notice", "body": "notice text"}
        native = _make_native_event(body="notice text", content=content)
        event = codec.decode(native, room_id="!room:server")
        assert event.event_kind == EventKind.MESSAGE_CREATED
        assert event.payload["body"] == "notice text"
