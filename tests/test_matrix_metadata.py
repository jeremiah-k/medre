"""Tests for MatrixMetadataEnvelope: round-trip serialisation, tolerance,
secret exclusion, and schema defaults.
"""

from __future__ import annotations

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope


class TestMatrixMetadataEnvelope:
    """MatrixMetadataEnvelope serialisation and parsing."""

    def test_round_trip_parse_render(self) -> None:
        envelope = MatrixMetadataEnvelope(
            schema_version=1,
            canonical_event_id="evt-001",
            source_adapter="matrix-1",
            source_channel="!room:server",
            provenance="matrix",
            relation_info="reply",
            lineage_pointer="parent-evt",
            metadata_mode="safe",
            native_source_summary="Matrix room message",
        )
        content = envelope.to_content()
        parsed = MatrixMetadataEnvelope.from_content(content)
        assert parsed is not None
        assert parsed.canonical_event_id == "evt-001"
        assert parsed.source_adapter == "matrix-1"
        assert parsed.source_channel == "!room:server"
        assert parsed.provenance == "matrix"
        assert parsed.relation_info == "reply"
        assert parsed.lineage_pointer == "parent-evt"
        assert parsed.metadata_mode == "safe"
        assert parsed.native_source_summary == "Matrix room message"

    def test_unknown_fields_tolerated(self) -> None:
        content = {
            "medre": {
                "envelope": {
                    "canonical_event_id": "evt-002",
                    "source_adapter": "matrix-1",
                    "unknown_future_field": "should not break",
                }
            }
        }
        parsed = MatrixMetadataEnvelope.from_content(content)
        assert parsed is not None
        assert parsed.canonical_event_id == "evt-002"

    def test_missing_envelope_returns_none(self) -> None:
        content = {"msgtype": "m.text", "body": "hello"}
        parsed = MatrixMetadataEnvelope.from_content(content)
        assert parsed is None

    def test_corrupt_envelope_returns_none(self) -> None:
        content = {"medre": {"envelope": "not a dict"}}
        parsed = MatrixMetadataEnvelope.from_content(content)
        assert parsed is None

    def test_no_secrets_in_envelope_output(self) -> None:
        envelope = MatrixMetadataEnvelope(
            canonical_event_id="evt-003",
            source_adapter="matrix-1",
        )
        rendered = envelope.to_content()
        rendered_str = str(rendered)
        assert "access_token" not in rendered_str
        assert "password" not in rendered_str
        assert "secret" not in rendered_str

    def test_schema_version_default(self) -> None:
        envelope = MatrixMetadataEnvelope()
        assert envelope.schema_version == 1

    def test_envelope_with_all_empty_fields_is_valid(self) -> None:
        """Envelope with default empty fields is still decoded correctly."""
        envelope = MatrixMetadataEnvelope()
        content = envelope.to_content()
        parsed = MatrixMetadataEnvelope.from_content(content)
        assert parsed is not None
        assert parsed.canonical_event_id == ""
        assert parsed.source_adapter == ""
        assert parsed.schema_version == 1

    def test_schema_version_must_be_positive(self) -> None:
        """__post_init__ rejects non-positive schema_version."""
        import pytest

        with pytest.raises(ValueError, match="positive integer"):
            MatrixMetadataEnvelope(schema_version=0)

    def test_frozen_dataclass_prevents_mutation(self) -> None:
        """Envelope is frozen and cannot be mutated after creation."""
        import dataclasses

        import pytest

        envelope = MatrixMetadataEnvelope()
        with pytest.raises(dataclasses.FrozenInstanceError):
            envelope.source_adapter = "tampered"  # type: ignore[misc]

    def test_dataclass_choice_is_intentional(self) -> None:
        """Confirm MatrixMetadataEnvelope is a dataclass, not msgspec.

        This is an adapter-internal serialization helper that does not
        need msgspec roundtrip encoding and is not stored in the
        canonical event model.
        """
        import dataclasses

        assert dataclasses.is_dataclass(MatrixMetadataEnvelope)
        assert hasattr(MatrixMetadataEnvelope, "__dataclass_fields__")
