"""Per-entry source-context origin labels for ``channel_room_map``.

These smoke tests verify that the structured ``channel_room_map`` value
shape (``{room, source_origin_label?, dest_origin_label?}``) is parsed
correctly and that per-entry labels are threaded through route expansion
onto the correct per-leg ``RouteSource.origin_label``.

Key invariants verified:

* The bare-string legacy shape still parses and expands identically
  (backward compatibility).
* Per-entry ``source_origin_label`` overrides the route-level label on
  the forward leg only.
* Per-entry ``dest_origin_label`` overrides the route-level label on the
  reverse leg only.
* An explicit empty-string per-entry label (``""``) is preserved as
  ``RouteSource.origin_label == ""`` — it does NOT fall through to the
  route-level label (explicit suppression sentinel).
* Unknown keys in a structured entry are rejected.
* Boolean and non-string label values are rejected.
"""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.routes import (
    ChannelRoomMapEntry,
    RouteConfig,
    RouteConfigSet,
)
from medre.core.routing.models import Route
from medre.runtime.route_engine import build_runtime_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLATFORMS = {
    "matrix_adapter": "matrix",
    "mesh_adapter": "meshtastic",
}

_BASE_DATA: dict[str, object] = {
    "source_adapters": ["matrix_adapter"],
    "dest_adapters": ["mesh_adapter"],
    "directionality": "bidirectional",
}


def _expand(rc: RouteConfig) -> list[Route]:
    rcs = RouteConfigSet(routes=(rc,))
    return build_runtime_routes(rcs, _PLATFORMS)


def _leg(routes: list[Route], direction: str, channel: str = "0") -> Route:
    """Return the single expanded route for a given direction and channel."""
    matches = [r for r in routes if direction in r.id and f"__ch{channel}__" in r.id]
    assert len(matches) == 1, (
        f"expected exactly one {direction!r} leg on channel {channel!r}, "
        f"got ids={[r.id for r in routes]}"
    )
    return matches[0]


# ===========================================================================
# 1. Structured shape parses with per-entry labels
# ===========================================================================


def test_structured_entry_parses_with_labels() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": "LongFast",
                    "dest_origin_label": "Matrix Ops",
                },
            },
        },
    )
    assert rc.channel_room_map is not None
    entry = rc.channel_room_map["0"]
    assert isinstance(entry, ChannelRoomMapEntry)
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label == "LongFast"
    assert entry.dest_origin_label == "Matrix Ops"


def test_structured_entry_parses_room_only() -> None:
    """A structured entry with just ``room`` (no labels) defaults to None."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "channel_room_map": {"0": {"room": "!room0:example.com"}},
        },
    )
    assert rc.channel_room_map is not None
    entry = rc.channel_room_map["0"]
    assert isinstance(entry, ChannelRoomMapEntry)
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label is None
    assert entry.dest_origin_label is None


# ===========================================================================
# 2. Bare-string shape still works (backward compat)
# ===========================================================================


def test_bare_string_shape_backward_compat() -> None:
    """Legacy bare-string values parse to ChannelRoomMapEntry with None labels."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "channel_room_map": {
                "0": "!room0:example.com",
                "1": "!room1:example.com",
            },
        },
    )
    assert rc.channel_room_map is not None
    for ch in ("0", "1"):
        entry = rc.channel_room_map[ch]
        assert isinstance(entry, ChannelRoomMapEntry)
        assert entry.source_origin_label is None
        assert entry.dest_origin_label is None
    # Backward-compat: a label-less entry compares equal to its room string.
    assert rc.channel_room_map == {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }


def test_mixed_bare_and_structured_entries() -> None:
    """A map with one bare-string and one structured entry parses correctly."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "channel_room_map": {
                "0": "!room0:example.com",
                "1": {
                    "room": "!room1:example.com",
                    "source_origin_label": "Ops",
                },
            },
        },
    )
    assert rc.channel_room_map is not None
    assert rc.channel_room_map["0"].source_origin_label is None
    assert rc.channel_room_map["1"].source_origin_label == "Ops"


# ===========================================================================
# 3. Per-entry source_origin_label on forward leg
# ===========================================================================


def test_per_entry_source_label_on_forward_leg() -> None:
    """Entry source_origin_label overrides route-level on forward leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": "Entry Level",
                },
            },
        },
    )
    fwd = _leg(_expand(rc), "matrix_to_meshtastic")
    assert fwd.source.origin_label == "Entry Level"


def test_per_entry_source_label_other_channel_keeps_route_label() -> None:
    """Only the entry with the label gets it; other entries fall back."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": "Entry Level",
                },
                "1": "!room1:example.com",
            },
        },
    )
    routes = _expand(rc)
    fwd_ch0 = _leg(routes, "matrix_to_meshtastic", "0")
    fwd_ch1 = _leg(routes, "matrix_to_meshtastic", "1")
    assert fwd_ch0.source.origin_label == "Entry Level"
    assert fwd_ch1.source.origin_label == "Route Level"


# ===========================================================================
# 4. Per-entry dest_origin_label on reverse leg
# ===========================================================================


def test_per_entry_dest_label_on_reverse_leg() -> None:
    """Entry dest_origin_label overrides route-level on reverse leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "dest_origin_label": "Route Dest",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "dest_origin_label": "Entry Dest",
                },
            },
        },
    )
    rev = _leg(_expand(rc), "meshtastic_to_matrix")
    assert rev.source.origin_label == "Entry Dest"


# ===========================================================================
# 5. Empty-string per-entry label is preserved (not None)
# ===========================================================================


