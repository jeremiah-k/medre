"""Tests for LxmfCodec: decode (native → canonical), metadata
population, source_native_ref, title extraction, fields envelope,
deferred relation reconstruction, and edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.errors import LxmfCodecError
from medre.config.adapters.lxmf import LxmfConfig
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
        envelope = {
            "schema_version": 1,
            "event_id": "evt-123",
            "relations": [],
            "metadata_keys": [],
        }
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

    def test_decode_envelope_stored_in_metadata_custom(self) -> None:
        """Decode stores the full envelope dict under metadata.custom."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-custom",
            "source_adapter": "lxmf-2",
            "source_transport_id": "ef" * 16,
            "lineage": ["evt-parent"],
            "relations": [],
            "metadata_keys": ["k1"],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        stored = event.metadata.custom["medre_envelope"]
        assert stored["source_adapter"] == "lxmf-2"
        assert stored["source_transport_id"] == "ef" * 16
        assert stored["lineage"] == ("evt-parent",)


class TestLxmfCodecDeferredRelations:
    """Inbound relation reconstruction from fields envelope is deferred
    to a future tranche.  The codec stores the raw envelope but does NOT
    create EventRelation objects from it."""

    def test_decode_does_not_create_event_relations_from_envelope(self) -> None:
        """Envelope with relations does NOT produce EventRelation objects."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-rel",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-target",
                    "target_native_ref": None,
                    "fallback_text": "a reply",
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        # The envelope is stored but no EventRelation objects are created
        assert len(event.relations) == 0
        # The envelope data is still accessible via custom metadata
        assert event.metadata.custom["medre_envelope"]["relations"] is not None


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


# ===================================================================
# Tranche 5: signature_validated flag propagation
# ===================================================================


class TestTranche5SignatureValidated:
    """signature_validated does not leak into native metadata.

    The codec accepts signature_validated=True/False/missing without
    error.  The value is NOT stored in ``metadata.native.data`` — it
    exists only in the session's normalised inbound dict.
    """

    def test_decode_signature_validated_true(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        packet["signature_validated"] = True
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert "signature_validated" not in event.metadata.native.data

    def test_decode_signature_validated_false(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        packet["signature_validated"] = False
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert "signature_validated" not in event.metadata.native.data

    def test_decode_missing_signature_validated(self) -> None:
        """Packet without signature_validated still decodes."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["signature_validated"]
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)


# ===================================================================
# Tranche 5: Missing/optional field handling
# ===================================================================


class TestTranche5MissingOptionalFields:
    """Codec handles packets with missing or empty optional fields."""

    def test_decode_missing_source_hash(self) -> None:
        """Packet without source_hash decodes with empty sender."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["source_hash"]
        event = codec.decode(packet)
        # Classifier returns None for missing sender; codec falls back to "".
        assert isinstance(event, CanonicalEvent)
        assert event.source_transport_id == ""
        assert event.metadata.native is not None
        assert event.metadata.native.data["source_hash"] == ""

    def test_decode_missing_timestamp(self) -> None:
        """Packet without timestamp still decodes."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["timestamp"]
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.metadata.native is not None
        assert event.metadata.native.data["timestamp"] is None

    def test_decode_missing_fields_key(self) -> None:
        """Packet without fields key decodes (no envelope extracted)."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["fields"]
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert "medre_envelope" not in event.metadata.custom

    def test_decode_empty_content_raises(self) -> None:
        """Packet with empty content string is classified as 'unknown'
        and raises LxmfCodecError — the classifier requires non-empty
        content to identify a text message."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(content="")
        with pytest.raises(LxmfCodecError, match="unsupported"):
            codec.decode(packet)

    def test_decode_missing_destination_hash(self) -> None:
        """Packet without destination_hash decodes with None."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["destination_hash"]
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.metadata.native is not None
        assert event.metadata.native.data["destination_hash"] is None

    def test_decode_missing_has_fields(self) -> None:
        """Packet without has_fields decodes successfully; codec computes
        has_fields from the fields dict (empty → False)."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        del packet["has_fields"]
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.metadata.native is not None
        assert event.metadata.native.data["has_fields"] is False


# ===================================================================
# Tranche 5: delivery_method in native metadata
# ===================================================================


class TestTranche5DeliveryMethodMetadata:
    """delivery_method is preserved in native metadata."""

    def test_decode_delivery_method_direct(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        packet["delivery_method"] = "direct"
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["delivery_method"] == "direct"

    def test_decode_delivery_method_none(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet()
        # No delivery_method key
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["delivery_method"] is None

    def test_decode_has_fields_in_metadata(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(fields={0x01: "data"})
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["has_fields"] is True

    def test_decode_no_fields_has_fields_false(self) -> None:
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(fields={})
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["has_fields"] is False
