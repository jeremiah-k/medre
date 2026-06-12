"""Focused unit tests for relay attribution model, extraction, and prefix
formatting.

Covers:
- RelayAttribution immutability and default construction.
- Generic preferred formatter variables ({sender}, {sender_short}, etc.).
- Old Meshtastic-era variables ({from_id}, {longname}, {shortname},
  {shortname5}) are now unknown placeholders.
- None coalescing to empty string; is-not-None preservation for labels.
- Unknown-placeholder policy (leave unchanged, set error).
- Brace / format edge cases (unmatched braces, empty template).
- Deterministic output.
- Extraction for Matrix, Meshtastic, MeshCore, LXMF shaped native metadata.
- Missing fields do not crash extraction or formatting.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
    NativeRef,
    RoutingMetadata,
)
from medre.core.rendering.attribution import (
    RelayAttribution,
    extract_relay_attribution,
    format_relay_prefix,
)

# ===================================================================
# Helpers
# ===================================================================


def _make_event(
    source_adapter: str = "source-adapter",
    source_channel_id: str | None = "ch-0",
    native_data: dict[str, object] | None = None,
    source_native_ref: NativeRef | None = None,
    route_trace: tuple[str, ...] = (),
    source_transport_id: str = "transport-1",
) -> CanonicalEvent:
    """Create a minimal canonical event for attribution tests."""
    metadata = EventMetadata(
        native=NativeMetadata(data=native_data) if native_data is not None else None,
        routing=RoutingMetadata(route_trace=route_trace) if route_trace else None,
    )
    return CanonicalEvent(
        event_id="evt-attrib-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2025, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "test message"},
        metadata=metadata,
        source_native_ref=source_native_ref,
    )


# ===================================================================
# RelayAttribution model tests
# ===================================================================


class TestRelayAttributionModel:
    """RelayAttribution is a frozen dataclass with sensible defaults."""

    def test_default_construction_all_none(self) -> None:
        attr = RelayAttribution()
        assert attr.source_adapter_id is None
        assert attr.source_platform is None
        assert attr.source_sender_id is None
        assert attr.source_sender_label is None
        assert attr.source_sender_short_label is None
        assert attr.source_sender_handle is None
        assert attr.route_id is None

    def test_frozen_immutability(self) -> None:
        attr = RelayAttribution(source_adapter_id="test")
        with pytest.raises(AttributeError):
            attr.source_adapter_id = "changed"  # type: ignore[misc]

    def test_full_construction(self) -> None:
        attr = RelayAttribution(
            source_adapter_id="matrix-1",
            source_platform="matrix",
            source_transport="transport-x",
            source_sender_id="@alice:matrix.org",
            source_display_name="Alice",
            source_sender_label="Alice",
            source_sender_short_label="alice",
            source_room_or_channel="!room:matrix.org",
            source_native_message_id="$msg1",
            source_native_channel_id="!room:matrix.org",
            route_id="route-1",
        )
        assert attr.source_adapter_id == "matrix-1"
        assert attr.source_platform == "matrix"
        assert attr.source_sender_id == "@alice:matrix.org"
        assert attr.source_sender_label == "Alice"
        assert attr.source_sender_short_label == "alice"

    def test_equality(self) -> None:
        a = RelayAttribution(source_adapter_id="x", source_platform="matrix")
        b = RelayAttribution(source_adapter_id="x", source_platform="matrix")
        assert a == b

    def test_inequality(self) -> None:
        a = RelayAttribution(source_adapter_id="x")
        b = RelayAttribution(source_adapter_id="y")
        assert a != b


# ===================================================================
# Generic preferred formatter variables
# ===================================================================


class TestGenericPreferredVariables:
    """Generic platform-neutral formatter variables render correctly."""

    def _full_attr(self) -> RelayAttribution:
        return RelayAttribution(
            source_adapter_id="matrix-1",
            source_platform="matrix",
            source_transport="transport-x",
            source_sender_id="@alice:matrix.org",
            source_sender_label="Alice Wonderland",
            source_sender_short_label="alice",
            source_sender_handle="@alice",
            source_display_name="Alice",
            source_room_or_channel="!room:matrix.org",
            source_origin_label="East Meshtastic",
            source_native_message_id="$msg1",
            source_native_channel_id="!room:matrix.org",
            route_id="route-42",
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            # Preferred generic aliases
            ("sender", "Alice Wonderland"),
            ("sender_short", "alice"),
            ("sender_id", "@alice:matrix.org"),
            ("sender_handle", "@alice"),
            ("platform", "matrix"),
            ("route_id", "route-42"),
            ("channel", "!room:matrix.org"),
            ("origin_label", "East Meshtastic"),
            # Canonical field names
            ("source_adapter_id", "matrix-1"),
            ("source_platform", "matrix"),
            ("source_transport", "transport-x"),
            ("source_sender_id", "@alice:matrix.org"),
            ("source_sender_label", "Alice Wonderland"),
            ("source_sender_short_label", "alice"),
            ("source_sender_handle", "@alice"),
            ("source_display_name", "Alice"),
            ("source_room_or_channel", "!room:matrix.org"),
            ("source_origin_label", "East Meshtastic"),
            ("source_native_message_id", "$msg1"),
            ("source_native_channel_id", "!room:matrix.org"),
        ],
    )
    def test_single_variable(self, name: str, expected: str) -> None:
        result = format_relay_prefix("{" + name + "}", self._full_attr())
        assert result.rendered_prefix == expected
        assert result.variables_used == (name,)
        assert not result.missing_variables
        assert not result.unknown_variables
        assert result.formatting_error is None

    def test_multiple_generic_variables(self) -> None:
        result = format_relay_prefix("[{sender}/{origin_label}]: ", self._full_attr())
        assert result.rendered_prefix == "[Alice Wonderland/East Meshtastic]: "
        assert "sender" in result.variables_used
        assert "origin_label" in result.variables_used

    def test_sender_and_sender_short(self) -> None:
        attr = RelayAttribution(
            source_sender_label="Operator",
            source_sender_short_label="Op",
        )
        result = format_relay_prefix("{sender} ({sender_short})", attr)
        assert result.rendered_prefix == "Operator (Op)"

    def test_sender_handle(self) -> None:
        attr = RelayAttribution(source_sender_handle="@bob:matrix.org")
        result = format_relay_prefix("{sender_handle}", attr)
        assert result.rendered_prefix == "@bob:matrix.org"

    def test_platform_variable(self) -> None:
        attr = RelayAttribution(source_platform="meshtastic")
        result = format_relay_prefix("[{platform}]", attr)
        assert result.rendered_prefix == "[meshtastic]"

    def test_channel_variable(self) -> None:
        attr = RelayAttribution(source_room_or_channel="!room:server")
        result = format_relay_prefix("{channel}", attr)
        assert result.rendered_prefix == "!room:server"

    def test_route_id_variable(self) -> None:
        attr = RelayAttribution(route_id="route-99")
        result = format_relay_prefix("{route_id}", attr)
        assert result.rendered_prefix == "route-99"


# ===================================================================
# Old Meshtastic-era variables are now unknown
# ===================================================================


class TestOldVariablesAreUnknown:
    """Old Meshtastic-era template variables are no longer known.

    {from_id}, {longname}, {shortname}, {shortname5} are NOT generic
    formatter aliases.  They pass through unchanged as unknown
    placeholders.
    """

    @pytest.mark.parametrize(
        "name",
        ["from_id", "longname", "shortname", "shortname5", "meshnet_name"],
    )
    def test_old_variable_is_unknown(self, name: str) -> None:
        attr = RelayAttribution(
            source_sender_id="!aabb",
            source_sender_label="RadioOp",
            source_sender_short_label="RO",
            source_origin_label="East",
        )
        result = format_relay_prefix("{" + name + "}", attr)
        assert result.rendered_prefix == "{" + name + "}"
        assert name in result.unknown_variables
        assert result.formatting_error is not None

    @pytest.mark.parametrize(
        "name",
        ["from_id", "longname", "shortname", "shortname5", "meshnet_name"],
    )
    def test_old_vars_still_unknown_after_extraction(self, name: str) -> None:
        """Old variables remain unknown even after Matrix extraction populates
        sender_handle.  Regression guard against broad alias reintroduction."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@alice:matrix.org",
                "displayname": "Alice",
            },
        )
        attr = extract_relay_attribution(event)
        # sender_handle should be populated
        assert attr.source_sender_handle == "@alice:matrix.org"
        # But old vars are still unknown in templates
        result = format_relay_prefix("{" + name + "}", attr)
        assert result.rendered_prefix == "{" + name + "}"
        assert name in result.unknown_variables


