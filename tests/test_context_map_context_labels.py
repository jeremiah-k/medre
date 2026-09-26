"""Per-entry origin labels for ``context_map`` entries.

These tests verify that the structured ``context_map`` entry shape
(``{dest_context | dest_destination, source_origin_label?,
dest_origin_label?}``) is parsed correctly and that per-entry labels are
threaded through compiler expansion onto the correct per-leg
``RouteSource.origin_label``.

Key invariants verified:

* Per-entry ``source_origin_label`` overrides the route-level label on
  the forward leg only.
* Per-entry ``dest_origin_label`` overrides the route-level label on
  the reverse leg only.
* An explicit empty-string per-entry label (``""``) is preserved as
  ``RouteSource.origin_label == ""`` — it does NOT fall through to the
  route-level label (explicit suppression sentinel).
* An explicit ``null`` per-entry label behaves like an absent key:
  both fall through to the route-level label.  ``None`` and ``""``
  must never be conflated.
* Unknown keys, and non-string label values, are rejected.
"""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.route_expansion import expand_route_config
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATA: dict[str, object] = {
    "source_adapters": ["radio_adapter"],
    "dest_adapters": ["chat_adapter"],
    "directionality": "bidirectional",
}


def _leg(routes: list, direction: str, source_context: str = "0"):
    """Return the single expanded leg for a direction and source context."""
    matches = [
        leg
        for leg in routes
        if leg.direction == direction and leg.mapping_source_context == source_context
    ]
    assert len(matches) == 1, (
        f"expected exactly one {direction!r} leg for source context "
        f"{source_context!r}, got ids={[leg.route.id for leg in routes]}"
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
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": "LongFast",
                    "dest_origin_label": "Chat Ops",
                },
            },
        },
    )
    assert rc.context_map is not None
    entry = rc.context_map["0"]
    assert isinstance(entry, ContextMapEntry)
    assert entry.dest_context == "!room0:example.com"
    assert entry.source_origin_label == "LongFast"
    assert entry.dest_origin_label == "Chat Ops"


def test_structured_entry_parses_context_only() -> None:
    """A structured entry with just ``dest_context`` (no labels) defaults to None."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "context_map": {"0": {"dest_context": "!room0:example.com"}},
        },
    )
    assert rc.context_map is not None
    entry = rc.context_map["0"]
    assert isinstance(entry, ContextMapEntry)
    assert entry.dest_context == "!room0:example.com"
    assert entry.source_origin_label is None
    assert entry.dest_origin_label is None


# ===========================================================================
# 2. Per-entry source_origin_label applies to the forward leg only
# ===========================================================================


def test_per_entry_source_label_on_forward_leg() -> None:
    """Entry source_origin_label overrides route-level on forward leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": "Entry Level",
                },
            },
        },
    )
    legs = expand_route_config(rc)
    fwd = _leg(legs, "source_to_dest")
    assert fwd.route.source.origin_label == "Entry Level"


def test_per_entry_source_label_other_context_keeps_route_label() -> None:
    """Only the entry with the label gets it; other entries fall back."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": "Entry Level",
                },
                "1": {"dest_context": "!room1:example.com"},
            },
        },
    )
    legs = expand_route_config(rc)
    fwd0 = _leg(legs, "source_to_dest", "0")
    fwd1 = _leg(legs, "source_to_dest", "1")
    assert fwd0.route.source.origin_label == "Entry Level"
    assert fwd1.route.source.origin_label == "Route Level"


# ===========================================================================
# 3. Per-entry dest_origin_label applies to the reverse leg only
# ===========================================================================


def test_per_entry_dest_label_on_reverse_leg() -> None:
    """Entry dest_origin_label overrides route-level on reverse leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "dest_origin_label": "Route Dest",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "dest_origin_label": "Entry Dest",
                },
            },
        },
    )
    legs = expand_route_config(rc)
    rev = _leg(legs, "dest_to_source")
    assert rev.route.source.origin_label == "Entry Dest"


