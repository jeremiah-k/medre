"""Tests for MatrixCodec: decode (native → canonical), metadata population,
envelope extraction, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.errors import MatrixCodecError
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events.kinds import EventKind


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
        assert event.payload["msgtype"] == "m.text"

    def test_decode_uses_matrix_origin_server_timestamp(self) -> None:
        """Backlog events must retain their native timestamp for stale filtering."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="old message")
        native.source["origin_server_ts"] = 1_700_000_000_123

        event = codec.decode(native, room_id="!room:server")

        assert event.timestamp == datetime.fromtimestamp(
            1_700_000_000.123,
            tz=timezone.utc,
        )

    def test_decode_defaults_missing_msgtype_to_text(self) -> None:
        """Malformed Matrix content should not produce schema-noisy payloads."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="hello", content={"body": "hello"})

        event = codec.decode(native, room_id="!room:server")

        assert event.payload["body"] == "hello"
        assert event.payload["msgtype"] == "m.text"

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

    # -- source_native_ref ------------------------------------------------

    def test_decode_populates_source_native_ref(self) -> None:
        """decode sets source_native_ref when event_id is non-empty."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(event_id="$evt-abc")
        event = codec.decode(native, room_id="!room:server")
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "matrix-1"
        assert event.source_native_ref.native_channel_id == "!room:server"
        assert event.source_native_ref.native_message_id == "$evt-abc"

    def test_decode_empty_event_id_no_source_native_ref(self) -> None:
        """decode leaves source_native_ref None when event_id is empty."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(event_id="")
        event = codec.decode(native, room_id="!room:server")
        assert event.source_native_ref is None

    # -- reply relation ---------------------------------------------------

    def test_decode_reply_creates_relation(self) -> None:
        """decode creates an EventRelation for Matrix reply events."""
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "a reply",
            "m.relates_to": {
                "m.in_reply_to": {"event_id": "$original-msg-001"},
            },
        }
        native = _make_native_event(
            body="a reply", event_id="$reply-001", content=content
        )
        event = codec.decode(native, room_id="!room:server")

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reply"
        assert rel.target_event_id is None
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.adapter == "matrix-1"
        assert rel.target_native_ref.native_channel_id == "!room:server"
        assert rel.target_native_ref.native_message_id == "$original-msg-001"

    def test_decode_no_reply_no_relation(self) -> None:
        """decode produces no relation when content has no m.relates_to."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="plain message")
        event = codec.decode(native, room_id="!room:server")
        assert len(event.relations) == 0

    def test_decode_malformed_reply_no_crash(self) -> None:
        """decode does not crash on malformed m.relates_to."""
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "broken reply",
            "m.relates_to": {"event_id": None},
        }
        native = _make_native_event(body="broken reply", content=content)
        event = codec.decode(native, room_id="!room:server")
        # No reply relation should be created; no crash.
        assert len(event.relations) == 0

    def test_decode_reply_preserves_source_native_ref(self) -> None:
        """decode populates both source_native_ref and reply relation."""
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "reply with ref",
            "m.relates_to": {
                "m.in_reply_to": {"event_id": "$orig-001"},
            },
        }
        native = _make_native_event(
            body="reply with ref", event_id="$reply-002", content=content
        )
        event = codec.decode(native, room_id="!room:server")

        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "$reply-002"
        assert len(event.relations) == 1
        assert event.relations[0].target_native_ref.native_message_id == "$orig-001"
