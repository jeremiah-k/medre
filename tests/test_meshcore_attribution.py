"""Tests for strict versioned MeshCore attribution projection.

Current metadata is read only from ``native.meshcore``. The tests exercise
identity/channel projection, contact-label handling, versioned platform
detection, and RelayAttribution integration.
"""

from __future__ import annotations

from medre.adapters.meshcore.attribution import (
    is_meshcore_native,
    project_meshcore_attribution,
)
from medre.core.rendering.attribution import (
    RelayAttribution,
    format_relay_prefix,
)
from tests.helpers.native_metadata import meshcore_native_data


def _project(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    return project_meshcore_attribution(meshcore_native_data(native_data))


# ===================================================================
# Full namespaced projection
# ===================================================================


def test_full_namespaced_projection() -> None:
    """All namespaced keys project to their generic counterparts."""
    result = _project(
        {
            "pubkey_prefix": "deadbeef",
            "sender_id": "deadbeef",
            "channel": 3,
            "packet_id": 999,
        }
    )
    assert result["source_sender_id"] == "deadbeef"
    assert result["source_native_channel_id"] == "3"
    assert result["source_native_message_id"] == "999"


def test_namespaced_pubkey_prefix_preferred_over_sender_id() -> None:
    """pubkey_prefix wins over sender_id."""
    result = _project(
        {
            "pubkey_prefix": "preferred",
            "sender_id": "fallback",
            "channel": 0,
        }
    )
    assert result["source_sender_id"] == "preferred"


def test_namespaced_sender_id_fallback() -> None:
    """When pubkey_prefix absent, sender_id is used."""
    result = _project(
        {
            "sender_id": "sender-val",
            "channel": 1,
        }
    )
    assert result["source_sender_id"] == "sender-val"


def test_namespaced_channel_preferred_over_bare() -> None:
    """channel preferred over bare channel_idx."""
    result = _project(
        {
            "pubkey_prefix": "pk1",
            "channel": 5,
            "channel_idx": 99,
        }
    )
    assert result["source_native_channel_id"] == "5"


def test_namespaced_packet_id_extracted() -> None:
    """packet_id populates source_native_message_id."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "packet_id": 12345,
        }
    )
    assert result["source_native_message_id"] == "12345"


# ===================================================================
# Unsupported inner aliases
# ===================================================================


def test_channel_idx_alias_is_not_projected() -> None:
    """The raw SDK ``channel_idx`` alias is not canonical native metadata."""
    result = _project({"pubkey_prefix": "pk1", "channel_idx": 7})
    assert result["source_sender_id"] == "pk1"
    assert result["source_native_channel_id"] is None


# ===================================================================
# str coercion of integer values
# ===================================================================


def test_integer_values_coerced_to_str() -> None:
    """Codec stores packet_id and channel as ints; projection coerces."""
    result = _project(
        {
            "pubkey_prefix": "abc123",
            "channel": 0,
            "packet_id": 42,
        }
    )
    assert result["source_native_channel_id"] == "0"
    assert result["source_native_message_id"] == "42"
    assert isinstance(result["source_native_channel_id"], str)
    assert isinstance(result["source_native_message_id"], str)


def test_none_channel_produces_none() -> None:
    """A None channel (DM packets) projects to None, not 'None'."""
    result = _project(
        {
            "pubkey_prefix": "dm-pk",
            "channel": None,
            "packet_id": 10,
        }
    )
    assert result["source_native_channel_id"] is None


# ===================================================================
# Label fields — known-contact labels
# ===================================================================


def test_sender_label_none_without_contact_data() -> None:
    """Labels are None when no known-contact label is present."""
    result = _project({"pubkey_prefix": "a1b2c3", "channel": 2})
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_label_fields_present_in_output_even_when_none() -> None:
    """Output always contains the label keys (for safe dict merge)."""
    result = _project({"pubkey_prefix": "deadbeef"})
    assert "source_sender_label" in result
    assert "source_sender_short_label" in result
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_label_projects_to_sender_label() -> None:
    """A known-contact label populates source_sender_label."""
    result = _project(
        {
            "pubkey_prefix": "a1b2c3",
            "channel": 0,
            "contact_label": "EA1ABC",
        }
    )
    assert result["source_sender_label"] == "EA1ABC"
    assert result["source_sender_id"] == "a1b2c3"


def test_contact_label_short_derived_from_first_token() -> None:
    """Short label falls back to the first token of the contact label."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": "Base Station Alpha",
        }
    )
    assert result["source_sender_label"] == "Base Station Alpha"
    assert result["source_sender_short_label"] == "Base"


def test_contact_label_single_word_short_equals_label() -> None:
    """A single-word contact label yields the same value for short."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 1,
            "contact_label": "EA1ABC",
        }
    )
    assert result["source_sender_short_label"] == "EA1ABC"


def test_explicit_contact_short_label_preferred() -> None:
    """An explicit contact_short_label wins over first-token derivation."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": "Base Station",
            "contact_short_label": "BASE",
        }
    )
    assert result["source_sender_label"] == "Base Station"
    assert result["source_sender_short_label"] == "BASE"


