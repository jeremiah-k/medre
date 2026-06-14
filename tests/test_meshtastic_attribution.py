"""Tests for Meshtastic adapter-adjacent attribution projection helper.

Validates :func:`~medre.adapters.meshtastic.attribution.project_meshtastic_attribution`
which projects native Meshtastic fields (longname, shortname, from_id)
into generic attribution fields without touching core extractors.
"""

from __future__ import annotations

from medre.adapters.meshtastic.attribution import project_meshtastic_attribution

# ===================================================================
# sender_id projection
# ===================================================================


def test_sender_id_from_from_id() -> None:
    """sender_id is projected from native from_id."""
    result = project_meshtastic_attribution({"from_id": "1234567890"})
    assert result["source_sender_id"] == "1234567890"


def test_sender_id_falls_back_to_transport_id() -> None:
    """sender_id uses source_transport_id when from_id is absent."""
    result = project_meshtastic_attribution({}, source_transport_id="!nodeABC")
    assert result["source_sender_id"] == "!nodeABC"


def test_sender_id_prefers_from_id_over_transport_id() -> None:
    """from_id takes priority over source_transport_id."""
    result = project_meshtastic_attribution(
        {"from_id": "42"}, source_transport_id="!fallback"
    )
    assert result["source_sender_id"] == "42"


def test_sender_id_none_when_both_absent() -> None:
    """sender_id is None when neither from_id nor transport_id present."""
    result = project_meshtastic_attribution({})
    assert result["source_sender_id"] is None


def test_sender_id_from_numeric_from_id() -> None:
    """sender_id coerces numeric from_id to string."""
    result = project_meshtastic_attribution({"from_id": 42})
    assert result["source_sender_id"] == "42"


def test_sender_id_ignores_empty_from_id() -> None:
    """Empty string from_id is treated as absent."""
    result = project_meshtastic_attribution(
        {"from_id": ""}, source_transport_id="!fallback"
    )
    assert result["source_sender_id"] == "!fallback"


# ===================================================================
# sender_label projection (longname > shortname > sender_id)
# ===================================================================


def test_sender_label_prefers_longname() -> None:
    """sender_label is longname when present."""
    result = project_meshtastic_attribution(
        {"longname": "MeshNode1", "shortname": "M1", "from_id": "123"}
    )
    assert result["source_sender_label"] == "MeshNode1"


def test_sender_label_falls_back_to_shortname() -> None:
    """sender_label is shortname when longname is absent."""
    result = project_meshtastic_attribution({"shortname": "M1", "from_id": "123"})
    assert result["source_sender_label"] == "M1"


def test_sender_label_falls_back_to_sender_id() -> None:
    """sender_label is sender_id when both longname and shortname absent."""
    result = project_meshtastic_attribution({"from_id": "123"})
    assert result["source_sender_label"] == "123"


def test_sender_label_falls_back_to_transport_id() -> None:
    """sender_label uses transport_id when no names and no from_id."""
    result = project_meshtastic_attribution({}, source_transport_id="!nodeX")
    assert result["source_sender_label"] == "!nodeX"


def test_sender_label_ignores_empty_longname() -> None:
    """Empty longname is skipped in favour of shortname."""
    result = project_meshtastic_attribution(
        {"longname": "", "shortname": "M1", "from_id": "123"}
    )
    assert result["source_sender_label"] == "M1"


def test_sender_label_ignores_empty_longname_and_shortname() -> None:
    """Empty longname and shortname fall through to sender_id."""
    result = project_meshtastic_attribution(
        {"longname": "", "shortname": "", "from_id": "99"}
    )
    assert result["source_sender_label"] == "99"


def test_sender_label_none_when_all_absent() -> None:
    """sender_label is None when no identifying field is present."""
    result = project_meshtastic_attribution({})
    assert result["source_sender_label"] is None


# ===================================================================
# sender_short_label projection (shortname > compact longname > compact sender_id)
# ===================================================================


def test_sender_short_label_prefers_shortname() -> None:
    """sender_short_label is shortname when present."""
    result = project_meshtastic_attribution(
        {"longname": "MeshNode1", "shortname": "M1", "from_id": "123"}
    )
    assert result["source_sender_short_label"] == "M1"


def test_sender_short_label_compact_longname_fallback() -> None:
    """sender_short_label is compact longname when shortname absent."""
    result = project_meshtastic_attribution(
        {"longname": "My Node Name", "from_id": "123"}
    )
    assert result["source_sender_short_label"] == "MyNodeName"