# ===========================================================================
# 4. Explicit null vs empty string: fallback vs suppression
#
# An explicit YAML ``null`` (Python ``None``) means "fall back through
# the precedence chain"; an explicit empty string ``""`` means "suppress
# the fallback for this entry".  These tests make the contrast explicit
# at both the parse and expansion levels.
# ===========================================================================


def test_explicit_null_entry_source_label_falls_back_to_route() -> None:
    """Explicit None (YAML null) per-entry label falls back.

    Counterpart to the absent-key test: an explicit ``null`` must
    behave identically to an absent key — both produce ``None`` on the
    parsed entry, and both fall through to the route-level label during
    expansion.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": None,  # explicit YAML null
                },
            },
        },
    )
    # Parse level: explicit None is stored as None (not stripped, not "").
    assert rc.context_map is not None
    assert rc.context_map["0"].source_origin_label is None
    # Expansion level: None falls through to the route-level label.
    legs = expand_route_config(rc)
    fwd = _leg(legs, "source_to_dest")
    assert fwd.route.source.origin_label == "Route Level"


def test_explicit_empty_string_entry_label_suppresses_fallback() -> None:
    """Explicit '' per-entry label suppresses the fallback chain.

    The counterpart to the null test: ``null`` falls back, ``""`` does
    NOT.  The route-level label is ignored when the entry carries an
    explicit empty string; the expanded leg's ``origin_label`` stays
    ``""`` so renderers suppress the adapter-level fallback.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": "",  # explicit empty string
                },
            },
        },
    )
    # Parse level: explicit "" is stored as "" (distinct from None).
    assert rc.context_map is not None
    entry = rc.context_map["0"]
    assert entry.source_origin_label == ""
    assert entry.source_origin_label is not None
    # Expansion level: "" is preserved — route-level label is NOT used.
    legs = expand_route_config(rc)
    fwd = _leg(legs, "source_to_dest")
    assert fwd.route.source.origin_label == ""


def test_explicit_null_and_empty_string_contrast_in_same_route() -> None:
    """Null and empty string behave differently in the same route.

    Two entries on different contexts — one with explicit ``None``,
    one with explicit ``""`` — must expand to different origin_labels.
    This guards against a regression where ``None`` and ``""`` are
    conflated.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_DATA,
            "source_origin_label": "Route Level",
            "context_map": {
                "0": {
                    "dest_context": "!room0:example.com",
                    "source_origin_label": None,  # falls back
                },
                "1": {
                    "dest_context": "!room1:example.com",
                    "source_origin_label": "",  # suppresses
                },
            },
        },
    )
    legs = expand_route_config(rc)
    fwd0 = _leg(legs, "source_to_dest", "0")
    fwd1 = _leg(legs, "source_to_dest", "1")
    # Context 0: None -> route-level label.
    assert fwd0.route.source.origin_label == "Route Level"
    # Context 1: "" -> stays empty (suppressed).
    assert fwd1.route.source.origin_label == ""


# ===========================================================================
# 5. Unknown map-entry key is rejected
# ===========================================================================


def test_unknown_entry_key_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="unknown key"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "context_map": {
                    "0": {
                        "dest_context": "!room0:example.com",
                        "label": "bad key",
                    },
                },
            },
        )


# ===========================================================================
# 6. Non-string label values are rejected (bool-before-str)
# ===========================================================================


def test_bool_source_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "context_map": {
                    "0": {
                        "dest_context": "!room0:example.com",
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
                "context_map": {
                    "0": {
                        "dest_context": "!room0:example.com",
                        "dest_origin_label": False,
                    },
                },
            },
        )


def test_int_label_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be a string"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "context_map": {
                    "0": {
                        "dest_context": "!room0:example.com",
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
                "context_map": {
                    "0": {
                        "dest_context": "!room0:example.com",
                        "dest_origin_label": ["a", "b"],
                    },
                },
            },
        )


def test_non_str_non_dict_value_rejected() -> None:
    """A raw entry value that is neither a string nor a dict is rejected."""
    with pytest.raises(ConfigValidationError, match="must be a table"):
        RouteConfig.from_dict(
            "t",
            {
                **_BASE_DATA,
                "context_map": {"0": 12345},
            },
        )
