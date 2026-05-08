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