def test_sender_short_label_compact_sender_id_fallback() -> None:
    """sender_short_label is compact sender_id when no names."""
    result = project_meshtastic_attribution({"from_id": "123 456"})
    assert result["source_sender_short_label"] == "123456"


def test_sender_short_label_none_when_all_absent() -> None:
    """sender_short_label is None when no identifying field present."""
    result = project_meshtastic_attribution({})
    assert result["source_sender_short_label"] is None


def test_sender_short_label_ignores_empty_shortname() -> None:
    """Empty shortname is skipped in favour of compact longname."""
    result = project_meshtastic_attribution(
        {"longname": "Alpha Node", "shortname": "", "from_id": "42"}
    )
    assert result["source_sender_short_label"] == "AlphaNode"


# ===================================================================
# compact mode
# ===================================================================


def test_compact_strips_spaces_from_longname() -> None:
    """compact=True strips spaces from sender_label."""
    result = project_meshtastic_attribution(
        {"longname": "My Node Name", "shortname": "MNN", "from_id": "123"},
        compact=True,
    )
    assert result["source_sender_label"] == "MyNodeName"


def test_compact_strips_spaces_from_shortname_in_short_label() -> None:
    """compact=True strips spaces from sender_short_label."""
    result = project_meshtastic_attribution(
        {"longname": "Alpha Node", "shortname": "A N", "from_id": "42"},
        compact=True,
    )
    assert result["source_sender_short_label"] == "AN"


def test_compact_strips_spaces_from_sender_id_label() -> None:
    """compact=True strips spaces when sender_label falls back to sender_id."""
    result = project_meshtastic_attribution(
        {"from_id": "1 2 3"},
        compact=True,
    )
    assert result["source_sender_label"] == "123"


def test_compact_preserves_already_compact() -> None:
    """compact=True is idempotent on space-free values."""
    result = project_meshtastic_attribution(
        {"longname": "Node", "shortname": "N", "from_id": "42"},
        compact=True,
    )
    assert result["source_sender_label"] == "Node"
    assert result["source_sender_short_label"] == "N"


