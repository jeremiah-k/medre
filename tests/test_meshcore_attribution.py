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
- Label fields are always None (MeshCore carries no display name).
- Empty / missing data yields all-None projected fields.
- Platform detection via is_meshcore_native.
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
# Label fields — always None (no display name in MeshCore)
# ===================================================================


def test_sender_label_always_none() -> None:
    """MeshCore carries no display name; label is always None."""
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
    """sender_short is missing (empty) since MeshCore has no short label."""
    projected = project_meshcore_attribution({"meshcore.pubkey_prefix": "aabbcc"})
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("{sender_short}[MC]: ", attr)
    assert result.rendered_prefix == "[MC]: "
    assert "sender_short" in result.missing_variables
