"""Focused unit tests for the MeshCore native-to-generic attribution
projection helper.

Covers:
- Full namespaced projection (meshcore.pubkey_prefix / sender_id /
  channel / packet_id).
- Precedence rules: pubkey_prefix over sender_id; namespaced channel
  over bare channel_idx.
- Bare-fixture-key backward compatibility (pubkey_prefix, channel_idx).
- str coercion of integer values (packet_id and channel from the codec
  are ints).
- Known-contact label projection (meshcore.contact_label ->
  source_sender_label; compact derivation for short label).
- Opaque pubkey prefixes never populate the label fields.
- Empty / missing / non-string contact label data handled safely.
- Platform detection via is_meshcore_native (contact-only dicts excluded).
- Compatibility with the core relay-attribution formatter when the
  projected fields are merged into a RelayAttribution.
"""

from __future__ import annotations

from medre.adapters.meshcore.attribution import (
    MESHCORE_NAMESPACED_KEYS,
    is_meshcore_native,
    project_meshcore_attribution,
)
from medre.core.rendering.attribution import (
    RelayAttribution,
    format_relay_prefix,
)

# ===================================================================
# Full namespaced projection
# ===================================================================


def test_full_namespaced_projection() -> None:
    """All namespaced keys project to their generic counterparts."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "deadbeef",
            "meshcore.sender_id": "deadbeef",
            "meshcore.channel": 3,
            "meshcore.packet_id": 999,
        }
    )
    assert result["source_sender_id"] == "deadbeef"
    assert result["source_native_channel_id"] == "3"
    assert result["source_native_message_id"] == "999"


def test_namespaced_pubkey_prefix_preferred_over_sender_id() -> None:
    """meshcore.pubkey_prefix wins over meshcore.sender_id."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "preferred",
            "meshcore.sender_id": "fallback",
            "meshcore.channel": 0,
        }
    )
    assert result["source_sender_id"] == "preferred"


def test_namespaced_sender_id_fallback() -> None:
    """When meshcore.pubkey_prefix absent, meshcore.sender_id is used."""
    result = project_meshcore_attribution(
        {
            "meshcore.sender_id": "sender-val",
            "meshcore.channel": 1,
        }
    )
    assert result["source_sender_id"] == "sender-val"


def test_namespaced_channel_preferred_over_bare() -> None:
    """meshcore.channel preferred over bare channel_idx."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk1",
            "meshcore.channel": 5,
            "channel_idx": 99,
        }
    )
    assert result["source_native_channel_id"] == "5"


def test_namespaced_packet_id_extracted() -> None:
    """meshcore.packet_id populates source_native_message_id."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.packet_id": 12345,
        }
    )
    assert result["source_native_message_id"] == "12345"


# ===================================================================
# Bare fixture-key backward compatibility
# ===================================================================


def test_bare_pubkey_prefix_fallback() -> None:
    """Bare pubkey_prefix still works when no namespaced key present."""
    result = project_meshcore_attribution(
        {
            "pubkey_prefix": "bare-pk",
            "channel_idx": "1",
        }
    )
    assert result["source_sender_id"] == "bare-pk"
    assert result["source_native_channel_id"] == "1"


def test_bare_channel_idx_fallback() -> None:
    """Bare channel_idx is used when meshcore.channel absent."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk1",
            "channel_idx": 7,
        }
    )
    assert result["source_native_channel_id"] == "7"


# ===================================================================
# str coercion of integer values
# ===================================================================


def test_integer_values_coerced_to_str() -> None:
    """Codec stores packet_id and channel as ints; projection coerces."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "abc123",
            "meshcore.channel": 0,
            "meshcore.packet_id": 42,
        }
    )
    assert result["source_native_channel_id"] == "0"
    assert result["source_native_message_id"] == "42"
    assert isinstance(result["source_native_channel_id"], str)
    assert isinstance(result["source_native_message_id"], str)