def test_compact_sender_label_is_not_source_display_name() -> None:
    """compact prefix works purely from native fields, no source_display_name."""
    # This replicates the scenario in the renderer where compact prefix
    # is built from longname/shortname/from_id without any display_name var.
    result = project_meshtastic_attribution(
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
    result = project_meshtastic_attribution({})
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_none_values_in_native_data() -> None:
    """Explicit None values in native_data are treated as absent."""
    result = project_meshtastic_attribution(
        {"longname": None, "shortname": None, "from_id": None}
    )
    assert result["source_sender_id"] is None
    assert result["source_sender_label"] is None
    assert result["source_sender_short_label"] is None


def test_numeric_values_coerced_to_string() -> None:
    """Numeric native values are coerced to strings."""
    result = project_meshtastic_attribution(
        {"longname": 42, "shortname": 7, "from_id": 123}
    )
    assert result["source_sender_id"] == "123"
    assert result["source_sender_label"] == "42"
    assert result["source_sender_short_label"] == "7"


def test_transport_id_only_no_native_data() -> None:
    """Only source_transport_id provided, no native data."""
    result = project_meshtastic_attribution({}, source_transport_id="!radio-node")
    assert result["source_sender_id"] == "!radio-node"
    assert result["source_sender_label"] == "!radio-node"
    assert result["source_sender_short_label"] == "!radio-node"


def test_longname_with_spaces_shortname_absent_not_compact() -> None:
    """Non-compact: longname with spaces preserved in sender_label,
    short_label gets compact longname."""
    result = project_meshtastic_attribution(
        {"longname": "Alice In Wonderland", "from_id": "!alice"}
    )
    assert result["source_sender_label"] == "Alice In Wonderland"
    assert result["source_sender_short_label"] == "AliceInWonderland"


def test_returns_only_three_fields() -> None:
    """Projection returns exactly the three generic fields."""
    result = project_meshtastic_attribution(
        {"longname": "X", "shortname": "Y", "from_id": "Z"}
    )
    assert set(result.keys()) == {
        "source_sender_id",
        "source_sender_label",
        "source_sender_short_label",
    }


# ===================================================================
# Namespaced keys (primary shape emitted by the codec)
# ===================================================================


def test_namespaced_from_id_primary_for_sender_id() -> None:
    """``meshtastic.from_id`` is the primary source for sender_id."""
    result = project_meshtastic_attribution({"meshtastic.from_id": "!primary"})
    assert result["source_sender_id"] == "!primary"


def test_namespaced_longname_primary_for_sender_label() -> None:
    """``meshtastic.longname`` is the primary source for sender_label."""
    result = project_meshtastic_attribution(
        {"meshtastic.longname": "Primary Name", "meshtastic.from_id": "!n"}
    )
    assert result["source_sender_label"] == "Primary Name"


def test_namespaced_shortname_primary_for_sender_short_label() -> None:
    """``meshtastic.shortname`` is the primary source for sender_short_label."""
    result = project_meshtastic_attribution(
        {
            "meshtastic.shortname": "PN",
            "meshtastic.longname": "Primary Name",
            "meshtastic.from_id": "!n",
        }
    )
    assert result["source_sender_short_label"] == "PN"


def test_namespaced_longname_falls_back_to_namespaced_shortname() -> None:
    """sender_label falls through meshtastic.longname → meshtastic.shortname."""
    result = project_meshtastic_attribution(
        {"meshtastic.shortname": "SN", "meshtastic.from_id": "!n"}
    )
    assert result["source_sender_label"] == "SN"


def test_namespaced_short_label_compact_longname_fallback() -> None:
    """sender_short_label falls through meshtastic.shortname →
    compact meshtastic.longname."""
    result = project_meshtastic_attribution(
        {"meshtastic.longname": "Alpha Node", "meshtastic.from_id": "!n"}
    )
    assert result["source_sender_short_label"] == "AlphaNode"


def test_namespaced_from_id_falls_back_to_transport_id() -> None:
    """sender_id falls through meshtastic.from_id → source_transport_id
    when namespaced from_id is absent."""
    result = project_meshtastic_attribution({}, source_transport_id="!transport")
    assert result["source_sender_id"] == "!transport"


# ===================================================================
# Namespaced → bare precedence (legacy input tolerance)
# ===================================================================


def test_namespaced_from_id_wins_over_bare_from_id() -> None:
    """``meshtastic.from_id`` takes precedence over bare ``from_id``."""
    result = project_meshtastic_attribution(
        {"meshtastic.from_id": "!new", "from_id": "!legacy"}
    )
    assert result["source_sender_id"] == "!new"


def test_namespaced_longname_wins_over_bare_longname() -> None:
    """``meshtastic.longname`` takes precedence over bare ``longname``."""
    result = project_meshtastic_attribution(
        {
            "meshtastic.longname": "New Name",
            "longname": "Legacy Name",
            "meshtastic.from_id": "!n",
        }
    )
    assert result["source_sender_label"] == "New Name"


def test_namespaced_shortname_wins_over_bare_shortname() -> None:
    """``meshtastic.shortname`` takes precedence over bare ``shortname``."""
    result = project_meshtastic_attribution(
        {
            "meshtastic.shortname": "NS",
            "shortname": "LS",
            "meshtastic.longname": "NL",
            "longname": "LL",
            "meshtastic.from_id": "!n",
        }
    )
    assert result["source_sender_short_label"] == "NS"


def test_namespaced_shortname_wins_over_bare_longname_for_label() -> None:
    """In a mixed dict, meshtastic.shortname wins over bare longname for
    sender_label (namespaced shape wins over bare shape)."""
    result = project_meshtastic_attribution(
        {
            "meshtastic.shortname": "Short",
            "longname": "Legacy Long",
            "from_id": "!n",
        }
    )
    assert result["source_sender_label"] == "Short"


def test_bare_keys_still_accepted_as_legacy_input() -> None:
    """Bare keys alone (no namespaced keys) still resolve correctly —
    this is the legacy input tolerance path exercised by stored events
    and test fixtures produced before namespacing."""
    result = project_meshtastic_attribution(
        {"longname": "Legacy", "shortname": "L", "from_id": "!legacy"}
    )
    assert result["source_sender_id"] == "!legacy"
    assert result["source_sender_label"] == "Legacy"
    assert result["source_sender_short_label"] == "L"


def test_namespaced_only_keys_resolve_end_to_end() -> None:
    """A dict using only namespaced keys (the codec's emitted shape)
    resolves end-to-end without any bare-key presence."""
    result = project_meshtastic_attribution(
        {
            "meshtastic.from_id": "!node",
            "meshtastic.longname": "Node Name",
            "meshtastic.shortname": "NN",
        }
    )
    assert result["source_sender_id"] == "!node"
    assert result["source_sender_label"] == "Node Name"
    assert result["source_sender_short_label"] == "NN"
