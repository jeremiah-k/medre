"""Tests for LxmfCodec: decode (native → canonical), metadata
population, source_native_ref, title extraction, fields envelope,
deferred relation reconstruction, and edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.errors import LxmfCodecError
from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
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
    """Inbound relation reconstruction from fields envelope is now
    implemented.  The codec creates EventRelation objects from envelope
    relations data."""

    def test_decode_creates_event_relations_from_envelope(self) -> None:
        """Envelope with relations produces EventRelation objects."""
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
        # EventRelation objects ARE now created from envelope
        assert len(event.relations) == 1
        assert isinstance(event.relations[0], EventRelation)
        assert event.relations[0].relation_type == "reply"
        assert event.relations[0].target_event_id == "evt-target"
        assert event.relations[0].target_native_ref is None
        assert event.relations[0].fallback_text == "a reply"
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
# signature_validated flag propagation
# ===================================================================


class TestSignatureValidated:
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
        assert event.metadata.native is not None
        assert "signature_validated" not in event.metadata.native.data


# ===================================================================
# Missing/optional field handling
# ===================================================================


class TestMissingOptionalFields:
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
# delivery_method in native metadata
# ===================================================================


class TestDeliveryMethodMetadata:
    """delivery_method and has_fields are preserved in native metadata.

    Tests cover both the delivery_method field (direct, opportunistic,
    propagated, or None) and the has_fields boolean indicator for the
    presence of LXMF fields on the inbound message.
    """

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


# ===================================================================
# EventRelation reconstruction from 0xFD MEDRE envelope
# ===================================================================


class TestEventRelationReconstruction:
    """Reconstruct EventRelation objects from MEDRE envelope relations."""

    def test_single_reply_relation(self) -> None:
        """Decode packet with one reply relation in the envelope."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-reply",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-target-1",
                    "target_native_ref": {
                        "adapter": "matrix",
                        "native_channel_id": "!room:server",
                        "native_message_id": "$event-id-1",
                    },
                    "fallback_text": "replying to you",
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert isinstance(rel, EventRelation)
        assert rel.relation_type == "reply"
        assert rel.target_event_id == "evt-target-1"
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.adapter == "matrix"
        assert rel.target_native_ref.native_channel_id == "!room:server"
        assert rel.target_native_ref.native_message_id == "$event-id-1"
        assert rel.fallback_text == "replying to you"

    def test_reaction_with_key(self) -> None:
        """Decode packet with reaction relation that includes a key."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-react",
            "relations": [
                {
                    "relation_type": "reaction",
                    "target_event_id": "evt-target-2",
                    "target_native_ref": None,
                    "key": "👍",
                    "fallback_text": None,
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reaction"
        assert rel.key == "👍"
        assert rel.target_native_ref is None

    def test_multiple_relations(self) -> None:
        """Decode packet with reply + reaction relations."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-multi",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-parent",
                    "target_native_ref": {
                        "adapter": "lxmf",
                        "native_channel_id": "",
                        "native_message_id": "aa" * 32,
                    },
                    "fallback_text": "reply",
                },
                {
                    "relation_type": "reaction",
                    "target_event_id": "evt-other",
                    "target_native_ref": None,
                    "key": "❤️",
                    "fallback_text": None,
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)

        assert len(event.relations) == 2
        assert event.relations[0].relation_type == "reply"
        assert event.relations[0].target_event_id == "evt-parent"
        assert event.relations[0].target_native_ref is not None
        assert event.relations[0].target_native_ref.adapter == "lxmf"
        assert event.relations[1].relation_type == "reaction"
        assert event.relations[1].key == "❤️"

    def test_round_trip(self) -> None:
        """Embed relations via LxmfFieldsHelper, decode via codec — relations match."""
        codec = LxmfCodec("lxmf-1", _make_config())

        original_relations = (
            EventRelation(
                relation_type="reply",
                target_event_id="evt-rt-1",
                target_native_ref=NativeRef(
                    adapter="matrix",
                    native_channel_id="!room:example.com",
                    native_message_id="$orig-msg",
                ),
                key=None,
                fallback_text="original reply",
            ),
            EventRelation(
                relation_type="reaction",
                target_event_id="evt-rt-2",
                target_native_ref=None,
                key="🔥",
                fallback_text=None,
            ),
        )

        # Embed via LxmfFieldsHelper
        fields = LxmfFieldsHelper.embed_envelope(
            fields={},
            event_id="evt-round-trip",
            relations=original_relations,
            metadata={},
            source_adapter="lxmf-1",
        )

        # Build packet with embedded fields
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)

        # Verify reconstructed relations match originals
        assert len(event.relations) == 2

        r0 = event.relations[0]
        assert r0.relation_type == "reply"
        assert r0.target_event_id == "evt-rt-1"
        assert r0.target_native_ref is not None
        assert r0.target_native_ref.adapter == "matrix"
        assert r0.target_native_ref.native_channel_id == "!room:example.com"
        assert r0.target_native_ref.native_message_id == "$orig-msg"
        assert r0.fallback_text == "original reply"

        r1 = event.relations[1]
        assert r1.relation_type == "reaction"
        assert r1.target_event_id == "evt-rt-2"
        assert r1.target_native_ref is None
        assert r1.key == "🔥"

    def test_native_channel_id_none_round_trip(self) -> None:
        """Envelope with native_channel_id=None in target_native_ref round-trips,
        preserving None (not empty string)."""
        codec = LxmfCodec("lxmf-1", _make_config())

        original_relations = (
            EventRelation(
                relation_type="reply",
                target_event_id="evt-no-chan",
                target_native_ref=NativeRef(
                    adapter="meshcore",
                    native_channel_id=None,
                    native_message_id="aabbccdd",
                ),
                key=None,
                fallback_text=None,
            ),
        )

        # Embed via LxmfFieldsHelper
        fields = LxmfFieldsHelper.embed_envelope(
            fields={},
            event_id="evt-no-chan-trip",
            relations=original_relations,
            metadata={},
            source_adapter="lxmf-1",
        )

        # Build packet with embedded fields
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)

        # Verify native_channel_id is preserved as None
        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.adapter == "meshcore"
        assert rel.target_native_ref.native_channel_id is None
        assert rel.target_native_ref.native_message_id == "aabbccdd"


