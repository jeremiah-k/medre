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
from medre.adapters.matrix.event_shape import MATRIX_NATIVE_SCHEMA_VERSION


def _matrix_native(**matrix: object) -> dict[str, object]:
    """Build the supported versioned Matrix native-metadata shape."""
    matrix = {"schema_version": MATRIX_NATIVE_SCHEMA_VERSION, **matrix}
    return {"matrix": matrix}


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

    # -- sender display name present --

    def test_sender_display_name_present(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@alice:example.com",
                sender_display_name="Alice Liddell",
            )
        )
        assert result["source_sender_id"] == "@alice:example.com"
        assert result["source_sender_handle"] == "@alice:example.com"
        assert result["source_sender_label"] == "Alice Liddell"
        assert result["source_sender_short_label"] == "alice"

    def test_unversioned_display_name_alias_is_ignored(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@alice:example.com",
                display_name="Alice U",
            )
        )
        assert result["source_sender_label"] is None

    def test_sender_display_name_is_authoritative(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@alice:example.com",
                sender_display_name="Primary",
                displayname="Ignored",
                display_name="Ignored too",
            )
        )
        assert result["source_sender_label"] == "Primary"

    # -- sender display name missing (key absent) --

    def test_sender_display_name_key_absent(self) -> None:
        """When sender_display_name is absent, label is None.
        No localpart fallback in the dispatch-oriented projection."""
        result = project_matrix_attribution(_matrix_native(sender="@bob:matrix.org"))
        assert result["source_sender_id"] == "@bob:matrix.org"
        assert result["source_sender_handle"] == "@bob:matrix.org"
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "bob"

    # -- sender display name empty string --

    def test_sender_display_name_empty_string(self) -> None:
        """An explicit empty sender display name stays absent (None),
        never the literal string 'None' or ''."""
        result = project_matrix_attribution(
            _matrix_native(sender="@carol:server.org", sender_display_name="")
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "carol"

    def test_empty_sender_display_name_does_not_use_alias(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@carol:server.org",
                sender_display_name="",
                display_name="Ignored",
            )
        )
        assert result["source_sender_label"] is None

    def test_sender_display_name_and_aliases_empty(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@carol:server.org",
                sender_display_name="",
                displayname="",
                display_name="",
            )
        )
        assert result["source_sender_label"] is None

    # -- sender display name is Python None --

    def test_sender_display_name_explicit_none(self) -> None:
        """Python None must never render as the literal 'None'."""
        result = project_matrix_attribution(
            _matrix_native(sender="@dave:matrix.org", sender_display_name=None)
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "dave"

    def test_none_sender_display_name_does_not_use_alias(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(
                sender="@dave:matrix.org",
                sender_display_name=None,
                display_name="Ignored",
            )
        )
        assert result["source_sender_label"] is None

    # -- sender missing / None --

    def test_sender_key_absent(self) -> None:
        result = project_matrix_attribution(_matrix_native(sender_display_name="Ghost"))
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] == "Ghost"
        assert result["source_sender_short_label"] is None

    def test_sender_none(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(sender=None, sender_display_name="Ghost")
        )
        assert result["source_sender_id"] is None
        assert result["source_sender_handle"] is None
        assert result["source_sender_label"] == "Ghost"
        assert result["source_sender_short_label"] is None

    def test_sender_empty_string(self) -> None:
        result = project_matrix_attribution(
            _matrix_native(sender="", sender_display_name="Ghost")
        )
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
        result = project_matrix_attribution(_matrix_native(sender="@:"))
        assert result["source_sender_id"] == "@:"
        assert result["source_sender_handle"] == "@:"
        assert result["source_sender_short_label"] is None

    def test_sender_no_leading_at(self) -> None:
        """``alice:example.com`` (no @) returned unchanged as localpart."""
        result = project_matrix_attribution(_matrix_native(sender="alice:example.com"))
        assert result["source_sender_short_label"] == "alice:example.com"

    # -- mmrelay field coexistence --

    def test_mmrelay_longname_shortname_ignored(self) -> None:
        """External mmrelay longname/shortname keys in native_data do not
        leak into generic Matrix sender fields. Matrix-native sender and
        displayname are authoritative."""
        result = project_matrix_attribution(
            {
                "matrix": {
                    "schema_version": MATRIX_NATIVE_SCHEMA_VERSION,
                    "sender": "@alice:example.com",
                    "sender_display_name": "Alice Matrix",
                },
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
                "matrix": {
                    "schema_version": MATRIX_NATIVE_SCHEMA_VERSION,
                    "sender": "@bob:matrix.org",
                },
                "longname": "Bob Meshtastic",
                "shortname": "BB",
            }
        )
        assert result["source_sender_label"] is None
        assert result["source_sender_short_label"] == "bob"

    # -- return shape --

    def test_return_keys(self) -> None:
        result = project_matrix_attribution(_matrix_native(sender="@alice:example.com"))
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