def test_none_channel_produces_none() -> None:
    """A None channel (DM packets) projects to None, not 'None'."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "dm-pk",
            "meshcore.channel": None,
            "meshcore.packet_id": 10,
        }
    )
    assert result["source_native_channel_id"] is None


# ===================================================================
# Label fields — known-contact labels
# ===================================================================


def test_sender_label_none_without_contact_data() -> None:
    """Labels are None when no known-contact label is present."""
    result = project_meshcore_attribution(
        {"meshcore.pubkey_prefix": "a1b2c3", "meshcore.channel": 2}
    )
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_label_fields_present_in_output_even_when_none() -> None:
    """Output always contains the label keys (for safe dict merge)."""
    result = project_meshcore_attribution({"meshcore.pubkey_prefix": "deadbeef"})
    assert "source_sender_label" in result
    assert "source_sender_short_label" in result
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_label_projects_to_sender_label() -> None:
    """A known-contact label populates source_sender_label."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "a1b2c3",
            "meshcore.channel": 0,
            "meshcore.contact_label": "EA1ABC",
        }
    )
    assert result["source_sender_label"] == "EA1ABC"
    assert result["source_sender_id"] == "a1b2c3"


def test_contact_label_short_derived_from_compact_label() -> None:
    """Short label falls back to the first token of the contact label."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": "Base Station Alpha",
        }
    )
    assert result["source_sender_label"] == "Base Station Alpha"
    assert result["source_sender_short_label"] == "Base"


def test_contact_label_single_word_short_equals_label() -> None:
    """A single-word contact label yields the same value for short."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 1,
            "meshcore.contact_label": "EA1ABC",
        }
    )
    assert result["source_sender_short_label"] == "EA1ABC"


def test_explicit_contact_short_label_preferred() -> None:
    """An explicit meshcore.contact_short_label wins over compact derivation."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": "Base Station",
            "meshcore.contact_short_label": "BASE",
        }
    )
    assert result["source_sender_label"] == "Base Station"
    assert result["source_sender_short_label"] == "BASE"


def test_contact_label_empty_string_treated_as_absent() -> None:
    """An empty-string contact label coalesces to None, not ''."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": "",
        }
    )
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_short_label_empty_falls_back_to_compact() -> None:
    """Empty short label falls back to compact contact label."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": "Node One",
            "meshcore.contact_short_label": "",
        }
    )
    assert result["source_sender_label"] == "Node One"
    assert result["source_sender_short_label"] == "Node"


def test_non_string_contact_label_coerced_safely() -> None:
    """Non-string contact labels are coerced via _str without raising."""
    # Integer: coerced to its string representation.
    result_int = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": 12345,
        }
    )
    assert result_int["source_sender_label"] == "12345"

    # None: treated as absent.
    result_none = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "pk",
            "meshcore.channel": 0,
            "meshcore.contact_label": None,
        }
    )
    assert result_none["source_sender_label"] is None


def test_pubkey_prefix_never_becomes_sender_label() -> None:
    """Opaque pubkey prefix stays in sender_id; label remains None."""
    result = project_meshcore_attribution(
        {"meshcore.pubkey_prefix": "deadbeef", "meshcore.channel": 0}
    )
    assert result["source_sender_id"] == "deadbeef"
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_only_dict_not_detected_as_meshcore_native() -> None:
    """A dict with only contact keys (no core identity keys) is not native."""
    assert not is_meshcore_native(
        {
            "meshcore.contact_label": "Alice",
            "meshcore.contact_short_label": "A",
        }
    )


# ===================================================================
# Empty / missing data
# ===================================================================


def test_empty_dict_yields_none_fields() -> None:
    """An empty native dict produces None for all resolvable fields."""
    result = project_meshcore_attribution({})
    assert result["source_sender_id"] is None
    assert result["source_native_channel_id"] is None
    assert result["source_native_message_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_missing_packet_id_yields_none() -> None:
    """Absent meshcore.packet_id projects source_native_message_id=None."""
    result = project_meshcore_attribution(
        {"meshcore.pubkey_prefix": "pk", "meshcore.channel": 1}
    )
    assert result["source_native_message_id"] is None


def test_empty_string_values_treated_as_absent() -> None:
    """Empty-string native values are coalesced to None, not ''."""
    result = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "",
            "meshcore.sender_id": "",
            "meshcore.channel": "",
        }
    )
    assert result["source_sender_id"] is None
    assert result["source_native_channel_id"] is None


# ===================================================================
# Platform detection
# ===================================================================


def test_is_meshcore_native_true_with_namespaced_keys() -> None:
    """Namespaced keys identify a dict as MeshCore-shaped."""
    assert is_meshcore_native({"meshcore.pubkey_prefix": "pk", "meshcore.channel": 0})


def test_is_meshcore_native_true_with_packet_id_only() -> None:
    """A single namespaced key is sufficient."""
    assert is_meshcore_native({"meshcore.packet_id": 42})


def test_is_meshcore_native_false_for_bare_keys() -> None:
    """Bare fixture keys alone are not a MeshCore signal."""
    assert not is_meshcore_native({"pubkey_prefix": "x", "channel_idx": 1})


def test_is_meshcore_native_false_for_empty_dict() -> None:
    """Empty dict is not MeshCore-shaped."""
    assert not is_meshcore_native({})


def test_is_meshcore_native_false_for_other_platforms() -> None:
    """Other platforms' characteristic keys are not MeshCore."""
    assert not is_meshcore_native({"sender": "@alice:matrix.org", "event_id": "$e1"})
    assert not is_meshcore_native(
        {"longname": "Op", "shortname": "O", "from_id": "!aa"}
    )


