"""Focused unit tests for relay attribution model, extraction, and prefix
formatting.

Covers:
- RelayAttribution immutability and default construction.
- All variable substitutions (canonical names + aliases).
- None coalescing to empty string.
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
            source_long_name="Alice",
            source_short_name="alice",
            source_short_name_5="alice",
            source_room_or_channel="!room:matrix.org",
            source_native_message_id="$msg1",
            source_native_channel_id="!room:matrix.org",
            route_id="route-1",
        )
        assert attr.source_adapter_id == "matrix-1"
        assert attr.source_platform == "matrix"
        assert attr.source_sender_id == "@alice:matrix.org"

    def test_equality(self) -> None:
        a = RelayAttribution(source_adapter_id="x", source_platform="matrix")
        b = RelayAttribution(source_adapter_id="x", source_platform="matrix")
        assert a == b

    def test_inequality(self) -> None:
        a = RelayAttribution(source_adapter_id="x")
        b = RelayAttribution(source_adapter_id="y")
        assert a != b


# ===================================================================
# Safe prefix formatter: all supported variables
# ===================================================================


class TestFormatRelayPrefixAllVariables:
    """Every canonical variable and alias renders correctly."""

    def _full_attr(self) -> RelayAttribution:
        return RelayAttribution(
            source_adapter_id="matrix-1",
            source_platform="matrix",
            source_transport="transport-x",
            source_sender_id="@alice:matrix.org",
            source_display_name="Alice",
            source_long_name="Alice Wonderland",
            source_short_name="alice",
            source_short_name_5="alice",
            source_room_or_channel="!room:matrix.org",
            source_origin_label="East Meshtastic",
            source_native_message_id="$msg1",
            source_native_channel_id="!room:matrix.org",
            route_id="route-42",
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("source_adapter_id", "matrix-1"),
            ("source_platform", "matrix"),
            ("source_transport", "transport-x"),
            ("source_sender_id", "@alice:matrix.org"),
            ("source_display_name", "Alice"),
            ("source_long_name", "Alice Wonderland"),
            ("source_short_name", "alice"),
            ("source_short_name_5", "alice"),
            ("source_room_or_channel", "!room:matrix.org"),
            ("source_origin_label", "East Meshtastic"),
            ("source_native_message_id", "$msg1"),
            ("source_native_channel_id", "!room:matrix.org"),
            ("route_id", "route-42"),
            # Aliases
            ("longname", "Alice Wonderland"),
            ("shortname", "alice"),
            ("shortname5", "alice"),
            ("from_id", "@alice:matrix.org"),
            ("origin_label", "East Meshtastic"),
        ],
    )
    def test_single_variable(self, name: str, expected: str) -> None:
        result = format_relay_prefix("{" + name + "}", self._full_attr())
        assert result.rendered_prefix == expected
        assert result.variables_used == (name,)
        assert not result.missing_variables
        assert not result.unknown_variables
        assert result.formatting_error is None

    def test_multiple_variables(self) -> None:
        result = format_relay_prefix("[{longname}/{origin_label}]: ", self._full_attr())
        assert result.rendered_prefix == "[Alice Wonderland/East Meshtastic]: "
        assert "longname" in result.variables_used
        assert "origin_label" in result.variables_used

    def test_shortname5_convention(self) -> None:
        """shortname5 is first 5 chars of shortname, falling back to from_id."""
        attr = RelayAttribution(
            source_short_name="abcdefgh",
            source_sender_id="!1234567890",
        )
        result = format_relay_prefix("{shortname5}", attr)
        assert result.rendered_prefix == "abcde"

    def test_shortname5_fallback_to_sender_id(self) -> None:
        attr = RelayAttribution(
            source_short_name=None,
            source_sender_id="node-42",
        )
        result = format_relay_prefix("{shortname5}", attr)
        assert result.rendered_prefix == "node-"

    def test_shortname5_empty_string_preserved(self) -> None:
        """Explicitly empty source_short_name_5 is preserved, not fallen back."""
        attr = RelayAttribution(
            source_short_name_5="",
            source_short_name="fallback",
            source_sender_id="fallback-id",
        )
        result = format_relay_prefix("{shortname5}", attr)
        assert result.rendered_prefix == ""
        assert "shortname5" in result.missing_variables


# ===================================================================
# None coalescing
# ===================================================================


class TestNoneCoalescing:
    """None values format as empty string, never the literal 'None'."""

    def test_none_renders_empty(self) -> None:
        attr = RelayAttribution(source_long_name=None)
        result = format_relay_prefix("[{longname}]", attr)
        assert result.rendered_prefix == "[]"
        assert "None" not in result.rendered_prefix

    def test_all_none_renders_empty(self) -> None:
        attr = RelayAttribution()
        result = format_relay_prefix("{source_sender_id}", attr)
        assert result.rendered_prefix == ""
        assert "source_sender_id" in result.missing_variables

    def test_partial_none(self) -> None:
        attr = RelayAttribution(
            source_long_name="Bob",
            source_origin_label=None,
        )
        result = format_relay_prefix("[{longname}/{origin_label}]", attr)
        assert result.rendered_prefix == "[Bob/]"
        assert "origin_label" in result.missing_variables
        assert "longname" not in result.missing_variables


# ===================================================================
# Existing templates
# ===================================================================


class TestExistingTemplates:
    """Existing adapter templates must work without modification."""

    def test_longname_origin_label(self) -> None:
        attr = RelayAttribution(
            source_long_name="Meshtastic User",
            source_origin_label="mynet",
        )
        result = format_relay_prefix("[{longname}/{origin_label}]: ", attr)
        assert result.rendered_prefix == "[Meshtastic User/mynet]: "

    def test_shortname5_bracket_m(self) -> None:
        attr = RelayAttribution(
            source_short_name_5="Short",
        )
        result = format_relay_prefix("{shortname5}[M]: ", attr)
        assert result.rendered_prefix == "Short[M]: "

    def test_shortname_bracket_origin(self) -> None:
        attr = RelayAttribution(
            source_short_name="SN",
            source_origin_label="net1",
        )
        result = format_relay_prefix("{shortname}[{origin_label}]: ", attr)
        assert result.rendered_prefix == "SN[net1]: "


# ===================================================================
# Unknown placeholder policy
# ===================================================================


class TestUnknownPlaceholderPolicy:
    """Unknown placeholders are left unchanged and recorded with error."""

    def test_unknown_left_unchanged(self) -> None:
        attr = RelayAttribution(source_long_name="Alice")
        result = format_relay_prefix("[{longname}/{bogus}]", attr)
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
        attr = RelayAttribution(source_long_name="Bob")
        result = format_relay_prefix("{longname}-{unknown}", attr)
        assert result.rendered_prefix == "Bob-{unknown}"
        assert "longname" in result.variables_used
        assert "unknown" in result.unknown_variables

    def test_unknown_no_error_when_all_known(self) -> None:
        attr = RelayAttribution(source_sender_id="user1")
        result = format_relay_prefix("[{from_id}]", attr)
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
            "{longname} [", RelayAttribution(source_long_name="A")
        )
        assert result.rendered_prefix == "A ["

    def test_double_braces_not_special(self) -> None:
        """Double braces are not escape sequences — they're just text."""
        result = format_relay_prefix(
            "{{longname}}", RelayAttribution(source_long_name="X")
        )
        assert result.rendered_prefix == "{X}"

    def test_nested_braces_not_matched(self) -> None:
        """{name{inner}} is not a valid placeholder."""
        result = format_relay_prefix("{a{b}}", RelayAttribution())
        assert result.rendered_prefix == "{a{b}}"
        assert result.unknown_variables == ("b",)

    def test_template_with_literal_text(self) -> None:
        attr = RelayAttribution(source_short_name="SN")
        result = format_relay_prefix(">>{shortname}<<", attr)
        assert result.rendered_prefix == ">>SN<<"


