"""Tests for LxmfFieldsHelper: embed_envelope, extract_envelope,
round-trip, has_attachment, envelope_has_relations, and corrupt
envelope handling.
"""

from __future__ import annotations

import pytest

from medre.adapters.lxmf.fields import (
    FIELD_MEDRE_ENVELOPE,
    LXMF_NAMESPACE,
    LxmfFieldsHelper,
)


class TestEmbedEnvelope:
    """embed_envelope adds correct key."""

    def test_embed_adds_envelope_key(self) -> None:
        fields = {}
        result = LxmfFieldsHelper.embed_envelope(
            fields, "evt-1", (), {}
        )
        assert FIELD_MEDRE_ENVELOPE in result

    def test_embed_envelope_has_namespace(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE]
        assert LXMF_NAMESPACE in envelope

    def test_embed_envelope_contains_event_id(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-42", (), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["event_id"] == "evt-42"

    def test_embed_envelope_contains_schema_version(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["schema_version"] == 1

    def test_embed_envelope_contains_relations(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {"key": "value"}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert isinstance(envelope["relations"], list)
        assert len(envelope["relations"]) == 0

    def test_embed_envelope_contains_metadata_keys(self) -> None:
        meta = {"source_hash": "ab", "timestamp": 42}
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), meta
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert "source_hash" in envelope["metadata_keys"]
        assert "timestamp" in envelope["metadata_keys"]

    def test_embed_does_not_mutate_original(self) -> None:
        original = {0x01: "keep"}
        result = LxmfFieldsHelper.embed_envelope(
            original, "evt-1", (), {}
        )
        assert FIELD_MEDRE_ENVELOPE not in original
        assert FIELD_MEDRE_ENVELOPE in result
        assert result[0x01] == "keep"

    def test_embed_preserves_existing_fields(self) -> None:
        existing = {0x01: "value1", 0x02: "value2"}
        result = LxmfFieldsHelper.embed_envelope(
            existing, "evt-1", (), {}
        )
        assert result[0x01] == "value1"
        assert result[0x02] == "value2"

    def test_embed_envelope_includes_source_adapter(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}, source_adapter="lxmf-1"
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["source_adapter"] == "lxmf-1"

    def test_embed_envelope_includes_source_transport_id(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {},
            source_transport_id="ab" * 16,
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["source_transport_id"] == "ab" * 16

    def test_embed_envelope_includes_source_channel_id(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {},
            source_channel_id="ch-1",
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["source_channel_id"] == "ch-1"

    def test_embed_envelope_includes_lineage(self) -> None:
        lineage = ("evt-parent", "evt-grandparent")
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}, lineage=lineage,
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["lineage"] == ["evt-parent", "evt-grandparent"]

    def test_embed_envelope_defaults_lineage_to_empty(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["lineage"] == []

    def test_embed_envelope_defaults_provenance_to_none_or_empty(self) -> None:
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["source_adapter"] == ""
        assert envelope["source_transport_id"] is None
        assert envelope["source_channel_id"] is None

    def test_embed_envelope_with_relations(self) -> None:
        """Relations with native refs are serialised into the envelope."""
        from medre.core.events.canonical import EventRelation, NativeRef

        native_ref = NativeRef(
            adapter="discord",
            native_channel_id="ch-1",
            native_message_id="msg-42",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target",
            target_native_ref=native_ref,
            key=None,
            fallback_text="reply to original",
        )
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (rel,), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert len(envelope["relations"]) == 1
        ser = envelope["relations"][0]
        assert ser["relation_type"] == "reply"
        assert ser["target_event_id"] == "evt-target"
        assert ser["target_native_ref"]["adapter"] == "discord"
        assert ser["target_native_ref"]["native_message_id"] == "msg-42"
        assert ser["fallback_text"] == "reply to original"

    def test_embed_envelope_relation_without_native_ref(self) -> None:
        """Relations without native_ref serialise target_native_ref as None."""
        from medre.core.events.canonical import EventRelation

        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-parent",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        result = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (rel,), {}
        )
        envelope = result[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        ser = envelope["relations"][0]
        assert ser["target_native_ref"] is None


class TestExtractEnvelope:
    """extract_envelope returns correct data."""

    def test_extract_returns_correct_data(self) -> None:
        envelope = {
            "schema_version": 1,
            "event_id": "evt-99",
            "relations": [],
            "metadata_keys": ["key1"],
        }
        fields = {FIELD_MEDRE_ENVELOPE: {LXMF_NAMESPACE: envelope}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is not None
        assert result["event_id"] == "evt-99"

    def test_extract_returns_full_envelope(self) -> None:
        """extract_envelope returns the full envelope dict, not just metadata_keys."""
        envelope = {
            "schema_version": 1,
            "event_id": "evt-full",
            "source_adapter": "lxmf-1",
            "source_transport_id": "ab" * 16,
            "source_channel_id": None,
            "lineage": ["evt-parent"],
            "relations": [{"relation_type": "reply", "target_event_id": "evt-r"}],
            "metadata_keys": ["k1"],
        }
        fields = {FIELD_MEDRE_ENVELOPE: {LXMF_NAMESPACE: envelope}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is not None
        assert result["source_adapter"] == "lxmf-1"
        assert result["source_transport_id"] == "ab" * 16
        assert result["lineage"] == ["evt-parent"]
        assert len(result["relations"]) == 1

    def test_extract_returns_none_on_missing(self) -> None:
        fields = {}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_wrong_key(self) -> None:
        fields = {0xFE: "something"}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_non_dict(self) -> None:
        result = LxmfFieldsHelper.extract_envelope("not a dict")
        assert result is None

    def test_extract_returns_none_on_corrupt_envelope(self) -> None:
        """Corrupt envelope value (not a dict) returns None."""
        fields = {FIELD_MEDRE_ENVELOPE: "corrupt string"}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_missing_namespace(self) -> None:
        """Envelope dict without LXMF_NAMESPACE returns None."""
        fields = {FIELD_MEDRE_ENVELOPE: {"other": "data"}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_non_dict_namespace_value(self) -> None:
        """Namespace value that is not a dict returns None."""
        fields = {FIELD_MEDRE_ENVELOPE: {LXMF_NAMESPACE: "not a dict"}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_missing_schema_version(self) -> None:
        """Envelope without schema_version returns None."""
        envelope = {"event_id": "evt-1", "relations": []}
        fields = {FIELD_MEDRE_ENVELOPE: {LXMF_NAMESPACE: envelope}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None

    def test_extract_returns_none_on_missing_event_id(self) -> None:
        """Envelope without event_id returns None."""
        envelope = {"schema_version": 1, "relations": []}
        fields = {FIELD_MEDRE_ENVELOPE: {LXMF_NAMESPACE: envelope}}
        result = LxmfFieldsHelper.extract_envelope(fields)
        assert result is None


class TestRoundTrip:
    """embed then extract returns same data."""

    def test_round_trip_preserves_event_id(self) -> None:
        fields = {}
        embedded = LxmfFieldsHelper.embed_envelope(
            fields, "evt-round-trip", (), {}
        )
        extracted = LxmfFieldsHelper.extract_envelope(embedded)
        assert extracted is not None
        assert extracted["event_id"] == "evt-round-trip"

    def test_round_trip_preserves_metadata_keys(self) -> None:
        meta = {"key1": "v1", "key2": "v2"}
        embedded = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), meta
        )
        extracted = LxmfFieldsHelper.extract_envelope(embedded)
        assert extracted is not None
        assert "key1" in extracted["metadata_keys"]
        assert "key2" in extracted["metadata_keys"]

    def test_round_trip_with_existing_fields(self) -> None:
        existing = {0x01: "data"}
        embedded = LxmfFieldsHelper.embed_envelope(
            existing, "evt-1", (), {}
        )
        extracted = LxmfFieldsHelper.extract_envelope(embedded)
        assert extracted is not None
        assert embedded[0x01] == "data"

    def test_round_trip_preserves_provenance(self) -> None:
        embedded = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (), {},
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            lineage=("evt-parent",),
        )
        extracted = LxmfFieldsHelper.extract_envelope(embedded)
        assert extracted is not None
        assert extracted["source_adapter"] == "lxmf-1"
        assert extracted["source_transport_id"] == "ab" * 16
        assert extracted["lineage"] == ["evt-parent"]

    def test_round_trip_with_relations(self) -> None:
        from medre.core.events.canonical import EventRelation

        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target",
            target_native_ref=None,
            key=None,
            fallback_text="replied",
        )
        embedded = LxmfFieldsHelper.embed_envelope(
            {}, "evt-1", (rel,), {}
        )
        extracted = LxmfFieldsHelper.extract_envelope(embedded)
        assert extracted is not None
        assert len(extracted["relations"]) == 1
        assert extracted["relations"][0]["relation_type"] == "reply"
        assert extracted["relations"][0]["target_event_id"] == "evt-target"


class TestHasAttachment:
    """has_attachment detects file/image/audio fields."""

    def test_has_attachment_file(self) -> None:
        fields = {0x05: [{"name": "file.txt", "size": 100}]}
        assert LxmfFieldsHelper.has_attachment(fields) is True

    def test_has_attachment_image(self) -> None:
        fields = {0x06: "image data"}
        assert LxmfFieldsHelper.has_attachment(fields) is True

    def test_has_attachment_audio(self) -> None:
        fields = {0x07: "audio data"}
        assert LxmfFieldsHelper.has_attachment(fields) is True

    def test_has_attachment_false(self) -> None:
        fields = {0xFD: {"medre": {}}}
        assert LxmfFieldsHelper.has_attachment(fields) is False

    def test_has_attachment_empty(self) -> None:
        assert LxmfFieldsHelper.has_attachment({}) is False

    def test_has_attachment_non_dict(self) -> None:
        assert LxmfFieldsHelper.has_attachment("not a dict") is False


class TestEnvelopeHasRelations:
    """envelope_has_relations checks for relations in an envelope."""

    def test_envelope_has_relations_true(self) -> None:
        envelope = {
            "schema_version": 1,
            "event_id": "evt-1",
            "relations": [{"relation_type": "reply"}],
        }
        assert LxmfFieldsHelper.envelope_has_relations(envelope) is True

    def test_envelope_has_relations_false_empty(self) -> None:
        envelope = {
            "schema_version": 1,
            "event_id": "evt-1",
            "relations": [],
        }
        assert LxmfFieldsHelper.envelope_has_relations(envelope) is False

    def test_envelope_has_relations_false_missing(self) -> None:
        envelope = {
            "schema_version": 1,
            "event_id": "evt-1",
        }
        assert LxmfFieldsHelper.envelope_has_relations(envelope) is False