def test_namespaced_keys_constant_completeness() -> None:
    """The constant contains the four characteristic keys."""
    assert MESHCORE_NAMESPACED_KEYS == frozenset(
        {
            "meshcore.pubkey_prefix",
            "meshcore.sender_id",
            "meshcore.channel",
            "meshcore.packet_id",
        }
    )


# ===================================================================
# Integration: projection + core RelayAttribution formatter
# ===================================================================


def test_projection_feeds_relay_attribution_formatter() -> None:
    """Projected fields merge cleanly into a RelayAttribution."""
    projected = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "a1b2c3",
            "meshcore.sender_id": "a1b2c3",
            "meshcore.channel": 2,
            "meshcore.packet_id": 42,
        }
    )
    attr = RelayAttribution(
        source_adapter_id="meshcore-node",
        source_platform="meshcore",
        **projected,
    )
    assert attr.source_sender_id == "a1b2c3"
    assert attr.source_native_channel_id == "2"
    assert attr.source_native_message_id == "42"

    result = format_relay_prefix(
        "{sender_id}/{source_native_channel_id}/{source_native_message_id}",
        attr,
    )
    assert result.rendered_prefix == "a1b2c3/2/42"
    assert result.formatting_error is None


def test_projection_short_label_missing_in_prefix() -> None:
    """sender_short is missing (empty) when no contact label is present."""
    projected = project_meshcore_attribution({"meshcore.pubkey_prefix": "aabbcc"})
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("{sender_short}[MC]: ", attr)
    assert result.rendered_prefix == "[MC]: "
    assert "sender_short" in result.missing_variables


def test_projection_sender_label_in_prefix_with_contact() -> None:
    """{sender} renders the contact label when one is present."""
    projected = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "a1b2c3",
            "meshcore.channel": 0,
            "meshcore.contact_label": "EA1ABC",
        }
    )
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("[MC] {sender}: ", attr)
    assert result.rendered_prefix == "[MC] EA1ABC: "
    assert result.formatting_error is None


def test_projection_sender_empty_in_prefix_without_contact() -> None:
    """{sender} renders empty when no contact label is available."""
    projected = project_meshcore_attribution(
        {"meshcore.pubkey_prefix": "a1b2c3", "meshcore.channel": 0}
    )
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("[MC] {sender}: ", attr)
    assert result.rendered_prefix == "[MC] : "
    assert "sender" in result.missing_variables


def test_projection_sender_id_shows_pubkey_prefix() -> None:
    """{sender_id} always exposes the pubkey prefix."""
    projected = project_meshcore_attribution(
        {
            "meshcore.pubkey_prefix": "deadbeef",
            "meshcore.channel": 0,
            "meshcore.contact_label": "Alice",
        }
    )
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("{sender_id} ({sender}): ", attr)
    assert result.rendered_prefix == "deadbeef (Alice): "