# ===================================================================
# Determinism
# ===================================================================


class TestDeterminism:
    """Same inputs always produce the same output."""

    def test_deterministic_repeated(self) -> None:
        attr = RelayAttribution(
            source_long_name="User1",
            source_origin_label="net",
        )
        template = "[{longname}/{origin_label}]: "
        r1 = format_relay_prefix(template, attr)
        r2 = format_relay_prefix(template, attr)
        assert r1.rendered_prefix == r2.rendered_prefix
        assert r1.variables_used == r2.variables_used
        assert r1.missing_variables == r2.missing_variables
        assert r1.formatting_error == r2.formatting_error

    def test_different_attrs_different_output(self) -> None:
        a1 = RelayAttribution(source_long_name="Alice")
        a2 = RelayAttribution(source_long_name="Bob")
        r1 = format_relay_prefix("{longname}", a1)
        r2 = format_relay_prefix("{longname}", a2)
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
        template = "[{longname}]: "
        result = format_relay_prefix(template, RelayAttribution())
        assert result.template_used == template

    def test_missing_variables_tracked(self) -> None:
        attr = RelayAttribution(source_long_name=None, source_sender_id=None)
        result = format_relay_prefix("{longname}-{from_id}", attr)
        assert "longname" in result.missing_variables
        assert "from_id" in result.missing_variables

    def test_empty_value_is_missing(self) -> None:
        attr = RelayAttribution(source_long_name="")
        result = format_relay_prefix("{longname}", attr)
        assert "longname" in result.missing_variables


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
        assert attr.source_long_name == "Alice"
        assert attr.source_short_name == "alice"

    def test_mxid_localpart_fallback(self) -> None:
        """When no displayname, short_name falls back to MXID localpart."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@bob:matrix.org",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_short_name == "bob"
        assert attr.source_display_name is None

    def test_sender_id_fallback_from_id(self) -> None:
        """from_id alias resolves to the Matrix sender MXID."""
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={"sender": "@carol:example.com"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{from_id}", attr)
        assert result.rendered_prefix == "@carol:example.com"


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
        assert attr.source_long_name == "Radio User"
        assert attr.source_short_name == "RU"

    def test_shortname5_from_shortname(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "shortname": "LongName",
                "from_id": "!1234",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_short_name_5 == "LongN"

    def test_shortname5_fallback_from_id(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={
                "from_id": "!abcdef123456",
            },
        )
        attr = extract_relay_attribution(event)
        assert attr.source_short_name_5 == "!abcd"

    def test_missing_longname(self) -> None:
        event = _make_event(
            source_adapter="meshtastic-radio",
            native_data={"from_id": "!node1"},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_long_name is None
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
        assert attr.source_long_name is None


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
        assert attr.source_long_name is None

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
        result = format_relay_prefix("[{longname}/{origin_label}]: ", attr)
        assert result.rendered_prefix == "[RadioOp/]: "
        assert "origin_label" in result.missing_variables
        assert result.formatting_error is None

    def test_meshcore_shortname5_template(self) -> None:
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={"pubkey_prefix": "aabbcc"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{shortname5}[M]: ", attr)
        # pubkey_prefix becomes source_sender_id, shortname5 derived from it
        assert result.rendered_prefix == "aabbc[M]: "

    def test_matrix_from_id_template(self) -> None:
        event = _make_event(
            source_adapter="matrix-bridge",
            native_data={"sender": "@dave:matrix.org"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{from_id}: ", attr)
        assert result.rendered_prefix == "@dave:matrix.org: "

    def test_lxmf_missing_display_no_crash(self) -> None:
        event = _make_event(
            source_adapter="lxmf-node",
            native_data={"source_hash": "hash1"},
        )
        attr = extract_relay_attribution(event)
        # longname is None -> renders empty
        result = format_relay_prefix("[{longname}]: ", attr)
        assert result.rendered_prefix == "[]: "
        assert "longname" in result.missing_variables


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
            "{from_id}/{source_sender_id}/{source_native_channel_id}", attr
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

    def test_bare_shortname5_derived_from_pubkey(self) -> None:
        """shortname5 derived from bare pubkey_prefix."""
        event = _make_event(
            source_adapter="meshcore-node",
            native_data={"pubkey_prefix": "aabbccdd"},
        )
        attr = extract_relay_attribution(event)
        result = format_relay_prefix("{shortname5}[MC]: ", attr)
        assert result.rendered_prefix == "aabbc[MC]: "


# ===================================================================
# Native-metadata-key platform detection
# ===================================================================


class TestPlatformDetectionFromNativeKeys:
    """Arbitrary adapter IDs produce correct platform from native metadata keys."""

    def test_meshcore_detected_from_namespaced_keys(self) -> None:
        """Adapter "radio-a" with MeshCore native keys → platform=meshcore."""
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
        """Adapter "relay" with Meshtastic native keys → platform=meshtastic."""
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
        """Adapter "base" with Matrix native keys → platform=matrix."""
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
        """Adapter "node-x" with LXMF native keys → platform=lxmf."""
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
        # adapter ID wins → meshtastic, not matrix
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
        """Unknown adapter with no native data → platform=None."""
        event = _make_event(source_adapter="unknown-thing", native_data=None)
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None

    def test_empty_native_data_no_platform(self) -> None:
        """Unknown adapter with empty native data → platform=None."""
        event = _make_event(source_adapter="unknown-thing", native_data={})
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None

    def test_unrecognizable_native_keys_no_platform(self) -> None:
        """Adapter with unrecognized native keys → platform=None."""
        event = _make_event(
            source_adapter="unknown-thing",
            native_data={"foo": "bar", "baz": 42},
        )
        attr = extract_relay_attribution(event)
        assert attr.source_platform is None


# ===================================================================
# MeshCore → formatter end-to-end with real codec data shape
# ===================================================================


class TestMeshCoreRealCodecEndToEnd:
    """Events shaped exactly like MeshCoreCodec produces work end-to-end."""

    def test_real_codec_shape_prefix(self) -> None:
        """Full codec-shaped native data produces {from_id}/{source_sender_id}/
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
            "{from_id}/{source_sender_id}/{source_native_channel_id}", attr
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

        result = format_relay_prefix("{from_id}[MC]: ", attr)
        assert result.rendered_prefix == "cafefe[MC]: "


# ===================================================================
# origin_label alias
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
            source_long_name="User1",
        )
        result = format_relay_prefix("[{origin_label}/{longname}]: ", attr)
        assert result.rendered_prefix == "[East Meshtastic/User1]: "
        assert "origin_label" in result.variables_used
        assert "longname" in result.variables_used

    def test_canonical_source_origin_label_direct(self) -> None:
        """Canonical name source_origin_label also resolves."""
        attr = RelayAttribution(source_origin_label="Direct Label")
        result = format_relay_prefix("{source_origin_label}", attr)
        assert result.rendered_prefix == "Direct Label"
        assert "source_origin_label" in result.variables_used

    def test_meshnet_name_is_unknown(self) -> None:
        """meshnet_name is no longer a known template variable."""
        attr = RelayAttribution(source_origin_label="Hub")
        result = format_relay_prefix("{meshnet_name}", attr)
        assert result.rendered_prefix == "{meshnet_name}"
        assert "meshnet_name" in result.unknown_variables
        assert result.formatting_error is not None
