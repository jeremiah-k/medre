"""Tests for LxmfCodec: decode (native → canonical), metadata
population, source_native_ref, title extraction, fields envelope, and
edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.errors import LxmfCodecError
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind


def _make_config() -> LxmfConfig:
    return LxmfConfig(adapter_id="lxmf-1")


def _make_text_packet(
    content: str = "hello lxmf",
    source_hash: str = "ab" * 16,
    msg_id: str = "cd" * 32,
    title: str = "",
    fields: dict | None = None,
) -> dict:
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": title,
        "content": content,
        "fields": fields or {},
        "signature_validated": True,
        "has_fields": fields is not None and len(fields) > 0,
    }


class TestLxmfCodecDecode:
    """LxmfCodec decode behaviour."""

    def test_decode_text_message(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(content="hello lxmf")
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.event_kind == EventKind.MESSAGE_CREATED
        assert event.payload["body"] == "hello lxmf"

    def test_decode_sets_portnum_lxmf(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.payload["portnum"] == "lxmf"

    def test_decode_with_title(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(title="Subject Line")
        event = codec.decode(packet)
        assert event.payload["title"] == "Subject Line"

    def test_decode_without_title(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(title="")
        event = codec.decode(packet)
        assert "title" not in event.payload

    def test_decode_sets_source_adapter(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.source_adapter == "lxmf-1"

    def test_decode_sets_source_transport_id(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(source_hash="ef" * 16)
        event = codec.decode(packet)
        assert event.source_transport_id == "ef" * 16

    def test_decode_channel_id_is_none(self) -> None:
        """LXMF has no channel concept — source_channel_id is always None."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.source_channel_id is None


class TestLxmfCodecSourceNativeRef:
    """source_native_ref population from message_id."""

    def test_decode_populates_source_native_ref(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(msg_id="aa" * 32)
        event = codec.decode(packet)
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "lxmf-1"
        assert event.source_native_ref.native_channel_id is None
        assert event.source_native_ref.native_message_id == "aa" * 32

    def test_decode_missing_message_id_no_ref(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["message_id"]
        event = codec.decode(packet)
        assert event.source_native_ref is None


class TestLxmfCodecMetadata:
    """Native metadata population."""

    def test_decode_populates_native_metadata(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(source_hash="ab" * 16, msg_id="cd" * 32)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert data["source_hash"] == "ab" * 16
        assert data["message_id"] == "cd" * 32

    def test_decode_metadata_has_timestamp(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["timestamp"] == 1700000000.0

    def test_decode_metadata_has_title(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(title="Test Title")
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["title"] == "Test Title"

    def test_decode_metadata_has_destination_hash(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["destination_hash"] == "00" * 16

    def test_decode_metadata_destination_hash_bytes(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        packet["destination_hash"] = bytes.fromhex("11" * 16)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["destination_hash"] == "11" * 16


class TestLxmfCodecFieldsEnvelope:
    """MEDRE envelope extraction from fields."""

    def test_decode_extracts_medre_envelope(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {"schema_version": 1, "event_id": "evt-123"}
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        assert event.metadata.custom is not None
        assert "medre_envelope" in event.metadata.custom
        assert event.metadata.custom["medre_envelope"]["event_id"] == "evt-123"

    def test_decode_no_envelope_no_custom(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(fields={})
        event = codec.decode(packet)
        assert "medre_envelope" not in event.metadata.custom


class TestLxmfCodecNoReplyRelations:
    """LXMF has no native reply relations."""

    def test_decode_no_reply_relations(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert len(event.relations) == 0


class TestLxmfCodecErrors:
    """Error cases."""

    def test_decode_non_dict_raises(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet: Any = "not a dict"
        with pytest.raises(LxmfCodecError, match="dict"):
            codec.decode(packet)

    def test_decode_unsupported_category_raises(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = {
            "source_hash": "ab" * 16,
            "fields": {0x05: [{"name": "file.txt"}]},
        }
        with pytest.raises(LxmfCodecError, match="unsupported"):
            codec.decode(packet)

    def test_decode_unknown_category_raises(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = {"foo": "bar"}
        with pytest.raises(LxmfCodecError, match="unsupported"):
            codec.decode(packet)