# ===================================================================
# Dispatch: project_source_fields for Matrix native dicts
# ===================================================================


class TestDispatchProjection:
    """Dispatch-level integration: ``project_source_fields`` detects
    matrix and delegates to ``project_matrix_attribution``."""

    def test_dispatch_matrix_full_attribution(self) -> None:
        """``project_source_fields`` detects matrix from the adapter-id
        heuristic, delegates to ``project_matrix_attribution``, and wires
        all four sender fields plus ``source_platform``. Parallel to the
        LXMF, Meshtastic, and MeshCore dispatch tests."""
        from medre.adapters._attribution_dispatch import project_source_fields

        fields = project_source_fields(
            _matrix_native(
                sender="@alice:example.com",
                sender_display_name="Alice",
            ),
            source_adapter="matrix-1",
        )
        assert fields["source_platform"] == "matrix"
        assert fields["source_sender_id"] == "@alice:example.com"
        assert fields["source_sender_handle"] == "@alice:example.com"
        assert fields["source_sender_label"] == "Alice"
        assert fields["source_sender_short_label"] == "alice"


# ===================================================================
# Adapter-enrichment flow edge cases
# ===================================================================


class TestAdapterEnrichmentFlow:
    """Document how adapter-level display-name enrichment flows
    through the projection."""

    def test_mxid_as_displayname_flows_to_sender_label(self) -> None:
        """When the adapter writes the sender MXID into the
        ``sender_display_name`` field (its MXID-as-display-name fallback for live
        rendering), the projection returns that MXID as
        ``source_sender_label``. This documents the data flow the
        transport-native-identity-enrichment audit describes: the
        projection applies no MXID fallback of its own, but it reads
        whatever the adapter enriched into ``sender_display_name``, so
        in live rendering ``source_sender_label`` carries the MXID when
        no member display name exists."""
        result = project_matrix_attribution(
            _matrix_native(
                sender="@alice:example.com",
                sender_display_name="@alice:example.com",
            )
        )
        assert result["source_sender_label"] == "@alice:example.com"
        assert result["source_sender_id"] == "@alice:example.com"
        assert result["source_sender_handle"] == "@alice:example.com"
        assert result["source_sender_short_label"] == "alice"

    def test_whitespace_only_sender_display_name_becomes_label(self) -> None:
        """A whitespace-only sender display name (e.g. ``"   "``) is truthy in
        Python and non-empty after ``str()``, so it flows through as
        ``source_sender_label`` unchanged. The projection does not strip
        or reject whitespace-only display names.

        Whether whitespace-only should be treated as absent is a judgment
        call left to the project. The current behaviour preserves the
        whitespace string because ``_str`` returns it unchanged when the
        result of ``str(value)`` is non-empty. If
        the project decides whitespace-only should be treated as absent,
        the fix belongs in ``_str`` or the projection, not here."""
        result = project_matrix_attribution(
            _matrix_native(
                sender="@alice:example.com",
                sender_display_name="   ",
            )
        )
        assert result["source_sender_label"] == "   "
        assert result["source_sender_id"] == "@alice:example.com"
        assert result["source_sender_handle"] == "@alice:example.com"
        assert result["source_sender_short_label"] == "alice"
