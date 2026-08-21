"""Tests for Meshtastic adapter-adjacent attribution projection helper.

Validates :func:`~medre.adapters.meshtastic.attribution.project_meshtastic_attribution`
which projects native Meshtastic fields (longname, shortname, from_id)
into generic attribution fields without touching core extractors.
"""

from __future__ import annotations

from medre.adapters.meshtastic.attribution import project_meshtastic_attribution
from tests.helpers.native_metadata import meshtastic_native_data


def _project(
    native_data: dict[str, object],
    *,
    source_transport_id: str | None = None,
    compact: bool = False,
) -> dict[str, str | None]:
    return project_meshtastic_attribution(
        meshtastic_native_data(native_data),
        source_transport_id=source_transport_id,
        compact=compact,
    )


# ===================================================================
# sender_id projection
# ===================================================================


def test_sender_id_from_from_id() -> None:
    """sender_id is projected from native from_id."""
    result = _project({"from_id": "1234567890"})
    assert result["source_sender_id"] == "1234567890"


def test_sender_id_falls_back_to_transport_id() -> None:
    """sender_id uses source_transport_id when from_id is absent."""
    result = _project({}, source_transport_id="!nodeABC")
    assert result["source_sender_id"] == "!nodeABC"


def test_sender_id_prefers_from_id_over_transport_id() -> None:
    """from_id takes priority over source_transport_id."""
    result = _project({"from_id": "42"}, source_transport_id="!fallback")
    assert result["source_sender_id"] == "42"


def test_sender_id_none_when_both_absent() -> None:
    """sender_id is None when neither from_id nor transport_id present."""
    result = _project({})
    assert result["source_sender_id"] is None


def test_sender_id_from_numeric_from_id() -> None:
    """sender_id coerces numeric from_id to string."""
    result = _project({"from_id": 42})
    assert result["source_sender_id"] == "42"


def test_sender_id_ignores_empty_from_id() -> None:
    """Empty string from_id is treated as absent."""
    result = _project({"from_id": ""}, source_transport_id="!fallback")
    assert result["source_sender_id"] == "!fallback"


# ===================================================================
# sender_label projection (longname > shortname > sender_id)
# ===================================================================


def test_sender_label_prefers_longname() -> None:
    """sender_label is longname when present."""
    result = _project({"longname": "MeshNode1", "shortname": "M1", "from_id": "123"})
    assert result["source_sender_label"] == "MeshNode1"


def test_sender_label_falls_back_to_shortname() -> None:
    """sender_label is shortname when longname is absent."""
    result = _project({"shortname": "M1", "from_id": "123"})
    assert result["source_sender_label"] == "M1"


def test_sender_label_falls_back_to_sender_id() -> None:
    """sender_label is sender_id when both longname and shortname absent."""
    result = _project({"from_id": "123"})
    assert result["source_sender_label"] == "123"


def test_sender_label_falls_back_to_transport_id() -> None:
    """sender_label uses transport_id when no names and no from_id."""
    result = _project({}, source_transport_id="!nodeX")
    assert result["source_sender_label"] == "!nodeX"


def test_sender_label_ignores_empty_longname() -> None:
    """Empty longname is skipped in favour of shortname."""
    result = _project({"longname": "", "shortname": "M1", "from_id": "123"})
    assert result["source_sender_label"] == "M1"


def test_sender_label_ignores_empty_longname_and_shortname() -> None:
    """Empty longname and shortname fall through to sender_id."""
    result = _project({"longname": "", "shortname": "", "from_id": "99"})
    assert result["source_sender_label"] == "99"


def test_sender_label_none_when_all_absent() -> None:
    """sender_label is None when no identifying field is present."""
    result = _project({})
    assert result["source_sender_label"] is None


# ===================================================================
# sender_short_label projection (shortname > compact longname > compact sender_id)
# ===================================================================


def test_sender_short_label_prefers_shortname() -> None:
    """sender_short_label is shortname when present."""
    result = _project({"longname": "MeshNode1", "shortname": "M1", "from_id": "123"})
    assert result["source_sender_short_label"] == "M1"


def test_sender_short_label_compact_longname_fallback() -> None:
    """sender_short_label is compact longname when shortname absent."""
    result = _project({"longname": "My Node Name", "from_id": "123"})
    assert result["source_sender_short_label"] == "MyNodeName"


def test_sender_short_label_compact_sender_id_fallback() -> None:
    """sender_short_label is compact sender_id when no names."""
    result = _project({"from_id": "123 456"})
    assert result["source_sender_short_label"] == "123456"


def test_sender_short_label_none_when_all_absent() -> None:
    """sender_short_label is None when no identifying field present."""
    result = _project({})
    assert result["source_sender_short_label"] is None


def test_sender_short_label_ignores_empty_shortname() -> None:
    """Empty shortname is skipped in favour of compact longname."""
    result = _project({"longname": "Alpha Node", "shortname": "", "from_id": "42"})
    assert result["source_sender_short_label"] == "AlphaNode"