# ===================================================================
# Explicit empty sender_short_label
# ===================================================================


class TestExplicitEmptySenderShortLabel:
    """Explicitly empty sender_short_label is preserved, not fallback-derived."""

    def test_explicit_empty_sender_short_label_renders_empty(self) -> None:
        """Explicit empty string is not derived from anything."""
        attr = RelayAttribution(
            source_sender_short_label="",
            source_sender_label="Alice",
        )
        result = format_relay_prefix("{sender_short}", attr)
        assert result.rendered_prefix == ""
        assert "sender_short" in result.missing_variables


# ===================================================================
# None coalescing
# ===================================================================


class TestNoneCoalescing:
    """None values format as empty string, never the literal 'None'."""

    def test_none_renders_empty(self) -> None:
        attr = RelayAttribution(source_sender_label=None)
        result = format_relay_prefix("[{sender}]", attr)
        assert result.rendered_prefix == "[]"
        assert "None" not in result.rendered_prefix

    def test_all_none_renders_empty(self) -> None:
        attr = RelayAttribution()
        result = format_relay_prefix("{source_sender_id}", attr)
        assert result.rendered_prefix == ""
        assert "source_sender_id" in result.missing_variables

    def test_partial_none(self) -> None:
        attr = RelayAttribution(
            source_sender_label="Bob",
            source_origin_label=None,
        )
        result = format_relay_prefix("[{sender}/{origin_label}]", attr)
        assert result.rendered_prefix == "[Bob/]"
        assert "origin_label" in result.missing_variables
        assert "sender" not in result.missing_variables

    def test_missing_sender_fields_no_literal_none(self) -> None:
        """Missing sender fields never render as the string 'None'."""
        attr = RelayAttribution()
        result = format_relay_prefix(
            "{sender}/{sender_short}/{sender_id}/{sender_handle}", attr
        )
        assert result.rendered_prefix == "///"
        assert "None" not in result.rendered_prefix