class TestEventRelationReconstructionEdgeCases:
    """Edge cases for EventRelation reconstruction from envelope."""

    def test_invalid_relation_type_skipped(self) -> None:
        """Envelope with relation_type='bogus' is skipped."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-bogus",
            "relations": [
                {
                    "relation_type": "bogus",
                    "target_event_id": "evt-x",
                    "target_native_ref": None,
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        assert len(event.relations) == 0

    def test_missing_target_native_ref(self) -> None:
        """Relation without target_native_ref has target_native_ref=None."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-no-ref",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-t",
                    "target_native_ref": None,
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        assert len(event.relations) == 1
        assert event.relations[0].target_native_ref is None

    def test_missing_native_message_id_in_ref(self) -> None:
        """Ref dict without native_message_id is skipped (target_native_ref=None)."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-no-msgid",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-t",
                    "target_native_ref": {
                        "adapter": "matrix",
                        "native_channel_id": "!room:server",
                        # native_message_id missing
                    },
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        assert len(event.relations) == 1
        assert event.relations[0].target_native_ref is None

    def test_corrupt_relation_not_dict(self) -> None:
        """Non-dict entries in relations list are skipped."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-corrupt",
            "relations": [
                "not a dict",
                42,
                None,
                {
                    "relation_type": "reply",
                    "target_event_id": "evt-valid",
                    "target_native_ref": None,
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        # Only the valid dict entry is reconstructed
        assert len(event.relations) == 1
        assert event.relations[0].relation_type == "reply"
        assert event.relations[0].target_event_id == "evt-valid"

    def test_empty_relations_list(self) -> None:
        """Envelope with empty relations list produces empty tuple."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-empty",
            "relations": [],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        assert event.relations == ()

    def test_no_envelope(self) -> None:
        """Packet without 0xFD field produces empty relations tuple."""
        codec = LxmfCodec("lxmf-1", _make_config())
        packet = _make_text_packet(fields={})
        event = codec.decode(packet)
        assert event.relations == ()

    def test_valid_relation_type_with_invalid_field_types(self) -> None:
        """Valid relation_type but non-string target_event_id is coerced safely."""
        codec = LxmfCodec("lxmf-1", _make_config())
        envelope = {
            "schema_version": 1,
            "event_id": "evt-invalid-fields",
            "relations": [
                {
                    "relation_type": "reply",
                    "target_event_id": [],
                    "key": 42,
                    "fallback_text": {"bad": True},
                },
            ],
            "metadata_keys": [],
        }
        fields = {0xFD: {"medre": envelope}}
        packet = _make_text_packet(fields=fields)
        event = codec.decode(packet)
        # Relation is still created but non-string fields are coerced to None.
        assert len(event.relations) == 1
        r = event.relations[0]
        assert r.relation_type == "reply"
        assert r.target_event_id is None
        assert r.key is None
        assert r.fallback_text is None