def test_empty_string_entry_label_preserved() -> None:
    """Explicit '' on an entry suppresses fallback — does NOT inherit route label."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": "",
                },
            },
        },
    )
    fwd = _leg(_expand(rc), "matrix_to_meshtastic")
    # Empty string is preserved, not None — renderers suppress adapter fallback.
    assert fwd.source.origin_label == ""


def test_empty_string_entry_dest_label_preserved() -> None:
    """Explicit '' on dest_origin_label is preserved on the reverse leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "dest_origin_label": "Route Dest",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "dest_origin_label": "",
                },
            },
        },
    )
    rev = _leg(_expand(rc), "meshtastic_to_matrix")
    assert rev.source.origin_label == ""


def test_entry_label_none_falls_back_to_route_label() -> None:
    """Entry with absent/None label inherits the route-level label."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    # source_origin_label absent → None → fall back
                },
            },
        },
    )
    fwd = _leg(_expand(rc), "matrix_to_meshtastic")
    assert fwd.source.origin_label == "Route Level"


# ===========================================================================
# 9. Explicit null vs empty string: fallback vs suppression (TC-013 / TC-014)
#
# Spec §17.5.8: an explicit YAML ``null`` (Python ``None``) means "fall
# back through the precedence chain"; an explicit empty string ``""``
# means "suppress the fallback for this entry".  These two tests make
# the contrast explicit at both the parse and route-expansion levels.
# ===========================================================================


def test_explicit_null_entry_source_label_falls_back_to_route() -> None:
    """Explicit None (YAML null) per-entry label falls back (TC-013).

    Counterpart to the absent-key test above: an explicit ``null`` must
    behave identically to an absent key — both produce ``None`` on the
    parsed entry, and both fall through to the route-level label during
    expansion.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": None,  # explicit YAML null
                },
            },
        },
    )
    # Parse level: explicit None is stored as None (not stripped, not "").
    assert rc.channel_room_map is not None
    assert rc.channel_room_map["0"].source_origin_label is None
    # Expansion level: None falls through to the route-level label.
    fwd = _leg(_expand(rc), "matrix_to_meshtastic")
    assert fwd.source.origin_label == "Route Level"


def test_explicit_empty_string_entry_label_suppresses_fallback() -> None:
    """Explicit '' per-entry label suppresses the fallback chain (TC-014).

    The counterpart to TC-013: ``null`` falls back, ``""`` does NOT.
    The route-level label is ignored when the entry carries an explicit
    empty string; the expanded leg's ``origin_label`` stays ``""`` so
    renderers suppress the adapter-level fallback for this entry's leg.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": "",  # explicit empty string
                },
            },
        },
    )
    # Parse level: explicit "" is stored as "" (distinct from None).
    assert rc.channel_room_map is not None
    entry = rc.channel_room_map["0"]
    assert entry.source_origin_label == ""
    assert entry.source_origin_label is not None
    # Expansion level: "" is preserved — route-level label is NOT used.
    fwd = _leg(_expand(rc), "matrix_to_meshtastic")
    assert fwd.source.origin_label == ""


def test_explicit_null_and_empty_string_contrast_in_same_route() -> None:
    """Null and empty string behave differently in the same route.

    Two entries on different channels — one with explicit ``None``,
    one with explicit ``""`` — must expand to different origin_labels.
    This guards against a regression where ``None`` and ``""`` are
    conflated.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "channel_room_map": {
                "0": {
                    "room": "!room0:example.com",
                    "source_origin_label": None,  # falls back
                },
                "1": {
                    "room": "!room1:example.com",
                    "source_origin_label": "",  # suppresses
                },
            },
        },
    )
    routes = _expand(rc)
    fwd_ch0 = _leg(routes, "matrix_to_meshtastic", "0")
    fwd_ch1 = _leg(routes, "matrix_to_meshtastic", "1")
    # Channel 0: None → route-level label.
    assert fwd_ch0.source.origin_label == "Route Level"
    # Channel 1: "" → stays empty (suppressed).
    assert fwd_ch1.source.origin_label == ""


# ===========================================================================
# 6. Unknown map-entry key is rejected
# ===========================================================================


def test_unknown_entry_key_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="unknown key"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {
                        "room": "!room0:example.com",
                        "label": "bad key",
                    },
                },
            },
        )


def test_structured_entry_missing_room_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="missing required 'room'"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {"source_origin_label": "No Room"},
                },
            },
        )


# ===========================================================================
# 7. Bool label is rejected
# ===========================================================================


def test_bool_source_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {
                        "room": "!room0:example.com",
                        "source_origin_label": True,
                    },
                },
            },
        )


def test_bool_dest_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {
                        "room": "!room0:example.com",
                        "dest_origin_label": False,
                    },
                },
            },
        )


# ===========================================================================
# 8. Non-string label is rejected
# ===========================================================================


def test_int_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {
                        "room": "!room0:example.com",
                        "source_origin_label": 42,
                    },
                },
            },
        )


def test_list_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {
                        "room": "!room0:example.com",
                        "dest_origin_label": ["a", "b"],
                    },
                },
            },
        )


def test_non_str_non_dict_value_rejected() -> None:
    """A raw value that is neither a string nor a dict is rejected."""
    with pytest.raises(ConfigValidationError):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {"0": 12345},
            },
        )


# ===========================================================================
# Existing room validation still applies in structured form
# ===========================================================================


def test_structured_entry_alias_room_rejected() -> None:
    """Room aliases (starting with '#') are rejected even in structured form."""
    with pytest.raises(ConfigValidationError, match="room alias"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {"room": "#room:example.com"},
                },
            },
        )


def test_structured_entry_non_canonical_room_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {"room": "not_a_room"},
                },
            },
        )


def test_structured_entry_non_string_room_rejected() -> None:
    """A non-string 'room' value in a structured entry is rejected."""
    with pytest.raises(ConfigValidationError, match="room"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "channel_room_map": {
                    "0": {"room": 12345},
                },
            },
        )
