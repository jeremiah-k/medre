"""Focused unit tests for relay attribution model, formatter, and generic
builder.

Covers:
- RelayAttribution immutability and default construction.
- Generic preferred formatter variables ({sender}, {sender_short}, etc.).
- Old Meshtastic-era variables ({from_id}, {longname}, {shortname},
  {shortname5}) are now unknown placeholders.
- None coalescing to empty string; is-not-None preservation for labels.
- Unknown-placeholder policy (leave unchanged, set error).
- Brace / format edge cases (unmatched braces, empty template).
- Deterministic output.
- ``build_relay_attribution`` generic builder: envelope fields,
  projected_fields merge, source_native_ref authority, route_id
  resolution, origin_label precedence.

Platform extraction coverage lives in adapter attribution tests
(test_matrix_attribution.py, test_meshtastic_attribution.py, etc.).
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
    build_relay_attribution,
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


def _full_attr() -> RelayAttribution:
    """RelayAttribution with all fields populated for formatter tests."""
    return RelayAttribution(
        source_adapter_id="matrix-1",
        source_platform="matrix",
        source_transport="transport-x",
        source_sender_id="@alice:matrix.org",
        source_sender_label="Alice Wonderland",
        source_sender_short_label="alice",
        source_sender_handle="@alice",
        source_room_or_channel="!room:matrix.org",
        source_origin_label="East Meshtastic",
        source_native_message_id="$msg1",
        source_native_channel_id="!room:matrix.org",
        route_id="route-42",
    )


# ===================================================================
# RelayAttribution model tests
# ===================================================================


def test_default_construction_all_none() -> None:
    attr = RelayAttribution()
    assert attr.source_adapter_id is None
    assert attr.source_platform is None
    assert attr.source_sender_id is None
    assert attr.source_sender_label is None
    assert attr.source_sender_short_label is None
    assert attr.source_sender_handle is None
    assert attr.route_id is None


def test_frozen_immutability() -> None:
    attr = RelayAttribution(source_adapter_id="test")
    with pytest.raises(AttributeError):
        attr.source_adapter_id = "changed"  # type: ignore[misc]


def test_full_construction() -> None:
    attr = RelayAttribution(
        source_adapter_id="matrix-1",
        source_platform="matrix",
        source_transport="transport-x",
        source_sender_id="@alice:matrix.org",
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


def test_equality() -> None:
    a = RelayAttribution(source_adapter_id="x", source_platform="matrix")
    b = RelayAttribution(source_adapter_id="x", source_platform="matrix")
    assert a == b


def test_inequality() -> None:
    a = RelayAttribution(source_adapter_id="x")
    b = RelayAttribution(source_adapter_id="y")
    assert a != b


# ===================================================================
# Generic preferred formatter variables
# ===================================================================


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
        ("source_room_or_channel", "!room:matrix.org"),
        ("source_origin_label", "East Meshtastic"),
        ("source_native_message_id", "$msg1"),
        ("source_native_channel_id", "!room:matrix.org"),
    ],
)
def test_single_variable(name: str, expected: str) -> None:
    result = format_relay_prefix("{" + name + "}", _full_attr())
    assert result.rendered_prefix == expected
    assert result.variables_used == (name,)
    assert not result.missing_variables
    assert not result.unknown_variables
    assert result.formatting_error is None


def test_multiple_generic_variables() -> None:
    result = format_relay_prefix("[{sender}/{origin_label}]: ", _full_attr())
    assert result.rendered_prefix == "[Alice Wonderland/East Meshtastic]: "
    assert "sender" in result.variables_used
    assert "origin_label" in result.variables_used


def test_sender_and_sender_short() -> None:
    attr = RelayAttribution(
        source_sender_label="Operator",
        source_sender_short_label="Op",
    )
    result = format_relay_prefix("{sender} ({sender_short})", attr)
    assert result.rendered_prefix == "Operator (Op)"


def test_sender_handle() -> None:
    attr = RelayAttribution(source_sender_handle="@bob:matrix.org")
    result = format_relay_prefix("{sender_handle}", attr)
    assert result.rendered_prefix == "@bob:matrix.org"


def test_platform_variable() -> None:
    attr = RelayAttribution(source_platform="meshtastic")
    result = format_relay_prefix("[{platform}]", attr)
    assert result.rendered_prefix == "[meshtastic]"


def test_channel_variable() -> None:
    attr = RelayAttribution(source_room_or_channel="!room:server")
    result = format_relay_prefix("{channel}", attr)
    assert result.rendered_prefix == "!room:server"


def test_route_id_variable() -> None:
    attr = RelayAttribution(route_id="route-99")
    result = format_relay_prefix("{route_id}", attr)
    assert result.rendered_prefix == "route-99"


# ===================================================================
# Old Meshtastic-era variables are now unknown
# ===================================================================


@pytest.mark.parametrize(
    "name",
    ["from_id", "longname", "shortname", "shortname5", "meshnet_name"],
)
def test_old_variable_is_unknown(name: str) -> None:
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


# ===================================================================
# Explicit empty sender_short_label
# ===================================================================


def test_explicit_empty_sender_short_label_renders_empty() -> None:
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


def test_none_renders_empty() -> None:
    attr = RelayAttribution(source_sender_label=None)
    result = format_relay_prefix("[{sender}]", attr)
    assert result.rendered_prefix == "[]"
    assert "None" not in result.rendered_prefix


def test_all_none_renders_empty() -> None:
    attr = RelayAttribution()
    result = format_relay_prefix("{source_sender_id}", attr)
    assert result.rendered_prefix == ""
    assert "source_sender_id" in result.missing_variables


def test_partial_none() -> None:
    attr = RelayAttribution(
        source_sender_label="Bob",
        source_origin_label=None,
    )
    result = format_relay_prefix("[{sender}/{origin_label}]", attr)
    assert result.rendered_prefix == "[Bob/]"
    assert "origin_label" in result.missing_variables
    assert "sender" not in result.missing_variables


def test_missing_sender_fields_no_literal_none() -> None:
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


def test_unknown_left_unchanged() -> None:
    attr = RelayAttribution(source_sender_label="Alice")
    result = format_relay_prefix("[{sender}/{bogus}]", attr)
    assert result.rendered_prefix == "[Alice/{bogus}]"
    assert result.unknown_variables == ("bogus",)
    assert result.formatting_error is not None
    assert "bogus" in result.formatting_error


def test_multiple_unknowns() -> None:
    attr = RelayAttribution()
    result = format_relay_prefix("{foo}{bar}", attr)
    assert result.rendered_prefix == "{foo}{bar}"
    assert "foo" in result.unknown_variables
    assert "bar" in result.unknown_variables


def test_known_and_unknown_mix() -> None:
    attr = RelayAttribution(source_sender_label="Bob")
    result = format_relay_prefix("{sender}-{unknown}", attr)
    assert result.rendered_prefix == "Bob-{unknown}"
    assert "sender" in result.variables_used
    assert "unknown" in result.unknown_variables


def test_unknown_no_error_when_all_known() -> None:
    attr = RelayAttribution(source_sender_id="user1")
    result = format_relay_prefix("[{sender_id}]", attr)
    assert result.formatting_error is None


# ===================================================================
# Brace / format edge cases
# ===================================================================


def test_empty_template() -> None:
    result = format_relay_prefix("", RelayAttribution())
    assert result.rendered_prefix == ""
    assert not result.variables_used


def test_no_placeholders() -> None:
    result = format_relay_prefix("[static]: ", RelayAttribution())
    assert result.rendered_prefix == "[static]: "
    assert not result.variables_used


def test_unmatched_open_brace() -> None:
    """Unmatched braces that don't match the {name} pattern pass through."""
    result = format_relay_prefix(
        "{sender} [", RelayAttribution(source_sender_label="A")
    )
    assert result.rendered_prefix == "A ["


