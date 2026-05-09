"""Tests for LxmfFieldsHelper: embed_envelope, extract_envelope,
round-trip, has_attachment, and corrupt envelope handling.
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