# ===================================================================
# Unknown placeholder policy
# ===================================================================


class TestUnknownPlaceholderPolicy:
    """Unknown placeholders are left unchanged and recorded with error."""

    def test_unknown_left_unchanged(self) -> None:
        attr = RelayAttribution(source_sender_label="Alice")
        result = format_relay_prefix("[{sender}/{bogus}]", attr)
        assert result.rendered_prefix == "[Alice/{bogus}]"
        assert result.unknown_variables == ("bogus",)
        assert result.formatting_error is not None
        assert "bogus" in result.formatting_error

    def test_multiple_unknowns(self) -> None:
        attr = RelayAttribution()
        result = format_relay_prefix("{foo}{bar}", attr)
        assert result.rendered_prefix == "{foo}{bar}"
        assert "foo" in result.unknown_variables
        assert "bar" in result.unknown_variables

    def test_known_and_unknown_mix(self) -> None:
        attr = RelayAttribution(source_sender_label="Bob")
        result = format_relay_prefix("{sender}-{unknown}", attr)
        assert result.rendered_prefix == "Bob-{unknown}"
        assert "sender" in result.variables_used
        assert "unknown" in result.unknown_variables

    def test_unknown_no_error_when_all_known(self) -> None:
        attr = RelayAttribution(source_sender_id="user1")
        result = format_relay_prefix("[{sender_id}]", attr)
        assert result.formatting_error is None


# ===================================================================
# Brace / format edge cases
# ===================================================================


class TestBraceEdgeCases:
    """Unmatched braces, empty template, no placeholders."""

    def test_empty_template(self) -> None:
        result = format_relay_prefix("", RelayAttribution())
        assert result.rendered_prefix == ""
        assert not result.variables_used

    def test_no_placeholders(self) -> None:
        result = format_relay_prefix("[static]: ", RelayAttribution())
        assert result.rendered_prefix == "[static]: "
        assert not result.variables_used

    def test_unmatched_open_brace(self) -> None:
        """Unmatched braces that don't match the {name} pattern pass through."""
        result = format_relay_prefix(
            "{sender} [", RelayAttribution(source_sender_label="A")
        )
        assert result.rendered_prefix == "A ["

    def test_double_braces_not_special(self) -> None:
        """Double braces are not escape sequences — they're just text."""
        result = format_relay_prefix(
            "{{sender}}", RelayAttribution(source_sender_label="X")
        )
        assert result.rendered_prefix == "{X}"

    def test_nested_braces_not_matched(self) -> None:
        """{name{inner}} is not a valid placeholder."""
        result = format_relay_prefix("{a{b}}", RelayAttribution())
        assert result.rendered_prefix == "{a{b}}"
        assert result.unknown_variables == ("b",)

    def test_template_with_literal_text(self) -> None:
        attr = RelayAttribution(source_sender_short_label="SN")
        result = format_relay_prefix(">>{sender_short}<<", attr)
        assert result.rendered_prefix == ">>SN<<"


# ===================================================================
# Determinism
# ===================================================================