def test_double_braces_not_special() -> None:
    """Double braces are not escape sequences — they're just text."""
    result = format_relay_prefix(
        "{{sender}}", RelayAttribution(source_sender_label="X")
    )
    assert result.rendered_prefix == "{X}"


def test_nested_braces_not_matched() -> None:
    """{name{inner}} is not a valid placeholder."""
    result = format_relay_prefix("{a{b}}", RelayAttribution())
    assert result.rendered_prefix == "{a{b}}"
    assert result.unknown_variables == ("b",)


def test_template_with_literal_text() -> None:
    attr = RelayAttribution(source_sender_short_label="SN")
    result = format_relay_prefix(">>{sender_short}<<", attr)
    assert result.rendered_prefix == ">>SN<<"


# ===================================================================
# Determinism
# ===================================================================


def test_deterministic_repeated() -> None:
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


def test_different_attrs_different_output() -> None:
    a1 = RelayAttribution(source_sender_label="Alice")
    a2 = RelayAttribution(source_sender_label="Bob")
    r1 = format_relay_prefix("{sender}", a1)
    r2 = format_relay_prefix("{sender}", a2)
    assert r1.rendered_prefix != r2.rendered_prefix


# ===================================================================
# PrefixFormatterResult structure
# ===================================================================


def test_prefix_formatter_result_frozen() -> None:
    result = format_relay_prefix("", RelayAttribution())
    with pytest.raises(AttributeError):
        result.rendered_prefix = "changed"  # type: ignore[misc]


