"""Tests for Matrix adapter-native attribution projection.

Covers :mod:`medre.adapters.matrix.attribution`:
MXID localpart extraction, sender field projection, and
``to_relay_fields()`` dict mapping.
"""

from __future__ import annotations

from medre.adapters.matrix.attribution import (
    extract_mxid_localpart,
    project_matrix_attribution,
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
        # ``@:domain`` — colon at position 0, localpart before it is empty.
        assert extract_mxid_localpart("@:example.com") == ""

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


# ===================================================================
# extract_mxid_localpart — malformed MXIDs
# ===================================================================


class TestExtractMxidLocalpartMalformed:
    """Localpart extraction for malformed and edge-case MXID strings."""

    def test_at_colon_only(self) -> None:
        # ``@:`` — leading @, empty localpart, empty domain.
        assert extract_mxid_localpart("@:") == ""

    def test_double_at_prefix(self) -> None:
        # ``@@:x`` — rest is ``@:x``, colon at index 1, localpart ``@``.
        assert extract_mxid_localpart("@@:x") == "@"

    def test_no_leading_at_with_colon(self) -> None:
        # ``alice:example.com`` — no @ prefix, returned unchanged.
        assert extract_mxid_localpart("alice:example.com") == "alice:example.com"

    def test_empty_string(self) -> None:
        assert extract_mxid_localpart("") == ""

    def test_at_only(self) -> None:
        # ``@`` — leading @, rest is empty, no colon.
        assert extract_mxid_localpart("@") == ""

    def test_at_colon_domain(self) -> None:
        # ``@:domain`` — empty localpart before the colon.
        assert extract_mxid_localpart("@:domain") == ""


# ===================================================================
# project_matrix_attribution — dispatch-oriented projection
# ===================================================================


class TestProjectMatrixAttribution:
    """Dispatch-oriented projection from native metadata dict."""

    # -- displayname present --

    def test_displayname_present(self) -> None:
        result = project_matrix_attribution(
            {"sender": "@alice:example.com", "displayname": "Alice Liddell"}
        )
        assert result["source_sender_id"] == "@alice:example.com"
        assert result["source_sender_handle"] == "@alice:example.com"
        assert result["source_sender_label"] == "Alice Liddell"
        assert result["source_sender_short_label"] == "alice"

    def test_display_name_underscore_key(self) -> None:
        result = project_matrix_attribution(
            {"sender": "@alice:example.com", "display_name": "Alice U"}
        )
        assert result["source_sender_label"] == "Alice U"

    def test_displayname_takes_precedence_over_display_name(self) -> None:
        result = project_matrix_attribution(
            {
                "sender": "@alice:example.com",
                "displayname": "Primary",
                "display_name": "Secondary",
            }
        )
        assert result["source_sender_label"] == "Primary"

    # -- displayname missing (key absent) --

    def test_displayname_key_absent(self) -> None:
        """When displayname/display_name keys are absent, label is None.
        No localpart fallback in the dispatch-oriented projection."""
        result = project_matrix_attribution({"sender": "@bob:matrix.org"})
        assert result["source_sender_id"] == "@bob:matrix.org"
        assert result["source_sender_handle"] == "@bob:matrix.org"
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "bob"

    # -- displayname empty string --

    def test_displayname_empty_string(self) -> None:
        """An explicit empty string displayname stays absent (None),
        never the literal string 'None' or ''."""
        result = project_matrix_attribution(
            {"sender": "@carol:server.org", "displayname": ""}
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "carol"

    def test_display_name_empty_string_falls_to_display_name(self) -> None:
        """Empty displayname is falsy, so display_name is checked."""
        result = project_matrix_attribution(
            {
                "sender": "@carol:server.org",
                "displayname": "",
                "display_name": "Carol Real",
            }
        )
        assert result["source_sender_label"] == "Carol Real"

    def test_both_displayname_keys_empty(self) -> None:
        result = project_matrix_attribution(
            {
                "sender": "@carol:server.org",
                "displayname": "",
                "display_name": "",
            }
        )
        assert result["source_sender_label"] is None

    # -- displayname is Python None --

    def test_displayname_explicit_none(self) -> None:
        """Python None displayname must never render as the literal 'None'."""
        result = project_matrix_attribution(
            {"sender": "@dave:matrix.org", "displayname": None}
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "dave"

    def test_displayname_none_falls_to_display_name(self) -> None:
        """None displayname is falsy, so display_name is checked."""
        result = project_matrix_attribution(
            {
                "sender": "@dave:matrix.org",
                "displayname": None,
                "display_name": "Dave Set",
            }
        )
        assert result["source_sender_label"] == "Dave Set"

    # -- sender missing / None --

    def test_sender_key_absent(self) -> None:
        result = project_matrix_attribution({"displayname": "Ghost"})
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] == "Ghost"
        assert result["source_sender_short_label"] is None

    def test_sender_none(self) -> None:
        result = project_matrix_attribution({"sender": None, "displayname": "Ghost"})
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] == "Ghost"
        assert result["source_sender_short_label"] is None

    def test_sender_empty_string(self) -> None:
        result = project_matrix_attribution({"sender": "", "displayname": "Ghost"})
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] == "Ghost"
        assert result["source_sender_short_label"] is None

    def test_empty_native_data(self) -> None:
        result = project_matrix_attribution({})
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] is None

    # -- malformed MXIDs through dispatch projection --

    def test_sender_at_colon_only(self) -> None:
        """``@:`` — empty localpart, short_label normalised to None."""
        result = project_matrix_attribution({"sender": "@:"})
        assert result["source_sender_id"] == "@:"
        assert result["source_sender_handle"] == "@:"
        assert result["source_sender_short_label"] is None

    def test_sender_no_leading_at(self) -> None:
        """``alice:example.com`` (no @) returned unchanged as localpart."""
        result = project_matrix_attribution({"sender": "alice:example.com"})
        assert result["source_sender_short_label"] == "alice:example.com"

    # -- mmrelay field coexistence --

    def test_mmrelay_longname_shortname_ignored(self) -> None:
        """External mmrelay longname/shortname keys in native_data do not
        leak into generic Matrix sender fields. Matrix-native sender and
        displayname are authoritative."""
        result = project_matrix_attribution(
            {
                "sender": "@alice:example.com",
                "displayname": "Alice Matrix",
                "longname": "Alice Meshtastic",
                "shortname": "ALM",
                "from_id": "!1234",
                "meshtastic_longname": "Alice Wire",
                "meshtastic_shortname": "AW",
            }
        )
        assert result["source_sender_id"] == "@alice:example.com"
        assert result["source_sender_handle"] == "@alice:example.com"
        assert result["source_sender_label"] == "Alice Matrix"
        assert result["source_sender_short_label"] == "alice"

    def test_mmrelay_fields_no_displayname(self) -> None:
        """mmrelay keys present but no displayname — label stays None,
        short_label is the Matrix localpart (not the mmrelay shortname)."""
        result = project_matrix_attribution(
            {
                "sender": "@bob:matrix.org",
                "longname": "Bob Meshtastic",
                "shortname": "BB",
            }
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "bob"

    # -- return shape --

    def test_return_keys(self) -> None:
        result = project_matrix_attribution({"sender": "@alice:example.com"})
        assert set(result.keys()) == {
            "source_sender_id",
            "source_sender_handle",
            "source_sender_label",
            "source_sender_short_label",
        }


# ===================================================================
# project_matrix_sender — direct variant edge cases
# ===================================================================


class TestProjectMatrixSenderDirect:
    """Edge cases for the pre-split-args projection variant."""

    def test_mxid_at_colon_empty_localpart(self) -> None:
        """``@:`` — empty localpart falls through to full mxid as label."""
        result = project_matrix_sender("@:", None)
        assert result.sender_id == "@:"
        assert result.sender_handle == "@:"
        # displayname is None, localpart is "" (falsy), falls to mxid.
        assert result.sender_label == "@:"
        assert result.sender_short_label is None

    def test_mxid_at_colon_with_displayname(self) -> None:
        result = project_matrix_sender("@:", "Server Guest")
        assert result.sender_id == "@:"
        assert result.sender_label == "Server Guest"
        assert result.sender_short_label is None

    def test_none_mxid_empty_displayname(self) -> None:
        """None mxid with empty displayname — label is None, not 'None'."""
        result = project_matrix_sender(None, "")
        assert result.sender_id is None
        assert result.sender_handle is None
        assert result.sender_label is None
        assert result.sender_short_label is None