class TestDeterminism:
    """Same inputs always produce the same output."""

    def test_deterministic_repeated(self) -> None:
        attr = RelayAttribution(
            source_sender_label="User1",
            source_origin_label="net",
        )
        template = "[{sender}/{origin_label}]: "
        r1 = format_relay_prefix(template, attr)
        r2 = format_relay_prefix(template, attr)
        assert r1.rendered_prefix == r2.rendered_prefix
        assert r1.variables_used == r2.variables_used
        assert r1.missing_variables == r2.missing_variables
        assert r1.formatting_error == r2.formatting_error

    def test_different_attrs_different_output(self) -> None:
        a1 = RelayAttribution(source_sender_label="Alice")
        a2 = RelayAttribution(source_sender_label="Bob")
        r1 = format_relay_prefix("{sender}", a1)
        r2 = format_relay_prefix("{sender}", a2)
        assert r1.rendered_prefix != r2.rendered_prefix


# ===================================================================
# PrefixFormatterResult structure
# ===================================================================


class TestPrefixFormatterResult:
    """PrefixFormatterResult is frozen and well-structured."""

    def test_frozen(self) -> None:
        result = format_relay_prefix("", RelayAttribution())
        with pytest.raises(AttributeError):
            result.rendered_prefix = "changed"  # type: ignore[misc]

    def test_template_used_recorded(self) -> None:
        template = "[{sender}]: "
        result = format_relay_prefix(template, RelayAttribution())
        assert result.template_used == template

    def test_missing_variables_tracked(self) -> None:
        attr = RelayAttribution(source_sender_label=None, source_sender_id=None)
        result = format_relay_prefix("{sender}-{sender_id}", attr)
        assert "sender" in result.missing_variables
        assert "sender_id" in result.missing_variables

    def test_empty_value_is_missing(self) -> None:
        attr = RelayAttribution(source_sender_label="")
        result = format_relay_prefix("{sender}", attr)
        assert "sender" in result.missing_variables


# ===================================================================
# Extraction: Matrix
# ===================================================================


class TestExtractionMatrix:
    """Matrix event extraction produces correct attribution fields."""

    def test_basic_matrix_extraction(self) -> None:
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@alice:matrix.org",
                "displayname": "Alice",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "matrix"
        assert attr.source_sender_id == "@alice:matrix.org"
        assert attr.source_display_name == "Alice"
        assert attr.source_sender_label == "Alice"
        assert attr.source_sender_short_label == "alice"

    def test_mxid_localpart_fallback(self) -> None:
        """When no displayname, short label falls back to MXID localpart."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@bob:matrix.org",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_short_label == "bob"
        assert attr.source_display_name is None

    def test_matrix_sender_renders_via_generic(self) -> None:
        """Matrix display name renders via generic {sender} variable."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@alice:matrix.org",
                "displayname": "Alice Display",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender}", attr)
        assert result.rendered_prefix == "Alice Display"

    def test_matrix_sender_handle_populated_from_mxid(self) -> None:
        """Matrix extraction populates source_sender_handle from sender MXID."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@carol:matrix.org",
                "displayname": "Carol",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_handle == "@carol:matrix.org"

    def test_matrix_sender_handle_renders_via_template(self) -> None:
        """{sender_handle} renders the MXID through the formatter."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@dave:example.com",
                "displayname": "Dave",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_handle}", attr)
        assert result.rendered_prefix == "@dave:example.com"
        assert "sender_handle" in result.variables_used
        assert not result.missing_variables

    def test_matrix_missing_sender_handle_renders_empty(self) -> None:
        """When no sender in native data, {sender_handle} renders empty."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_handle is None
        result = format_relay_prefix("{sender_handle}", attr)
        assert result.rendered_prefix == ""
        assert "sender_handle" in result.missing_variables


# ===================================================================
# Extraction: Meshtastic
# ===================================================================


class TestExtractionMeshtastic:
    """Meshtastic event extraction produces correct attribution fields."""

    def test_basic_meshtastic_extraction(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "Radio User",
                "shortname": "RU",
                "from_id": "!aabbccdd",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshtastic"
        assert attr.source_sender_id == "!aabbccdd"
        assert attr.source_sender_label == "Radio User"
        assert attr.source_sender_short_label == "RU"

    def test_sender_short_truncates_to_five(self) -> None:
        """sender_short_label is stored as-is (no auto-truncation)."""
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "shortname": "LongName",
                "from_id": "!1234",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_short}", attr)
        assert result.rendered_prefix == "LongName"

    def test_sender_id_fallback_when_no_shortname(self) -> None:
        """When no shortname, sender_id is still available."""
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "from_id": "!abcdef123456",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_id}", attr)
        assert result.rendered_prefix == "!abcdef123456"

    def test_missing_longname(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={"from_id": "!node1"},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_label is None
        assert attr.source_sender_id == "!node1"


# ===================================================================
# Extraction: MeshCore
# ===================================================================


class TestExtractionMeshCore:
    """MeshCore event extraction produces correct attribution fields."""

    def test_basic_meshcore_extraction(self) -> None:
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "pubkey_prefix": "a1b2c3",
                "channel_idx": "2",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshcore"
        assert attr.source_sender_id == "a1b2c3"
        assert attr.source_native_channel_id == "2"

    def test_no_display_name(self) -> None:
        """MeshCore has no display name by default."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={"pubkey_prefix": "deadbeef"},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_display_name is None
        assert attr.source_sender_label is None