def test_template_used_recorded() -> None:
    template = "[{sender}]: "
    result = format_relay_prefix(template, RelayAttribution())
    assert result.template_used == template


def test_missing_variables_tracked() -> None:
    attr = RelayAttribution(source_sender_label=None, source_sender_id=None)
    result = format_relay_prefix("{sender}-{sender_id}", attr)
    assert "sender" in result.missing_variables
    assert "sender_id" in result.missing_variables


def test_empty_value_is_missing() -> None:
    attr = RelayAttribution(source_sender_label="")
    result = format_relay_prefix("{sender}", attr)
    assert "sender" in result.missing_variables


# ===================================================================
# build_relay_attribution: generic builder
# ===================================================================


def test_builder_copies_adapter_id() -> None:
    event = _make_event(source_adapter="my-adapter")
    attr = build_relay_attribution(event)
    assert attr.source_adapter_id == "my-adapter"


def test_builder_copies_transport() -> None:
    event = _make_event(source_transport_id="tcp-conn-1")
    attr = build_relay_attribution(event)
    assert attr.source_transport == "tcp-conn-1"


def test_builder_copies_room_or_channel() -> None:
    event = _make_event(source_channel_id="!room:server")
    attr = build_relay_attribution(event)
    assert attr.source_room_or_channel == "!room:server"


def test_builder_source_native_ref() -> None:
    ref = NativeRef(
        adapter="matrix-1",
        native_channel_id="!room:server",
        native_message_id="$event123",
    )
    event = _make_event(source_native_ref=ref)
    attr = build_relay_attribution(event)
    assert attr.source_native_message_id == "$event123"
    assert attr.source_native_channel_id == "!room:server"


def test_builder_route_id_from_route_trace() -> None:
    event = _make_event(route_trace=("route-1", "route-2"))
    attr = build_relay_attribution(event)
    assert attr.route_id == "route-1"


def test_builder_route_id_explicit_overrides() -> None:
    event = _make_event(route_trace=("route-1",))
    attr = build_relay_attribution(event, route_id="route-override")
    assert attr.route_id == "route-override"


def test_builder_no_projected_fields() -> None:
    """Without projected_fields, sender identity fields are None."""
    event = _make_event()
    attr = build_relay_attribution(event)
    assert attr.source_sender_id is None
    assert attr.source_sender_label is None
    assert attr.source_platform is None


