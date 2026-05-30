"""Ingress conformance tests: native input becomes CanonicalEvent.

Loads deterministic JSON fixtures for Matrix and Meshtastic, decodes
them through the real codecs, and asserts that the resulting
CanonicalEvent satisfies the MEDRE ingress contracts:

* ``event_kind`` matches the fixture expectation.
* ``source_native_ref`` carries the correct adapter, channel, and
  message ID.
* ``source_adapter`` is correct.
* ``source_channel_id`` is correct.
* Payload shape is canonical (body, msgtype/portnum as appropriate).
* Relations are correct (type, target_native_ref, key).
* Metadata is deterministic (native metadata present).

No real Matrix homeserver, real Meshtastic hardware, SDK network tests,
or external network involved.
"""

from __future__ import annotations

import pytest

from tests.conformance.conftest import MATRIX_ADAPTER_ID, MESHTASTIC_ADAPTER_ID
from tests.conformance.fixtures.loader import load_all_fixtures

# ---------------------------------------------------------------------------
# Matrix ingress conformance
# ---------------------------------------------------------------------------


class TestMatrixIngressConformance:
    """Assert Matrix codec decode contracts against JSON fixtures."""

    @pytest.fixture(params=load_all_fixtures("matrix"))
    def fixture(self, request) -> dict:
        """Parameterise over all Matrix fixtures."""
        return request.param

    def test_event_kind_matches(self, matrix_codec, fixture):
        """Codec decode produces the expected event_kind."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.event_kind == fixture["expected"]["event_kind"]

    def test_source_adapter_correct(self, matrix_codec, fixture):
        """source_adapter is set to the codec's adapter ID."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.source_adapter == MATRIX_ADAPTER_ID

    def test_source_channel_id_correct(self, matrix_codec, fixture):
        """source_channel_id matches the room_id from decode context."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.source_channel_id == fixture["expected"]["source_channel_id"]

    def test_source_native_ref(self, matrix_codec, fixture):
        """source_native_ref carries adapter, channel, and message ID."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        expected_ref = fixture["expected"]["source_native_ref"]
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == expected_ref["adapter"]
        assert (
            event.source_native_ref.native_channel_id
            == expected_ref["native_channel_id"]
        )
        assert (
            event.source_native_ref.native_message_id
            == expected_ref["native_message_id"]
        )

    def test_payload_shape(self, matrix_codec, fixture):
        """Payload contains the expected body and msgtype."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        for key, value in fixture["expected"]["payload_shape"].items():
            assert (
                event.payload.get(key) == value
            ), f"payload[{key!r}]: expected {value!r}, got {event.payload.get(key)!r}"

    def test_relations_count(self, matrix_codec, fixture):
        """Number of relations matches fixture expectation."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert len(event.relations) == fixture["expected"]["relations_count"]

    def test_first_relation(self, matrix_codec, fixture):
        """First relation type, target, and key match when present."""
        expected = fixture["expected"]
        if expected["relations_count"] == 0:
            pytest.skip("No relations in this fixture")
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        rel = event.relations[0]
        first = expected["first_relation"]
        assert rel.relation_type == first["relation_type"]
        if "key" in first:
            assert rel.key == first["key"]
        else:
            assert rel.key is None
        if "target_native_ref" in first:
            assert rel.target_native_ref is not None
            tnr = first["target_native_ref"]
            assert rel.target_native_ref.adapter == tnr["adapter"]
            assert rel.target_native_ref.native_channel_id == tnr["native_channel_id"]
            assert rel.target_native_ref.native_message_id == tnr["native_message_id"]
        else:
            assert rel.target_native_ref is None

    def test_metadata_deterministic(self, matrix_codec, fixture):
        """Native metadata carries deterministic fixture-known values."""
        event = matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        expected_meta = fixture["expected"]["expected_metadata"]
        for key, expected_value in expected_meta.items():
            assert native_data[key] == expected_value, (
                f"metadata[{key!r}]: expected {expected_value!r}, "
                f"got {native_data.get(key)!r}"
            )


# ---------------------------------------------------------------------------
# Meshtastic ingress conformance
# ---------------------------------------------------------------------------


class TestMeshtasticIngressConformance:
    """Assert Meshtastic codec decode contracts against JSON fixtures."""

    @pytest.fixture(params=load_all_fixtures("meshtastic"))
    def fixture(self, request) -> dict:
        """Parameterise over all Meshtastic fixtures."""
        return request.param

    def test_event_kind_matches(self, meshtastic_codec, fixture):
        """Codec decode produces the expected event_kind."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.event_kind == fixture["expected"]["event_kind"]

    def test_source_adapter_correct(self, meshtastic_codec, fixture):
        """source_adapter is set to the codec's adapter ID."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.source_adapter == MESHTASTIC_ADAPTER_ID

    def test_source_channel_id_correct(self, meshtastic_codec, fixture):
        """source_channel_id is a string of the channel index."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.source_channel_id == fixture["expected"]["source_channel_id"]

    def test_source_native_ref(self, meshtastic_codec, fixture):
        """source_native_ref carries adapter, channel, and packet ID."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        expected_ref = fixture["expected"]["source_native_ref"]
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == expected_ref["adapter"]
        assert (
            event.source_native_ref.native_channel_id
            == expected_ref["native_channel_id"]
        )
        assert (
            event.source_native_ref.native_message_id
            == expected_ref["native_message_id"]
        )

    def test_payload_shape(self, meshtastic_codec, fixture):
        """Payload body and other fields match fixture expectations."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        for key, value in fixture["expected"]["payload_shape"].items():
            assert (
                event.payload.get(key) == value
            ), f"payload[{key!r}]: expected {value!r}, got {event.payload.get(key)!r}"

    def test_relations_count(self, meshtastic_codec, fixture):
        """Number of relations matches fixture expectation."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert len(event.relations) == fixture["expected"]["relations_count"]

    def test_first_relation(self, meshtastic_codec, fixture):
        """First relation type, target, and key match when present."""
        expected = fixture["expected"]
        if expected["relations_count"] == 0:
            pytest.skip("No relations in this fixture")
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        rel = event.relations[0]
        first = expected["first_relation"]
        assert rel.relation_type == first["relation_type"]
        if "key" in first:
            assert rel.key == first["key"]
        else:
            assert rel.key is None
        if "target_native_ref" in first:
            assert rel.target_native_ref is not None
            tnr = first["target_native_ref"]
            assert rel.target_native_ref.adapter == tnr["adapter"]
            assert rel.target_native_ref.native_channel_id == tnr["native_channel_id"]
            assert rel.target_native_ref.native_message_id == tnr["native_message_id"]
        else:
            assert rel.target_native_ref is None

    def test_metadata_deterministic(self, meshtastic_codec, fixture):
        """Native metadata carries deterministic fixture-known values."""
        event = meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        expected_meta = fixture["expected"]["expected_metadata"]
        for key, expected_value in expected_meta.items():
            assert native_data[key] == expected_value, (
                f"metadata[{key!r}]: expected {expected_value!r}, "
                f"got {native_data.get(key)!r}"
            )