# ===================================================================
# Extraction: LXMF
# ===================================================================


class TestExtractionLxmf:
    """LXMF event extraction produces correct attribution fields."""

    def test_basic_lxmf_extraction(self) -> None:
        event = _make_event(
            source_adapter="lxmf-node",
            native_data={
                "source_hash": "abc123def",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "lxmf"
        assert attr.source_sender_id == "abc123def"

    def test_no_display_name(self) -> None:
        event = _make_event(
            source_adapter="lxmf-node",
            native_data={"source_hash": "xyz789"},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_display_name is None


# ===================================================================
# Extraction: common fields
# ===================================================================


class TestExtractionCommon:
    """Common fields are populated regardless of platform."""

    def test_source_adapter_id(self) -> None:
        event = _make_event(source_adapter="my-adapter")
        attr = extract_relay_attribution(event)
        assert attr.source_adapter_id == "my-adapter"

    def test_source_transport(self) -> None:
        event = _make_event(source_transport_id="tcp-conn-1")
        attr = extract_relay_attribution(event)
        assert attr.source_transport == "tcp-conn-1"

    def test_source_room_or_channel(self) -> None:
        event = _make_event(source_channel_id="!room:server")
        attr = extract_relay_attribution(event)
        assert attr.source_room_or_channel == "!room:server"

    def test_source_native_ref(self) -> None:
        ref = NativeRef(
            adapter="matrix-1",
            native_channel_id="!room:server",
            native_message_id="$event123",
        )
        event = _make_event(source_native_ref=ref)
        attr = extract_relay_attribution(event)
        assert attr.source_native_message_id == "$event123"
        assert attr.source_native_channel_id == "!room:server"

    def test_route_id_from_route_trace(self) -> None:
        event = _make_event(route_trace=("route-1", "route-2"))
        attr = extract_relay_attribution(event)
        assert attr.route_id == "route-1"

    def test_route_id_explicit_overrides(self) -> None:
        event = _make_event(route_trace=("route-1",))
        attr = extract_relay_attribution(event, route_id="route-override")
        assert attr.route_id == "route-override"

    def test_no_native_metadata(self) -> None:
        event = _make_event(native_data=None)
        attr = extract_relay_attribution(event)
        assert attr.source_sender_id is None
        assert attr.source_sender_label is None

    def test_empty_native_data(self) -> None:
        event = _make_event(native_data={})
        attr = extract_relay_attribution(event)
        assert attr.source_sender_id is None

    def test_unknown_platform(self) -> None:
        event = _make_event(source_adapter="unknown-adapter", native_data={})
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None

    def test_platform_explicit_override(self) -> None:
        event = _make_event(source_adapter="custom-adapter")
        attr = extract_relay_attribution(event, source_platform="matrix")
        assert attr.source_platform == "matrix"

    def test_source_native_ref_wins_over_meshcore_packet_id(self) -> None:
        """source_native_ref IDs are authoritative over platform metadata."""
        ref = NativeRef(
            adapter="meshcore-1",
            native_channel_id="ch-envelope",
            native_message_id="$envelope-msg-id",
        )
        event = _make_event(
            source_adapter="meshcore-node",
            source_native_ref=ref,
            native_data={
                "meshcore.packet_id": 99999,
                "meshcore.channel": 7,
                "meshcore.pubkey_prefix": "pk1",
            },
        )
        attr = extract_relay_attribution(event)
        # source_native_ref values must win, not the raw meshcore metadata.
        assert attr.source_native_message_id == "$envelope-msg-id"
        assert attr.source_native_channel_id == "ch-envelope"


# ===================================================================
# Integration: extraction + formatting
# ===================================================================


class TestExtractionAndFormatting:
    """End-to-end extraction then formatting produces expected output."""

    def test_meshtastic_full_pipeline(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "RadioOp",
                "shortname": "RO",
                "from_id": "!11223344",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("[{sender}/{origin_label}]: ", attr)
        assert result.rendered_prefix == "[RadioOp/]: "
        assert "origin_label" in result.missing_variables
        assert result.formatting_error is None

    def test_meshtastic_compat_now_unknown(self) -> None:
        """Old compat alias {longname} is now unknown."""
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "RadioOp",
                "shortname": "RO",
                "from_id": "!11223344",
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("[{longname}/{origin_label}]: ", attr)
        # {longname} is unknown → left as literal
        assert result.rendered_prefix == "[{longname}/]: "
        assert "longname" in result.unknown_variables

    def test_meshcore_sender_short_template(self) -> None:
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={"pubkey_prefix": "aabbcc"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_short}: ", attr)
        # No short label → sender_short is empty
        assert result.rendered_prefix == ": "
        assert "sender_short" in result.missing_variables

    def test_matrix_sender_id_template(self) -> None:
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={"sender": "@dave:matrix.org"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_id}: ", attr)
        assert result.rendered_prefix == "@dave:matrix.org: "

    def test_lxmf_missing_display_no_crash(self) -> None:
        event = _make_event(
            source_adapter="lxmf-node",
            native_data={"source_hash": "hash1"},
        )
        attr = extract_relay_attribution(event)
        # sender_label is None -> renders empty
        result = format_relay_prefix("[{sender}]: ", attr)
        assert result.rendered_prefix == "[]: "
        assert "sender" in result.missing_variables

    def test_lxmf_compat_longname_now_unknown(self) -> None:
        """Old compat alias {longname} is now unknown for LXMF."""
        event = _make_event(
            source_adapter="lxmf-node",
            native_data={"source_hash": "hash1"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("[{longname}]: ", attr)
        # {longname} is unknown → left as literal
        assert result.rendered_prefix == "[{longname}]: "
        assert "longname" in result.unknown_variables


# ===================================================================
# Extraction: MeshCore with real codec namespaced keys
# ===================================================================


class TestExtractionMeshCoreNamespaced:
    """MeshCore extraction with namespaced keys as produced by MeshCoreCodec."""

    def test_namespaced_pubkey_prefix_as_sender_id(self) -> None:
        """meshcore.pubkey_prefix populates source_sender_id."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.pubkey_prefix": "a1b2c3",
                "meshcore.sender_id": "a1b2c3",
                "meshcore.channel": 2,
                "meshcore.packet_id": 42,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshcore"
        assert attr.source_sender_id == "a1b2c3"
        assert attr.source_native_channel_id == "2"
        assert attr.source_native_message_id == "42"

    def test_namespaced_pubkey_prefix_preferred_over_sender_id(self) -> None:
        """meshcore.pubkey_prefix wins over meshcore.sender_id."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.pubkey_prefix": "preferred",
                "meshcore.sender_id": "fallback",
                "meshcore.channel": 0,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_id == "preferred"

    def test_namespaced_sender_id_fallback(self) -> None:
        """When meshcore.pubkey_prefix absent, meshcore.sender_id used."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.sender_id": "sender-val",
                "meshcore.channel": 1,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_id == "sender-val"

    def test_namespaced_channel_preferred_over_bare(self) -> None:
        """meshcore.channel preferred over bare channel_idx."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.pubkey_prefix": "pk1",
                "meshcore.channel": 5,
                "channel_idx": 99,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_native_channel_id == "5"

    def test_namespaced_packet_id_extracted(self) -> None:
        """meshcore.packet_id populates source_native_message_id."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.pubkey_prefix": "pk",
                "meshcore.channel": 0,
                "meshcore.packet_id": 12345,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_native_message_id == "12345"

    def test_full_namespaced_pipeline_format(self) -> None:
        """Namespaced MeshCore data produces correct prefix via formatter."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.pubkey_prefix": "deadbeef",
                "meshcore.sender_id": "deadbeef",
                "meshcore.channel": 3,
                "meshcore.packet_id": 999,
            },
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix(
            "{sender_id}/{source_sender_id}/{source_native_channel_id}", attr
        )
        assert result.rendered_prefix == "deadbeef/deadbeef/3"


# ===================================================================
# Extraction: MeshCore bare fixture keys (backward compat)
# ===================================================================


class TestExtractionMeshCoreBareFixture:
    """Bare unnamespaced keys still work for test fixture compatibility."""

    def test_bare_pubkey_prefix_fallback(self) -> None:
        """Bare pubkey_prefix still works when no namespaced key present."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "pubkey_prefix": "bare-pk",
                "channel_idx": "1",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_sender_id == "bare-pk"
        assert attr.source_native_channel_id == "1"

    def test_bare_sender_short_from_pubkey(self) -> None:
        """sender_short is empty when MeshCore has no short label."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={"pubkey_prefix": "aabbccdd"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{sender_short}[MC]: ", attr)
        assert result.rendered_prefix == "[MC]: "


# ===================================================================
# Native-metadata-key platform detection
# ===================================================================


class TestPlatformDetectionFromNativeKeys:
    """Arbitrary adapter IDs produce correct platform from native metadata keys."""

    def test_meshcore_detected_from_namespaced_keys(self) -> None:
        """Adapter "radio-a" with MeshCore native keys -> platform=meshcore."""
        event = _make_event(
            source_adapter="radio-a",
            native_data={
                "meshcore.pubkey_prefix": "pk1",
                "meshcore.channel": 0,
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshcore"
        assert attr.source_sender_id == "pk1"

    def test_meshtastic_detected_from_native_keys(self) -> None:
        """Adapter "relay" with Meshtastic native keys -> platform=meshtastic."""
        event = _make_event(
            source_adapter="relay",
            native_data={
                "longname": "Base Station",
                "shortname": "BS",
                "from_id": "!aabbcc",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshtastic"
        assert attr.source_sender_id == "!aabbcc"

    def test_matrix_detected_from_native_keys(self) -> None:
        """Adapter "base" with Matrix native keys -> platform=matrix."""
        event = _make_event(
            source_adapter="base",
            native_data={
                "sender": "@alice:matrix.org",
                "event_id": "$event123",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "matrix"
        assert attr.source_sender_id == "@alice:matrix.org"

    def test_lxmf_detected_from_native_keys(self) -> None:
        """Adapter "node-x" with LXMF native keys -> platform=lxmf."""
        event = _make_event(
            source_adapter="node-x",
            native_data={
                "source_hash": "abc123",
                "destination_hash": "def456",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "lxmf"
        assert attr.source_sender_id == "abc123"

    def test_adapter_id_wins_over_native_keys(self) -> None:
        """Adapter-ID heuristic takes priority over native key detection."""
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "sender": "@intruder:matrix.org",
            },
        )
        attr = extract_relay_attribution(event)
        # adapter ID wins -> meshtastic, not matrix
        assert attr.source_platform == "meshtastic"

    def test_explicit_platform_overrides_all(self) -> None:
        """Explicit source_platform overrides both heuristics."""
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={"sender": "@alice:matrix.org"},
        )
        attr = extract_relay_attribution(event, source_platform="lxmf")
        assert attr.source_platform == "lxmf"

    def test_no_native_data_no_platform(self) -> None:
        """Unknown adapter with no native data -> platform=None."""
        event = _make_event(source_adapter="unknown-thing", native_data=None)
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None

    def test_empty_native_data_no_platform(self) -> None:
        """Unknown adapter with empty native data -> platform=None."""
        event = _make_event(source_adapter="unknown-thing", native_data={})
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None

    def test_unrecognizable_native_keys_no_platform(self) -> None:
        """Adapter with unrecognized native keys -> platform=None."""
        event = _make_event(
            source_adapter="unknown-thing",
            native_data={"foo": "bar", "baz": 42},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None


class TestPlatformDetectionMixedKeys:
    """Matrix keys win over Meshtastic bare keys when both are present.

    Matrix native data may be enriched with Meshtastic-style bare keys
    (``longname``, ``shortname``) by the relay pipeline.  The
    Matrix-specific keys (``sender``, ``event_id``, ``room_id``) are a
    stronger signal and MUST be detected first.
    """

    def test_matrix_wins_over_meshtastic_when_both_present(self) -> None:
        """Native data with both Matrix and Meshtastic keys -> platform=matrix."""
        event = _make_event(
            source_adapter="bridge",
            native_data={
                "sender": "@alice:matrix.org",
                "event_id": "$event123",
                "longname": "Alice Radio",
                "shortname": "AR",
                "from_id": "!aabbcc",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "matrix"
        assert attr.source_sender_id == "@alice:matrix.org"

    def test_matrix_sender_with_meshtastic_longname_detects_matrix(self) -> None:
        """Only ``sender`` (Matrix) + ``longname`` (Meshtastic) -> matrix."""
        event = _make_event(
            source_adapter="relay",
            native_data={
                "sender": "@bob:matrix.org",
                "longname": "Bob Node",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "matrix"

    def test_matrix_room_id_with_meshtastic_from_id_detects_matrix(self) -> None:
        """``room_id`` (Matrix) + ``from_id`` (Meshtastic bare) -> matrix."""
        event = _make_event(
            source_adapter="bridge",
            native_data={
                "room_id": "!room:matrix.org",
                "from_id": "!node123",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "matrix"

    def test_pure_meshtastic_keys_still_detected(self) -> None:
        """Only Meshtastic bare keys (no Matrix keys) -> meshtastic."""
        event = _make_event(
            source_adapter="bridge",
            native_data={
                "longname": "Base Station",
                "shortname": "BS",
                "from_id": "!aabbcc",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshtastic"


# ===================================================================
# MeshCore -> formatter end-to-end with real codec data shape
# ===================================================================


class TestMeshCoreRealCodecEndToEnd:
    """Events shaped exactly like MeshCoreCodec produces work end-to-end."""

    def test_real_codec_shape_prefix(self) -> None:
        """Full codec-shaped native data produces {sender_id}/{source_sender_id}/
        {source_native_channel_id} through the shared formatter."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={
                "meshcore.packet_id": 42,
                "meshcore.sender_id": "abcdef",
                "meshcore.channel": 3,
                "meshcore.pubkey_prefix": "abcdef",
                "meshcore.txt_type": 1,
                "meshcore.is_direct_message": False,
                "meshcore.classification": {
                    "action": "relay",
                    "category": "text",
                    "reason": "public_text",
                    "is_direct_message": False,
                    "routeable": True,
                },
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshcore"
        assert attr.source_sender_id == "abcdef"
        assert attr.source_native_channel_id == "3"
        assert attr.source_native_message_id == "42"

        result = format_relay_prefix(
            "{sender_id}/{source_sender_id}/{source_native_channel_id}", attr
        )
        assert result.rendered_prefix == "abcdef/abcdef/3"

    def test_real_codec_shape_with_arbitrary_adapter_id(self) -> None:
        """Arbitrary adapter ID still resolves via native-key detection."""
        event = _make_event(
            source_adapter="radio-a",
            native_data={
                "meshcore.packet_id": 100,
                "meshcore.sender_id": "cafefe",
                "meshcore.channel": 1,
                "meshcore.pubkey_prefix": "cafefe",
                "meshcore.txt_type": 0,
                "meshcore.is_direct_message": False,
                "meshcore.classification": {
                    "action": "relay",
                    "category": "text",
                    "reason": "public_text",
                    "is_direct_message": False,
                    "routeable": True,
                },
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform == "meshcore"
        assert attr.source_sender_id == "cafefe"

        result = format_relay_prefix("{sender_id}[MC]: ", attr)
        assert result.rendered_prefix == "cafefe[MC]: "


# ===================================================================
# origin_label alias (preferred)
# ===================================================================


class TestOriginLabelAlias:
    """origin_label alias maps to source_origin_label."""

    def test_origin_label_resolves_when_set(self) -> None:
        attr = RelayAttribution(source_origin_label="East Meshtastic")
        result = format_relay_prefix("{origin_label}", attr)
        assert result.rendered_prefix == "East Meshtastic"
        assert "origin_label" in result.variables_used
        assert not result.missing_variables
        assert result.formatting_error is None

    def test_origin_label_empty_when_none(self) -> None:
        attr = RelayAttribution(source_origin_label=None)
        result = format_relay_prefix("{origin_label}", attr)
        assert result.rendered_prefix == ""
        assert "origin_label" in result.missing_variables

    def test_origin_label_empty_when_default(self) -> None:
        attr = RelayAttribution()
        result = format_relay_prefix("{origin_label}", attr)
        assert result.rendered_prefix == ""
        assert "None" not in result.rendered_prefix

    def test_origin_label_with_value_renders(self) -> None:
        attr = RelayAttribution(source_origin_label="West Hub")
        result = format_relay_prefix("[{origin_label}]: ", attr)
        assert result.rendered_prefix == "[West Hub]: "

    def test_origin_label_with_other_vars(self) -> None:
        """{origin_label} works alongside other template variables."""
        attr = RelayAttribution(
            source_origin_label="East Meshtastic",
            source_sender_label="User1",
        )
        result = format_relay_prefix("[{origin_label}/{sender}]: ", attr)
        assert result.rendered_prefix == "[East Meshtastic/User1]: "
        assert "origin_label" in result.variables_used
        assert "sender" in result.variables_used

    def test_canonical_source_origin_label_direct(self) -> None:
        """Canonical name source_origin_label also resolves."""
        attr = RelayAttribution(source_origin_label="Direct Label")
        result = format_relay_prefix("{source_origin_label}", attr)
        assert result.rendered_prefix == "Direct Label"
        assert "source_origin_label" in result.variables_used