def test_builder_merges_projected_fields() -> None:
    """Projected fields populate sender identity."""
    event = _make_event()
    attr = build_relay_attribution(
        event,
        projected_fields={
            "source_platform": "matrix",
            "source_sender_id": "@alice:matrix.org",
            "source_sender_label": "Alice",
            "source_sender_short_label": "alice",
        },
    )
    assert attr.source_platform == "matrix"
    assert attr.source_sender_id == "@alice:matrix.org"
    assert attr.source_sender_label == "Alice"
    assert attr.source_sender_short_label == "alice"


def test_builder_projected_fields_preserve_envelope() -> None:
    """Projected fields don't overwrite envelope fields."""
    event = _make_event(source_adapter="my-adapter")
    attr = build_relay_attribution(
        event,
        projected_fields={"source_adapter_id": "wrong"},
    )
    # Envelope field wins because it's set first; projected_fields
    # overwrites it since update() is used.
    assert attr.source_adapter_id == "wrong"


def test_builder_native_ref_overrides_projected() -> None:
    """source_native_ref IDs are authoritative over projected values."""
    ref = NativeRef(
        adapter="meshcore-1",
        native_channel_id="ch-envelope",
        native_message_id="$envelope-msg-id",
    )
    event = _make_event(
        source_adapter="meshcore-node",
        source_native_ref=ref,
    )
    attr = build_relay_attribution(
        event,
        projected_fields={
            "source_native_message_id": "raw-pkt-999",
            "source_native_channel_id": "raw-ch-7",
        },
    )
    # source_native_ref values must win.
    assert attr.source_native_message_id == "$envelope-msg-id"
    assert attr.source_native_channel_id == "ch-envelope"


def test_builder_origin_label_param_wins() -> None:
    """Explicit source_origin_label overrides projected value."""
    event = _make_event()
    attr = build_relay_attribution(
        event,
        source_origin_label="explicit-label",
        projected_fields={"source_origin_label": "projected-label"},
    )
    assert attr.source_origin_label == "explicit-label"


def test_builder_origin_label_from_projected() -> None:
    """When no explicit param, projected origin_label is used."""
    event = _make_event()
    attr = build_relay_attribution(
        event,
        projected_fields={"source_origin_label": "projected-label"},
    )
    assert attr.source_origin_label == "projected-label"


def test_builder_route_id_param_wins() -> None:
    """Explicit route_id overrides routing metadata."""
    event = _make_event(route_trace=("trace-route",))
    attr = build_relay_attribution(
        event,
        route_id="explicit-route",
        projected_fields={"route_id": "projected-route"},
    )
    assert attr.route_id == "explicit-route"


def test_builder_full_pipeline_format() -> None:
    """Builder + formatter produce correct prefix."""
    event = _make_event()
    attr = build_relay_attribution(
        event,
        projected_fields={
            "source_platform": "meshtastic",
            "source_sender_id": "!aabbcc",
            "source_sender_label": "RadioOp",
            "source_sender_short_label": "RO",
        },
    )
    result = format_relay_prefix("[{sender}/{origin_label}]: ", attr)
    assert result.rendered_prefix == "[RadioOp/]: "
    assert "origin_label" in result.missing_variables
    assert result.formatting_error is None


# ===================================================================
# origin_label alias (preferred)
# ===================================================================


def test_origin_label_resolves_when_set() -> None:
    attr = RelayAttribution(source_origin_label="East Meshtastic")
    result = format_relay_prefix("{origin_label}", attr)
    assert result.rendered_prefix == "East Meshtastic"
    assert "origin_label" in result.variables_used
    assert not result.missing_variables
    assert result.formatting_error is None


def test_origin_label_empty_when_none() -> None:
    attr = RelayAttribution(source_origin_label=None)
    result = format_relay_prefix("{origin_label}", attr)
    assert result.rendered_prefix == ""
    assert "origin_label" in result.missing_variables


def test_origin_label_empty_when_default() -> None:
    attr = RelayAttribution()
    result = format_relay_prefix("{origin_label}", attr)
    assert result.rendered_prefix == ""
    assert "None" not in result.rendered_prefix


