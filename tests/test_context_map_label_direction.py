"""Direction-aware per-entry label behavior for ``context_map``.

The tests in ``test_context_map_context_labels.py`` cover the basic
per-entry label parsing and single-entry override.  This file covers the
**direction-aware** scenarios operators rely on: distinct per-entry
labels landing on the correct expanded leg when the map has multiple
entries, no cross-side leaking, and route-level fallback for the side an
entry does not label.

Invariants verified:

* Two entries targeting two different dest contexts carry distinct
  ``source_origin_label`` values and each expanded forward leg carries
  the matching label — the headline operator use case: per-context
  attribution.
* A per-entry ``source_origin_label`` lands on the forward leg only and
  does **not** leak onto the reverse leg of the same entry.
* A per-entry ``dest_origin_label`` lands on the reverse leg only.
* When a per-entry label is present alongside a route-level label of
  the **other** direction, each side wins for its own leg.
* Entries that do not label a side fall back to the route-level label
  (or ``None`` when the route-level label is unset too).
* Single-direction routes still honour the per-entry label on the one
  leg they create.
* Entry equality/hash is by structured fields.

All tests use ``RouteConfig.from_dict`` and
:func:`expand_route_config` so the parsing + expansion path is the same
one operators hit through YAML config.
"""

from __future__ import annotations

from medre.config.route_expansion import expand_route_config
from medre.config.routes import ContextMapEntry, RouteConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE: dict[str, object] = {
    "source_adapters": ["radio_adapter"],
    "dest_adapters": ["chat_adapter"],
    "directionality": "bidirectional",
}


def _structured_entry(
    dest_context: str,
    *,
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
) -> dict[str, object]:
    """Build a structured context_map entry dict for clarity in tests."""
    entry: dict[str, object] = {"dest_context": dest_context}
    if source_origin_label is not None:
        entry["source_origin_label"] = source_origin_label
    if dest_origin_label is not None:
        entry["dest_origin_label"] = dest_origin_label
    return entry


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
# 1. Distinct per-entry labels land on distinct forward legs
# ===========================================================================


def test_two_entries_distinct_source_labels() -> None:
    """Two entries → two dest contexts, distinct per-entry source labels.

    Each forward leg must carry its own per-entry label, not its
    sibling's.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Ops Channel",
                ),
                "1": _structured_entry(
                    "!room1:example.com",
                    source_origin_label="Tactical Net",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd0 = _leg(legs, "source_to_dest", "0")
    fwd1 = _leg(legs, "source_to_dest", "1")
    # Each forward leg resolves its own source context and its own label.
    assert fwd0.route.source.channel == "0"
    assert fwd1.route.source.channel == "1"
    assert fwd0.route.source.origin_label == "Ops Channel"
    assert fwd1.route.source.origin_label == "Tactical Net"


def test_distinct_dest_contexts_distinct_labels() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "context_map": {
                "chan-a": _structured_entry(
                    "!roomA:example.com",
                    source_origin_label="Ops A",
                ),
                "chan-b": _structured_entry(
                    "!roomB:example.com",
                    source_origin_label="Ops B",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd_a = _leg(legs, "source_to_dest", "chan-a")
    fwd_b = _leg(legs, "source_to_dest", "chan-b")
    assert fwd_a.route.source.channel == "chan-a"
    assert fwd_a.route.targets[0].channel == "!roomA:example.com"
    assert fwd_b.route.targets[0].channel == "!roomB:example.com"
    assert fwd_a.route.source.origin_label == "Ops A"
    assert fwd_b.route.source.origin_label == "Ops B"


# ===========================================================================
# 2. Per-entry labels do not leak across legs
# ===========================================================================


def test_entry_source_label_does_not_leak_onto_reverse_leg() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd = _leg(legs, "source_to_dest", "0")
    rev = _leg(legs, "dest_to_source", "0")
    # Forward leg: entry source label wins.
    assert fwd.route.source.origin_label == "Entry Src"
    # Reverse leg: entry source label does NOT leak; route dest wins
    # because the entry has no dest_origin_label.
    assert rev.route.source.origin_label == "Route Dst"


def test_entry_dest_label_does_not_leak_onto_forward_leg() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd = _leg(legs, "source_to_dest", "0")
    rev = _leg(legs, "dest_to_source", "0")
    # Forward leg: entry has no source label → route source wins.
    assert fwd.route.source.origin_label == "Route Src"
    # Reverse leg: entry dest label wins.
    assert rev.route.source.origin_label == "Entry Dst"


# ===========================================================================
# 3. Both per-entry labels on the same entry
# ===========================================================================


def test_both_entry_labels_override_both_route_labels() -> None:
    """Entry sets both labels: forward carries the entry source label,
    reverse carries the entry dest label.  Route-level labels are
    entirely overridden for this entry."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd = _leg(legs, "source_to_dest", "0")
    rev = _leg(legs, "dest_to_source", "0")
    assert fwd.route.source.origin_label == "Entry Src"
    assert rev.route.source.origin_label == "Entry Dst"