def test_contact_label_empty_string_treated_as_absent() -> None:
    """An empty-string contact label coalesces to None, not ''."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": "",
        }
    )
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_short_label_empty_falls_back_to_compact() -> None:
    """Empty short label falls back to compact contact label."""
    result = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": "Node One",
            "contact_short_label": "",
        }
    )
    assert result["source_sender_label"] == "Node One"
    assert result["source_sender_short_label"] == "Node"


def test_non_string_contact_label_not_coerced() -> None:
    """Non-string contact labels yield None rather than str()-coerced text.

    Contact labels are human-readable names resolved from the local
    contacts store.  Integer, dict, and other non-string inputs are
    rejected by the strict ``_contact_label_str`` helper so that
    ``str(123)``-style rendering never pollutes ``source_sender_label``.
    """
    # Integer: rejected (no longer coerced to "12345").
    result_int = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": 12345,
        }
    )
    assert result_int["source_sender_label"] is None
    assert result_int["source_sender_short_label"] is None

    # Dict: rejected (no longer coerced to "{...}").
    result_dict = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": {"x": 1},
        }
    )
    assert result_dict["source_sender_label"] is None

    # None: treated as absent.
    result_none = _project(
        {
            "pubkey_prefix": "pk",
            "channel": 0,
            "contact_label": None,
        }
    )
    assert result_none["source_sender_label"] is None


def test_int_contact_label_not_coerced() -> None:
    """An integer contact label is rejected, sender_id still resolves."""
    result = _project(
        {
            "pubkey_prefix": "a1b2",
            "contact_label": 123,
        }
    )
    assert result["source_sender_label"] is None
    assert result["source_sender_id"] == "a1b2"


def test_dict_contact_label_not_coerced() -> None:
    """A dict contact label is rejected rather than rendered as '{...}'."""
    result = _project(
        {
            "pubkey_prefix": "a1b2",
            "contact_label": {"x": 1},
        }
    )
    assert result["source_sender_label"] is None


def test_whitespace_contact_label_trimmed() -> None:
    """Surrounding whitespace on a contact label is trimmed."""
    result = _project(
        {
            "pubkey_prefix": "a1b2",
            "contact_label": "  Alice  ",
        }
    )
    assert result["source_sender_label"] == "Alice"


def test_none_contact_label_is_none() -> None:
    """An explicit None contact label projects to None."""
    result = _project(
        {
            "pubkey_prefix": "a1b2",
            "contact_label": None,
        }
    )
    assert result["source_sender_label"] is None


def test_pubkey_prefix_never_becomes_sender_label() -> None:
    """Opaque pubkey prefix stays in sender_id; label remains None."""
    result = _project({"pubkey_prefix": "deadbeef", "channel": 0})
    assert result["source_sender_id"] == "deadbeef"
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_contact_only_dict_not_detected_as_meshcore_native() -> None:
    """A dict with only contact keys (no core identity keys) is not native."""
    assert not is_meshcore_native(
        {
            "contact_label": "Alice",
            "contact_short_label": "A",
        }
    )


# ===================================================================
# Empty / missing data
# ===================================================================


def test_empty_dict_yields_none_fields() -> None:
    """An empty native dict produces None for all resolvable fields."""
    result = _project({})
    assert result["source_sender_id"] is None
    assert result["source_native_channel_id"] is None
    assert result["source_native_message_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_missing_packet_id_yields_none() -> None:
    """Absent packet_id projects source_native_message_id=None."""
    result = _project({"pubkey_prefix": "pk", "channel": 1})
    assert result["source_native_message_id"] is None


def test_empty_string_values_treated_as_absent() -> None:
    """Empty-string native values are coalesced to None, not ''."""
    result = _project(
        {
            "pubkey_prefix": "",
            "sender_id": "",
            "channel": "",
        }
    )
    assert result["source_sender_id"] is None
    assert result["source_native_channel_id"] is None


# ===================================================================
# Platform detection
# ===================================================================


def test_is_meshcore_native_true_for_versioned_namespace() -> None:
    """A positively versioned MeshCore namespace identifies the platform."""
    assert is_meshcore_native(meshcore_native_data({"packet_id": 42}))


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


# ===================================================================
# Integration: projection + core RelayAttribution formatter
# ===================================================================


def test_projection_feeds_relay_attribution_formatter() -> None:
    """Projected fields merge cleanly into a RelayAttribution."""
    projected = _project(
        {
            "pubkey_prefix": "a1b2c3",
            "sender_id": "a1b2c3",
            "channel": 2,
            "packet_id": 42,
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
    projected = _project({"pubkey_prefix": "aabbcc"})
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("{sender_short}[MC]: ", attr)
    assert result.rendered_prefix == "[MC]: "
    assert "sender_short" in result.missing_variables


def test_projection_sender_label_in_prefix_with_contact() -> None:
    """{sender} renders the contact label when one is present."""
    projected = _project(
        {
            "pubkey_prefix": "a1b2c3",
            "channel": 0,
            "contact_label": "EA1ABC",
        }
    )
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("[MC] {sender}: ", attr)
    assert result.rendered_prefix == "[MC] EA1ABC: "
    assert result.formatting_error is None


def test_projection_sender_empty_in_prefix_without_contact() -> None:
    """{sender} renders empty when no contact label is available."""
    projected = _project({"pubkey_prefix": "a1b2c3", "channel": 0})
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("[MC] {sender}: ", attr)
    assert result.rendered_prefix == "[MC] : "
    assert "sender" in result.missing_variables


def test_projection_sender_id_shows_pubkey_prefix() -> None:
    """{sender_id} always exposes the pubkey prefix."""
    projected = _project(
        {
            "pubkey_prefix": "deadbeef",
            "channel": 0,
            "contact_label": "Alice",
        }
    )
    attr = RelayAttribution(source_platform="meshcore", **projected)
    result = format_relay_prefix("{sender_id} ({sender}): ", attr)
    assert result.rendered_prefix == "deadbeef (Alice): "