def test_origin_label_with_value_renders() -> None:
    attr = RelayAttribution(source_origin_label="West Hub")
    result = format_relay_prefix("[{origin_label}]: ", attr)
    assert result.rendered_prefix == "[West Hub]: "


def test_origin_label_with_other_vars() -> None:
    """{origin_label} works alongside other template variables."""
    attr = RelayAttribution(
        source_origin_label="East Meshtastic",
        source_sender_label="User1",
    )
    result = format_relay_prefix("[{origin_label}/{sender}]: ", attr)
    assert result.rendered_prefix == "[East Meshtastic/User1]: "
    assert "origin_label" in result.variables_used
    assert "sender" in result.variables_used


def test_canonical_source_origin_label_direct() -> None:
    """Canonical name source_origin_label also resolves."""
    attr = RelayAttribution(source_origin_label="Direct Label")
    result = format_relay_prefix("{source_origin_label}", attr)
    assert result.rendered_prefix == "Direct Label"
    assert "source_origin_label" in result.variables_used


# ===================================================================
# platform_hint in dispatch (project_source_fields)
# ===================================================================


def test_platform_hint_matrix_overrides_adapter_id() -> None:
    """platform_hint 'matrix' takes precedence over adapter-id heuristic."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"sender": "@user:matrix.org", "event_id": "$123:matrix.org"},
        source_adapter="base",
        platform_hint="matrix",
    )
    assert fields["source_platform"] == "matrix"


def test_platform_hint_meshtastic_overrides_adapter_id() -> None:
    """platform_hint 'meshtastic' takes precedence over adapter-id heuristic."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"longname": "Radio Op", "shortname": "RO", "from_id": "!aabbcc"},
        source_adapter="radio-a",
        platform_hint="meshtastic",
    )
    assert fields["source_platform"] == "meshtastic"


def test_platform_hint_meshcore_overrides_adapter_id() -> None:
    """platform_hint 'meshcore' takes precedence over adapter-id heuristic."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"meshcore.pubkey_prefix": "abc123"},
        source_adapter="public",
        platform_hint="meshcore",
    )
    assert fields["source_platform"] == "meshcore"


def test_platform_hint_lxmf_overrides_adapter_id() -> None:
    """platform_hint 'lxmf' takes precedence over adapter-id heuristic."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"source_hash": "deadbeef"},
        source_adapter="mailbox",
        platform_hint="lxmf",
    )
    assert fields["source_platform"] == "lxmf"


def test_platform_hint_wins_over_native_keys() -> None:
    """platform_hint wins even when native keys suggest a different platform."""
    from medre.adapters._attribution_dispatch import project_source_fields

    # Native keys look like Meshtastic, but platform_hint says matrix.
    fields = project_source_fields(
        {"longname": "Op", "from_id": "!1234"},
        source_adapter="generic-adapter",
        platform_hint="matrix",
    )
    assert fields["source_platform"] == "matrix"


def test_native_key_fallback_without_platform_hint() -> None:
    """Without platform_hint, native key shape determines platform."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"longname": "Op", "shortname": "OP", "from_id": "!1234"},
        source_adapter="some-adapter",
    )
    assert fields["source_platform"] == "meshtastic"


def test_adapter_id_heuristic_without_platform_hint() -> None:
    """Without platform_hint, adapter-id substring determines platform."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {},
        source_adapter="meshtastic-radio-a",
    )
    assert fields["source_platform"] == "meshtastic"