# ===========================================================================
# 4. Entry-only labels with no route-level fallback
# ===========================================================================


def test_entry_source_label_only_no_route_label_reverse_is_none() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd = _leg(legs, "source_to_dest", "0")
    rev = _leg(legs, "dest_to_source", "0")
    assert fwd.route.source.origin_label == "Entry Src"
    # Reverse leg: no entry dest, no route dest → None.
    assert rev.route.source.origin_label is None


def test_entry_dest_label_only_no_route_label_forward_is_none() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    legs = expand_route_config(rc)

    fwd = _leg(legs, "source_to_dest", "0")
    rev = _leg(legs, "dest_to_source", "0")
    assert fwd.route.source.origin_label is None
    assert rev.route.source.origin_label == "Entry Dst"


# ===========================================================================
# 5. Multi-entry map: some entries override, some inherit
# ===========================================================================


def test_mixed_entries_one_overrides_one_inherits() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src 0",
                    dest_origin_label="Entry Dst 0",
                ),
                "1": _structured_entry("!room1:example.com"),
            },
        },
    )
    legs = expand_route_config(rc)

    # Entry 0: both legs use the entry labels.
    fwd0 = _leg(legs, "source_to_dest", "0")
    rev0 = _leg(legs, "dest_to_source", "0")
    assert fwd0.route.source.origin_label == "Entry Src 0"
    assert rev0.route.source.origin_label == "Entry Dst 0"

    # Entry 1: both legs fall back to the route-level labels.
    fwd1 = _leg(legs, "source_to_dest", "1")
    rev1 = _leg(legs, "dest_to_source", "1")
    assert fwd1.route.source.origin_label == "Route Src"
    assert rev1.route.source.origin_label == "Route Dst"


# ===========================================================================
# 6. Single-direction routes still honour per-entry labels
# ===========================================================================


def test_source_to_dest_per_entry_source_label_applies() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            "source_adapters": ["radio_adapter"],
            "dest_adapters": ["chat_adapter"],
            "directionality": "source_to_dest",
            "source_origin_label": "Route Src",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    legs = expand_route_config(rc)
    # Only the forward leg exists.
    assert len(legs) == 1
    assert legs[0].route.id.startswith("t__maph")
    assert legs[0].route.id.endswith("__fwd")
    assert legs[0].route.source.origin_label == "Entry Src"


def test_dest_to_source_per_entry_dest_label_applies() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            "source_adapters": ["radio_adapter"],
            "dest_adapters": ["chat_adapter"],
            "directionality": "dest_to_source",
            "dest_origin_label": "Route Dst",
            "context_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    legs = expand_route_config(rc)
    # Only the reverse leg exists.
    assert len(legs) == 1
    assert legs[0].route.id.startswith("t__maph")
    assert legs[0].route.id.endswith("__rev")
    assert legs[0].route.source.origin_label == "Entry Dst"


# ===========================================================================
# 7. ContextMapEntry equality / hash
# ===========================================================================


def test_unlabeled_entries_compare_by_structured_fields() -> None:
    a = ContextMapEntry(dest_context="!r:example.com")
    b = ContextMapEntry(dest_context="!r:example.com")
    c = ContextMapEntry(dest_context="!other:example.com")
    assert a == b
    assert hash(a) == hash(b)
    assert a != c


def test_labeled_entries_equal_only_when_all_fields_match() -> None:
    a = ContextMapEntry(
        dest_context="!r:example.com",
        source_origin_label="S",
        dest_origin_label="D",
    )
    b = ContextMapEntry(
        dest_context="!r:example.com",
        source_origin_label="S",
        dest_origin_label="D",
    )
    c = ContextMapEntry(
        dest_context="!r:example.com",
        source_origin_label="S",
        dest_origin_label="X",
    )
    assert a == b
    assert a != c


def test_structured_entry_does_not_equal_plain_string() -> None:
    assert ContextMapEntry(dest_context="!r:example.com") != "!r:example.com"
