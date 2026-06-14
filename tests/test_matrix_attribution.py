"""Tests for Matrix adapter-native attribution projection.

Covers :mod:`medre.adapters.matrix.attribution`:
MXID localpart extraction, sender field projection, and
``to_relay_fields()`` dict mapping.
"""

from __future__ import annotations

from medre.adapters.matrix.attribution import (
    extract_mxid_localpart,
    project_matrix_sender,
)

# ===================================================================
# extract_mxid_localpart
# ===================================================================


class TestExtractMxidLocalpart:
    """MXID localpart extraction from ``@user:domain`` strings."""

    def test_standard_mxid(self) -> None:
        assert extract_mxid_localpart("@alice:example.com") == "alice"

    def test_mxid_no_domain(self) -> None:
        assert extract_mxid_localpart("@bob") == "bob"

    def test_mxid_empty_localpart(self) -> None:
        # ``@:domain`` — colon at position 0, not > 0, returns rest
        assert extract_mxid_localpart("@:example.com") == ":example.com"

    def test_plain_string_no_at_prefix(self) -> None:
        assert extract_mxid_localpart("plain") == "plain"

    def test_mxid_with_hyphen_domain(self) -> None:
        assert extract_mxid_localpart("@carol:my-matrix.server.org") == "carol"

    def test_mxid_with_underscore_localpart(self) -> None:
        assert extract_mxid_localpart("@user_name:server.tld") == "user_name"

    def test_mxid_with_dots_in_localpart(self) -> None:
        assert extract_mxid_localpart("@first.last:domain.com") == "first.last"


# ===================================================================
# project_matrix_sender
# ===================================================================


class TestProjectMatrixSender:
    """Full sender projection from MXID and displayname."""

    # -- Both MXID and displayname provided --

    def test_mxid_and_displayname(self) -> None:
        result = project_matrix_sender("@alice:example.com", "Alice Liddell")
        assert result.sender_id == "@alice:example.com"
        assert result.sender_handle == "@alice:example.com"
        assert result.sender_label == "Alice Liddell"
        assert result.sender_short_label == "alice"

    # -- MXID only (no displayname) --

    def test_mxid_only_label_falls_back_to_localpart(self) -> None:
        result = project_matrix_sender("@bob:matrix.org")
        assert result.sender_id == "@bob:matrix.org"
        assert result.sender_handle == "@bob:matrix.org"
        assert result.sender_label == "bob"
        assert result.sender_short_label == "bob"

    # -- MXID without domain (no colon) --

    def test_mxid_no_domain(self) -> None:
        result = project_matrix_sender("@carol")
        assert result.sender_id == "@carol"
        assert result.sender_label == "carol"
        assert result.sender_short_label == "carol"

    # -- Neither MXID nor displayname --

    def test_none_mxid_none_displayname(self) -> None:
        result = project_matrix_sender(None, None)
        assert result.sender_id is None
        assert result.sender_handle is None
        assert result.sender_label is None
        assert result.sender_short_label is None

    # -- None MXID, displayname present --

    def test_none_mxid_with_displayname(self) -> None:
        result = project_matrix_sender(None, "Ghost User")
        assert result.sender_id is None
        assert result.sender_handle is None
        assert result.sender_label == "Ghost User"
        assert result.sender_short_label is None

    # -- Empty string displayname (falsy) falls back to localpart --

    def test_empty_displayname_falls_back_to_localpart(self) -> None:
        result = project_matrix_sender("@dave:server.org", "")
        assert result.sender_label == "dave"
        assert result.sender_short_label == "dave"

    # -- None displayname falls back to localpart --

    def test_none_displayname_falls_back_to_localpart(self) -> None:
        result = project_matrix_sender("@eve:server.org", None)
        assert result.sender_label == "eve"

    # -- Displayname with special characters --

    def test_displayname_with_special_chars(self) -> None:
        result = project_matrix_sender("@frank:server.org", "Frank (dev) 🚀")
        assert result.sender_label == "Frank (dev) 🚀"
        assert result.sender_short_label == "frank"

    # -- Immutability --

    def test_result_is_frozen(self) -> None:
        result = project_matrix_sender("@alice:example.com", "Alice")
        # Frozen dataclass should raise on attribute assignment
        errored = False
        try:
            result.sender_id = "mutated"  # type: ignore[misc]
        except AttributeError:
            errored = True
        assert errored, "MatrixSenderFields should be frozen"


# ===================================================================
# MatrixSenderFields.to_relay_fields
# ===================================================================


class TestToRelayFields:
    """Dict mapping to RelayAttribution canonical field names."""

    def test_roundtrip_keys(self) -> None:
        result = project_matrix_sender("@alice:example.com", "Alice")
        fields = result.to_relay_fields()
        assert set(fields.keys()) == {
            "source_sender_id",
            "source_sender_handle",
            "source_sender_label",
            "source_sender_short_label",
        }

    def test_values_match(self) -> None:
        result = project_matrix_sender("@alice:example.com", "Alice")
        fields = result.to_relay_fields()
        assert fields["source_sender_id"] == "@alice:example.com"
        assert fields["source_sender_handle"] == "@alice:example.com"
        assert fields["source_sender_label"] == "Alice"
        assert fields["source_sender_short_label"] == "alice"

    def test_none_produces_none_values(self) -> None:
        result = project_matrix_sender(None, None)
        fields = result.to_relay_fields()
        assert fields["source_sender_id"] is None
        assert fields["source_sender_handle"] is None
        assert fields["source_sender_label"] is None
        assert fields["source_sender_short_label"] is None

    def test_relay_fields_usable_in_relay_attribution(self) -> None:
        """Verify the dict can be passed as kwargs to RelayAttribution."""
        from medre.core.rendering.attribution import RelayAttribution

        result = project_matrix_sender("@alice:example.com", "Alice")
        relay_fields = result.to_relay_fields()
        attr = RelayAttribution(**relay_fields)
        assert attr.source_sender_id == "@alice:example.com"
        assert attr.source_sender_handle == "@alice:example.com"
        assert attr.source_sender_label == "Alice"
        assert attr.source_sender_short_label == "alice"