# ===================================================================
# compact mode
# ===================================================================


def test_compact_strips_spaces_from_longname() -> None:
    """compact=True strips spaces from sender_label."""
    result = _project(
        {"longname": "My Node Name", "shortname": "MNN", "from_id": "123"},
        compact=True,
    )
    assert result["source_sender_label"] == "MyNodeName"


def test_compact_strips_spaces_from_shortname_in_short_label() -> None:
    """compact=True strips spaces from sender_short_label."""
    result = _project(
        {"longname": "Alpha Node", "shortname": "A N", "from_id": "42"},
        compact=True,
    )
    assert result["source_sender_short_label"] == "AN"


def test_compact_strips_spaces_from_sender_id_label() -> None:
    """compact=True strips spaces when sender_label falls back to sender_id."""
    result = _project(
        {"from_id": "1 2 3"},
        compact=True,
    )
    assert result["source_sender_label"] == "123"


def test_compact_preserves_already_compact() -> None:
    """compact=True is idempotent on space-free values."""
    result = _project(
        {"longname": "Node", "shortname": "N", "from_id": "42"},
        compact=True,
    )
    assert result["source_sender_label"] == "Node"
    assert result["source_sender_short_label"] == "N"


def test_compact_sender_label_is_not_source_display_name() -> None:
    """compact prefix works purely from native fields, no source_display_name."""
    # This replicates the scenario in the renderer where compact prefix
    # is built from longname/shortname/from_id without any display_name var.
    result = _project(
        {"longname": "Bob Smith", "shortname": "BS", "from_id": "!bob"},
        compact=True,
    )
    assert result["source_sender_label"] == "BobSmith"
    assert result["source_sender_short_label"] == "BS"
    assert result["source_sender_id"] == "!bob"


# ===================================================================
# Edge cases
# ===================================================================


def test_empty_native_data_dict() -> None:
    """Empty dict returns all None fields."""
    result = _project({})
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_none_values_in_native_data() -> None:
    """Explicit None values in native_data are treated as absent."""
    result = _project({"longname": None, "shortname": None, "from_id": None})
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_numeric_values_coerced_to_string() -> None:
    """Numeric native values are coerced to strings."""
    result = _project({"longname": 42, "shortname": 7, "from_id": 123})
    assert result["source_sender_id"] == "123"
    assert result["source_sender_label"] == "42"
    assert result["source_sender_short_label"] == "7"


def test_transport_id_only_no_native_data() -> None:
    """Only source_transport_id provided, no native data."""
    result = _project({}, source_transport_id="!radio-node")
    assert result["source_sender_id"] == "!radio-node"
    assert result["source_sender_label"] == "!radio-node"
    assert result["source_sender_short_label"] == "!radio-node"


def test_longname_with_spaces_shortname_absent_not_compact() -> None:
    """Non-compact: longname with spaces preserved in sender_label,
    short_label gets compact longname."""
    result = _project({"longname": "Alice In Wonderland", "from_id": "!alice"})
    assert result["source_sender_label"] == "Alice In Wonderland"
    assert result["source_sender_short_label"] == "AliceInWonderland"


def test_returns_only_generic_fields() -> None:
    """Projection returns only generic attribution fields."""
    result = _project({"longname": "X", "shortname": "Y", "from_id": "Z"})
    assert set(result.keys()) == {
        "source_sender_id",
        "source_sender_label",
        "source_sender_short_label",
        "source_sender_handle",
    }


# ===================================================================
# Strict versioned namespace
# ===================================================================


def test_current_versioned_namespace_resolves_end_to_end() -> None:
    native = meshtastic_native_data(
        {
            "from_id": "!node",
            "longname": "Node Name",
            "shortname": "NN",
        }
    )
    result = project_meshtastic_attribution(native)
    assert result["source_sender_id"] == "!node"
    assert result["source_sender_label"] == "Node Name"
    assert result["source_sender_short_label"] == "NN"


def test_flat_native_fields_are_not_interpreted() -> None:
    result = project_meshtastic_attribution(
        {"from_id": "!flat", "longname": "Flat", "shortname": "F"}
    )
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_dotted_native_fields_are_not_interpreted() -> None:
    result = project_meshtastic_attribution(
        {
            "meshtastic.from_id": "!dotted",
            "meshtastic.longname": "Dotted",
            "meshtastic.shortname": "D",
        }
    )
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_unsupported_namespace_version_is_not_projected() -> None:
    native = meshtastic_native_data({"from_id": "!future"})
    matrix = native["meshtastic"]
    assert isinstance(matrix, dict)
    matrix["schema_version"] = 2
    result = project_meshtastic_attribution(native)
    assert result["source_sender_id"] is None


def test_transport_id_fallback_does_not_require_native_namespace() -> None:
    result = project_meshtastic_attribution({}, source_transport_id="!transport")
    assert result["source_sender_id"] == "!transport"
    assert result["source_sender_label"] == "!transport"
