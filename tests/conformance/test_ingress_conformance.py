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

from .conftest import MATRIX_ADAPTER_ID, MESHTASTIC_ADAPTER_ID
from .fixtures.loader import load_all_fixtures

# ---------------------------------------------------------------------------
# Matrix ingress conformance
# ---------------------------------------------------------------------------


class TestMatrixIngressConformance:
    """Assert Matrix codec decode contracts against JSON fixtures.

    Structurally mirrors ``TestMeshtasticIngressConformance``: both
    classes run the same assertion categories (event_kind, source_adapter,
    source_channel_id, source_native_ref, payload_shape, relations,
    metadata) against their respective adapter fixtures.  The duplication
    is intentional -- each class uses a different codec and adapter ID,
    and merging them into a single parametrised class would obscure
    per-adapter contract differences.
    """

    @pytest.fixture(params=load_all_fixtures("matrix"))
    def fixture(self, request) -> dict:
        """Parameterise over all Matrix fixtures."""
        return request.param

    @pytest.fixture
    def decoded_event(self, matrix_codec, fixture):
        """Decode the fixture once and return the CanonicalEvent."""
        return matrix_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )

    def test_event_kind_matches(self, decoded_event, fixture):
        """Codec decode produces the expected event_kind."""
        assert decoded_event.event_kind == fixture["expected"]["event_kind"]

    def test_source_adapter_correct(self, decoded_event):
        """source_adapter is set to the codec's adapter ID."""
        assert decoded_event.source_adapter == MATRIX_ADAPTER_ID

    def test_source_channel_id_correct(self, decoded_event, fixture):
        """source_channel_id matches the room_id from decode context."""
        assert (
            decoded_event.source_channel_id == fixture["expected"]["source_channel_id"]
        )

    def test_source_native_ref(self, decoded_event, fixture):
        """source_native_ref carries adapter, channel, and message ID."""
        expected_ref = fixture["expected"]["source_native_ref"]
        assert decoded_event.source_native_ref is not None
        assert decoded_event.source_native_ref.adapter == expected_ref["adapter"]
        assert (
            decoded_event.source_native_ref.native_channel_id
            == expected_ref["native_channel_id"]
        )
        assert (
            decoded_event.source_native_ref.native_message_id
            == expected_ref["native_message_id"]
        )

    def test_payload_shape(self, decoded_event, fixture):
        """Payload contains the expected body and msgtype."""
        for key, value in fixture["expected"]["payload_shape"].items():
            assert (
                decoded_event.payload.get(key) == value
            ), f"payload[{key!r}]: expected {value!r}, got {decoded_event.payload.get(key)!r}"

    def test_relations_count(self, decoded_event, fixture):
        """Number of relations matches fixture expectation."""
        assert len(decoded_event.relations) == fixture["expected"]["relations_count"]

    def test_first_relation(self, decoded_event, fixture):
        """First relation type, target, and key match when present."""
        expected = fixture["expected"]
        if expected["relations_count"] == 0:
            pytest.skip("No relations in this fixture")
        rel = decoded_event.relations[0]
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

    def test_metadata_deterministic(self, decoded_event, fixture):
        """Native metadata carries deterministic fixture-known values.

        Also asserts ``metadata_has_native`` from the fixture to ensure
        conformance between fixture expectations and the decoded event.
        """
        assert decoded_event.metadata.native is not None
        # Assert the fixture's metadata_has_native flag matches reality.
        assert fixture["expected"]["metadata_has_native"] is True
        native_data = decoded_event.metadata.native.data
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
    """Assert Meshtastic codec decode contracts against JSON fixtures.

    Structurally mirrors ``TestMatrixIngressConformance``: both
    classes run the same assertion categories (event_kind, source_adapter,
    source_channel_id, source_native_ref, payload_shape, relations,
    metadata) against their respective adapter fixtures.  The duplication
    is intentional -- each class uses a different codec and adapter ID,
    and merging them into a single parametrised class would obscure
    per-adapter contract differences.
    """

    @pytest.fixture(params=load_all_fixtures("meshtastic"))
    def fixture(self, request) -> dict:
        """Parameterise over all Meshtastic fixtures."""
        return request.param

    @pytest.fixture
    def decoded_event(self, meshtastic_codec, fixture):
        """Decode the fixture once and return the CanonicalEvent."""
        return meshtastic_codec.decode(
            fixture["native_input"],
            **fixture["decode_context"],
        )

    def test_event_kind_matches(self, decoded_event, fixture):
        """Codec decode produces the expected event_kind."""
        assert decoded_event.event_kind == fixture["expected"]["event_kind"]

    def test_source_adapter_correct(self, decoded_event):
        """source_adapter is set to the codec's adapter ID."""
        assert decoded_event.source_adapter == MESHTASTIC_ADAPTER_ID

    def test_source_channel_id_correct(self, decoded_event, fixture):
        """source_channel_id is a string of the channel index."""
        assert (
            decoded_event.source_channel_id == fixture["expected"]["source_channel_id"]
        )

    def test_source_native_ref(self, decoded_event, fixture):
        """source_native_ref carries adapter, channel, and packet ID."""
        expected_ref = fixture["expected"]["source_native_ref"]
        assert decoded_event.source_native_ref is not None
        assert decoded_event.source_native_ref.adapter == expected_ref["adapter"]
        assert (
            decoded_event.source_native_ref.native_channel_id
            == expected_ref["native_channel_id"]
        )
        assert (
            decoded_event.source_native_ref.native_message_id
            == expected_ref["native_message_id"]
        )

    def test_payload_shape(self, decoded_event, fixture):
        """Payload body and other fields match fixture expectations."""
        for key, value in fixture["expected"]["payload_shape"].items():
            assert (
                decoded_event.payload.get(key) == value
            ), f"payload[{key!r}]: expected {value!r}, got {decoded_event.payload.get(key)!r}"

    def test_relations_count(self, decoded_event, fixture):
        """Number of relations matches fixture expectation."""
        assert len(decoded_event.relations) == fixture["expected"]["relations_count"]

    def test_first_relation(self, decoded_event, fixture):
        """First relation type, target, and key match when present."""
        expected = fixture["expected"]
        if expected["relations_count"] == 0:
            pytest.skip("No relations in this fixture")
        rel = decoded_event.relations[0]
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

    def test_metadata_deterministic(self, decoded_event, fixture):
        """Native metadata carries deterministic fixture-known values.

        Also asserts ``metadata_has_native`` from the fixture to ensure
        conformance between fixture expectations and the decoded event.
        """
        assert decoded_event.metadata.native is not None
        # Assert the fixture's metadata_has_native flag matches reality.
        assert fixture["expected"]["metadata_has_native"] is True
        native_data = decoded_event.metadata.native.data
        expected_meta = fixture["expected"]["expected_metadata"]
        for key, expected_value in expected_meta.items():
            assert native_data[key] == expected_value, (
                f"metadata[{key!r}]: expected {expected_value!r}, "
                f"got {native_data.get(key)!r}"
            )