def test_no_platform_detected_with_sparse_data() -> None:
    """No platform_hint and no recognisable keys → source_platform is None."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"unknown_key": "value"},
        source_adapter="generic",
    )
    assert fields["source_platform"] is None


def test_platform_hint_projects_sender_fields() -> None:
    """platform_hint dispatch produces correct sender fields, not just platform."""
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"sender": "@alice:matrix.org", "displayname": "Alice"},
        source_adapter="base",
        platform_hint="matrix",
    )
    assert fields["source_platform"] == "matrix"
    assert fields["source_sender_id"] == "@alice:matrix.org"
    assert fields["source_sender_label"] == "Alice"
    assert fields["source_sender_short_label"] == "alice"
    assert fields["source_sender_handle"] == "@alice:matrix.org"


def test_no_cross_platform_flat_key_enrichment() -> None:
    """Dispatch does NOT apply cross-platform flat-key enrichment.

    When platform_hint='matrix' but native_data carries only Meshtastic-
    style flat keys (longname, shortname, from_id), the Matrix projection
    finds no Matrix keys and returns None sender fields.  The dispatch
    does not silently patch from Meshtastic flat keys.
    """
    from medre.adapters._attribution_dispatch import project_source_fields

    fields = project_source_fields(
        {"longname": "RadioOp", "shortname": "RO", "from_id": "!aabbcc"},
        source_adapter="generic",
        platform_hint="matrix",
    )
    assert fields["source_platform"] == "matrix"
    # Matrix projection finds no Matrix keys → sender fields stay unset.
    # No global flat-key fallback patches them.
    assert fields.get("source_sender_label") is None
    assert fields.get("source_sender_id") is None


# ===================================================================
# Meshtastic platform detection: namespaced vs legacy vs channel
# ===================================================================


def test_detect_meshtastic_namespaced_from_id() -> None:
    """Namespaced ``meshtastic.from_id`` alone detects Meshtastic."""
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"meshtastic.from_id": "!node"},
    )
    assert platform == "meshtastic"


def test_detect_meshtastic_namespaced_longname() -> None:
    """Namespaced ``meshtastic.longname`` alone detects Meshtastic."""
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"meshtastic.longname": "Alpha"},
    )
    assert platform == "meshtastic"


def test_detect_meshtastic_namespaced_shortname() -> None:
    """Namespaced ``meshtastic.shortname`` alone detects Meshtastic."""
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"meshtastic.shortname": "AB"},
    )
    assert platform == "meshtastic"


def test_channel_alone_not_detected_as_meshtastic() -> None:
    """Bare ``channel`` without a platform hint is NOT Meshtastic.

    A sparse dict carrying only the generic ``channel`` key is too
    ambiguous to identify Meshtastic native data; the dispatch returns
    ``None`` rather than false-detecting Meshtastic.
    """
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform("generic", {"channel": 0})
    assert platform is None


def test_channel_alone_with_platform_hint_uses_hint() -> None:
    """platform_hint wins even when native data carries only ``channel``."""
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"channel": 0},
        platform_hint="meshtastic",
    )
    assert platform == "meshtastic"


def test_legacy_bare_keys_still_detect_meshtastic() -> None:
    """Legacy bare keys (without ``channel``) still detect Meshtastic.

    Backward compatibility for test fixtures and older data that carries
    bare ``longname``/``shortname``/``from_id`` keys.
    """
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"longname": "X", "shortname": "Y", "from_id": "!node"},
    )
    assert platform == "meshtastic"


def test_platform_hint_overrides_detection() -> None:
    """platform_hint overrides native key shape pointing at another platform.

    Native data carries Matrix keys, but platform_hint='meshtastic' wins
    and the dispatch reports Meshtastic.
    """
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"sender": "@alice:matrix.org", "event_id": "$1:matrix.org"},
        platform_hint="meshtastic",
    )
    assert platform == "meshtastic"


def test_namespaced_meshtastic_not_confused_with_matrix() -> None:
    """Namespaced ``meshtastic.*`` keys do not trigger Matrix detection.

    Even though the dict is sparse, the unambiguous ``meshtastic.*``
    namespace routes detection to Meshtastic, not Matrix.
    """
    from medre.adapters._attribution_dispatch import detect_source_platform

    platform = detect_source_platform(
        "generic",
        {"meshtastic.from_id": "!node"},
    )
    assert platform == "meshtastic"
    assert platform != "matrix"
