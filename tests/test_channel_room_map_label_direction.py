"""Direction-aware per-entry label behavior for ``channel_room_map``.

The smoke tests in ``test_channel_room_map_context_labels.py`` cover the
basic per-entry label parsing and a single-channel override.  This file
covers the **direction-aware** scenarios that operators actually rely
on: distinct per-channel labels landing on the correct expanded leg
when the map has multiple channels, when the source/dest orientation is
reversed (Meshtastic source), and when both per-entry labels are present
on the same entry (forward leg uses ``source_origin_label`` only, reverse
leg uses ``dest_origin_label`` only).

Invariants verified here that are NOT already covered by the smoke
tests:

* Two channels targeting two different Matrix rooms can carry distinct
  ``source_origin_label`` values and each expanded forward leg carries
  the matching label.  This is the headline operator use case for the
  feature: per-channel attribution.  (Two channels pointing to the
  SAME room is intentionally rejected by the parser — duplicate rooms
  are a documented invariant in audit §7.3.)
* Two channels targeting different Matrix rooms with different labels
  also resolve the per-leg labels correctly across both the forward
  and reverse legs.
* A per-entry ``source_origin_label`` lands on the forward leg only and
  does **not** leak onto the reverse leg of the same entry.
* A per-entry ``dest_origin_label`` lands on the reverse leg only.
* When a per-entry label is present alongside a route-level label of
  the **other** direction, each side wins for its own leg (entry source
  on forward, route-level dest on reverse when no entry dest is set).
* The Meshtastic→Matrix source orientation (source=Meshtastic,
  dest=Matrix) also honours per-entry ``source_origin_label`` on its
  forward leg (the ``meshtastic_to_matrix`` leg) and per-entry
  ``dest_origin_label`` on its reverse leg.
* A bidirectional map with both per-entry labels set produces a forward
  leg carrying the entry source label and a reverse leg carrying the
  entry dest label for the same channel.

All tests use ``RouteConfig.from_dict`` and
:func:`build_runtime_routes` so the parsing + expansion path is the
same one operators hit through YAML config.
"""

from __future__ import annotations

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

# Two platform orientations are exercised so we prove that per-entry
# labels land on the correct *direction-aware* leg regardless of which
# adapter is the declared source.
_PLATFORMS_MATRIX_SOURCE: dict[str, str] = {
    "matrix_adapter": "matrix",
    "mesh_adapter": "meshtastic",
}
_PLATFORMS_MESH_SOURCE: dict[str, str] = {
    "mesh_adapter": "meshtastic",
    "matrix_adapter": "matrix",
}

_BASE_MATRIX_SOURCE: dict[str, object] = {
    "source_adapters": ["matrix_adapter"],
    "dest_adapters": ["mesh_adapter"],
    "directionality": "bidirectional",
}

_BASE_MESH_SOURCE: dict[str, object] = {
    "source_adapters": ["mesh_adapter"],
    "dest_adapters": ["matrix_adapter"],
    "directionality": "bidirectional",
}


def _expand_matrix_source(rc: RouteConfig) -> list[Route]:
    rcs = RouteConfigSet(routes=(rc,))
    return build_runtime_routes(rcs, _PLATFORMS_MATRIX_SOURCE)


def _expand_mesh_source(rc: RouteConfig) -> list[Route]:
    rcs = RouteConfigSet(routes=(rc,))
    return build_runtime_routes(rcs, _PLATFORMS_MESH_SOURCE)


def _leg(routes: list[Route], direction: str, channel: str = "0") -> Route:
    """Return the single expanded route for a direction and channel.

    ``direction`` is a substring of the expanded route id, e.g.
    ``"matrix_to_meshtastic"`` or ``"meshtastic_to_matrix"``.
    """
    matches = [r for r in routes if direction in r.id and f"__ch{channel}__" in r.id]
    assert len(matches) == 1, (
        f"expected exactly one {direction!r} leg on channel {channel!r}, "
        f"got ids={[r.id for r in routes]}"
    )
    return matches[0]


def _structured_entry(
    room: str,
    *,
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
) -> dict[str, object]:
    """Build a structured channel_room_map entry dict for clarity in tests."""
    entry: dict[str, object] = {"room": room}
    if source_origin_label is not None:
        entry["source_origin_label"] = source_origin_label
    if dest_origin_label is not None:
        entry["dest_origin_label"] = dest_origin_label
    return entry


# ===========================================================================
# 1. Distinct per-channel labels land on distinct forward legs
#
# Note on the "same room, different channel" scenario: the config
# parser intentionally rejects duplicate Matrix rooms inside a single
# ``channel_room_map`` (see ``src/medre/config/routes.py:705-711``,
# covered by ``test_routes_channel_room_map.py::TestChannelRoomMapConfig
# ::test_reject_duplicate_room``).  The realistic operator pattern for
# per-channel labelling is therefore one channel per room, each
# carrying its own label.  These tests prove each expanded forward leg
# carries the per-entry label of its own channel and not its sibling's.
# ===========================================================================


def test_two_channels_distinct_source_labels_matrix_source() -> None:
    """Two channels → two rooms, distinct per-entry source labels.

    Each forward (Matrix→Meshtastic) leg must carry its own per-entry
    label, not its sibling's.
    """
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
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
    routes = _expand_matrix_source(rc)

    fwd_ch0 = _leg(routes, "matrix_to_meshtastic", "0")
    fwd_ch1 = _leg(routes, "matrix_to_meshtastic", "1")
    # Each forward leg resolves its own room and its own label.
    assert fwd_ch0.source.channel == "!room0:example.com"
    assert fwd_ch1.source.channel == "!room1:example.com"
    assert fwd_ch0.source.origin_label == "Ops Channel"
    assert fwd_ch1.source.origin_label == "Tactical Net"


def test_two_channels_distinct_labels_mesh_source() -> None:
    """Source = Meshtastic: per-entry source label lands on meshtastic_to_matrix leg."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MESH_SOURCE,
            "channel_room_map": {
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
    routes = _expand_mesh_source(rc)

    # Forward (source→dest) leg is meshtastic_to_matrix here.
    fwd_ch0 = _leg(routes, "meshtastic_to_matrix", "0")
    fwd_ch1 = _leg(routes, "meshtastic_to_matrix", "1")
    assert fwd_ch0.source.origin_label == "Ops Channel"
    assert fwd_ch1.source.origin_label == "Tactical Net"


# ===========================================================================
# 2. Different rooms, different channels, distinct labels
# ===========================================================================


def test_distinct_rooms_distinct_channels_distinct_labels() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
                "0": _structured_entry(
                    "!roomA:example.com",
                    source_origin_label="Ops A",
                ),
                "1": _structured_entry(
                    "!roomB:example.com",
                    source_origin_label="Ops B",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd_a = _leg(routes, "matrix_to_meshtastic", "0")
    fwd_b = _leg(routes, "matrix_to_meshtastic", "1")
    assert fwd_a.source.channel == "!roomA:example.com"
    assert fwd_b.source.channel == "!roomB:example.com"
    assert fwd_a.source.origin_label == "Ops A"
    assert fwd_b.source.origin_label == "Ops B"


# ===========================================================================
# 3. Per-entry source_origin_label does NOT leak onto the reverse leg
#
# When only ``source_origin_label`` is set on an entry, the reverse leg
# of that entry must inherit the route-level ``dest_origin_label``
# (which may be unset / None).  The entry source label must not leak.
# ===========================================================================


def test_entry_source_label_does_not_leak_onto_reverse_leg() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd = _leg(routes, "matrix_to_meshtastic", "0")
    rev = _leg(routes, "meshtastic_to_matrix", "0")
    # Forward leg: entry source label wins.
    assert fwd.source.origin_label == "Entry Src"
    # Reverse leg: entry source label does NOT leak; route dest wins
    # because the entry has no dest_origin_label.
    assert rev.source.origin_label == "Route Dst"


def test_entry_dest_label_does_not_leak_onto_forward_leg() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd = _leg(routes, "matrix_to_meshtastic", "0")
    rev = _leg(routes, "meshtastic_to_matrix", "0")
    # Forward leg: entry has no source label → route source wins.
    assert fwd.source.origin_label == "Route Src"
    # Reverse leg: entry dest label wins.
    assert rev.source.origin_label == "Entry Dst"


# ===========================================================================
# 4. Both per-entry labels on the same entry
#
# Entry sets both source and dest labels.  Forward leg carries the
# entry source label, reverse leg carries the entry dest label.
# Route-level labels are entirely overridden for this entry.
# ===========================================================================


def test_both_entry_labels_override_both_route_labels_matrix_source() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd = _leg(routes, "matrix_to_meshtastic", "0")
    rev = _leg(routes, "meshtastic_to_matrix", "0")
    assert fwd.source.origin_label == "Entry Src"
    assert rev.source.origin_label == "Entry Dst"


def test_both_entry_labels_override_both_route_labels_mesh_source() -> None:
    """Same entry-both-labels behaviour with Meshtastic as the source."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MESH_SOURCE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    routes = _expand_mesh_source(rc)

    # With mesh as source: forward = meshtastic_to_matrix, reverse = matrix_to_meshtastic.
    fwd = _leg(routes, "meshtastic_to_matrix", "0")
    rev = _leg(routes, "matrix_to_meshtastic", "0")
    assert fwd.source.origin_label == "Entry Src"
    assert rev.source.origin_label == "Entry Dst"


# ===========================================================================
# 5. Entry-only labels with no route-level fallback (route labels unset)
#
# If route-level labels are unset, the per-entry labels still apply.
# The reverse leg of an entry that only sets a source label falls back
# to None (because the route dest label is also None).
# ===========================================================================


def test_entry_source_label_only_no_route_label_reverse_is_none() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd = _leg(routes, "matrix_to_meshtastic", "0")
    rev = _leg(routes, "meshtastic_to_matrix", "0")
    assert fwd.source.origin_label == "Entry Src"
    # Reverse leg: no entry dest, no route dest → None.
    assert rev.source.origin_label is None


def test_entry_dest_label_only_no_route_label_forward_is_none() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    fwd = _leg(routes, "matrix_to_meshtastic", "0")
    rev = _leg(routes, "meshtastic_to_matrix", "0")
    assert fwd.source.origin_label is None
    assert rev.source.origin_label == "Entry Dst"


# ===========================================================================
# 6. Multi-entry map where some entries override and some inherit
#
# Realistic operator config: one channel carries an entry label, the
# other relies on the route-level label.  This proves the per-entry
# override is genuinely per-entry and does not bleed across channels.
# ===========================================================================


def test_mixed_entries_one_overrides_one_inherits() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "source_origin_label": "Route Src",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src 0",
                    dest_origin_label="Entry Dst 0",
                ),
                "1": _structured_entry("!room1:example.com"),
            },
        },
    )
    routes = _expand_matrix_source(rc)

    # Channel 0: both legs use the entry labels.
    fwd0 = _leg(routes, "matrix_to_meshtastic", "0")
    rev0 = _leg(routes, "meshtastic_to_matrix", "0")
    assert fwd0.source.origin_label == "Entry Src 0"
    assert rev0.source.origin_label == "Entry Dst 0"

    # Channel 1: both legs fall back to the route-level labels.
    fwd1 = _leg(routes, "matrix_to_meshtastic", "1")
    rev1 = _leg(routes, "meshtastic_to_matrix", "1")
    assert fwd1.source.origin_label == "Route Src"
    assert rev1.source.origin_label == "Route Dst"


# ===========================================================================
# 7. Single-direction route (source_to_dest) honours per-entry source label
#
# ``source_to_dest`` produces only the forward leg.  Per-entry
# ``source_origin_label`` must still override the route-level label on
# that leg.  ``dest_origin_label`` on the entry is irrelevant because
# no reverse leg is created.
# ===========================================================================


def test_source_to_dest_per_entry_source_label_applies() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            "source_adapters": ["matrix_adapter"],
            "dest_adapters": ["mesh_adapter"],
            "directionality": "source_to_dest",
            "source_origin_label": "Route Src",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    source_origin_label="Entry Src",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)
    # Only the forward leg exists.
    assert len(routes) == 1
    assert "matrix_to_meshtastic" in routes[0].id
    assert routes[0].source.origin_label == "Entry Src"


def test_dest_to_source_per_entry_dest_label_applies() -> None:
    rc = RouteConfig.from_dict(
        "t",
        {
            "source_adapters": ["matrix_adapter"],
            "dest_adapters": ["mesh_adapter"],
            "directionality": "dest_to_source",
            "dest_origin_label": "Route Dst",
            "channel_room_map": {
                "0": _structured_entry(
                    "!room0:example.com",
                    dest_origin_label="Entry Dst",
                ),
            },
        },
    )
    routes = _expand_matrix_source(rc)
    # Only the reverse leg exists.
    assert len(routes) == 1
    assert "meshtastic_to_matrix" in routes[0].id
    assert routes[0].source.origin_label == "Entry Dst"


# ===========================================================================
# 8. ChannelRoomMapEntry equality: labeled vs unlabeled vs bare-string
#
# Per the audit §8.1, an entry with no labels compares equal to its
# bare room string (backward compatibility).  An entry with any label
# does NOT compare equal to a bare string.  These invariants are what
# lets existing code that compares a normalised map against a flat
# ``dict[str, str]`` continue to work when no labels are set.
# ===========================================================================


def test_unlabeled_entry_equals_bare_room_string() -> None:
    entry = ChannelRoomMapEntry(room="!r:example.com")
    assert entry == "!r:example.com"
    assert hash(entry) == hash("!r:example.com")


def test_source_labeled_entry_does_not_equal_bare_room_string() -> None:
    entry = ChannelRoomMapEntry(room="!r:example.com", source_origin_label="S")
    assert entry != "!r:example.com"


def test_dest_labeled_entry_does_not_equal_bare_room_string() -> None:
    entry = ChannelRoomMapEntry(room="!r:example.com", dest_origin_label="D")
    assert entry != "!r:example.com"


def test_labeled_entries_equal_only_when_all_fields_match() -> None:
    a = ChannelRoomMapEntry(
        room="!r:example.com", source_origin_label="S", dest_origin_label="D"
    )
    b = ChannelRoomMapEntry(
        room="!r:example.com", source_origin_label="S", dest_origin_label="D"
    )
    c = ChannelRoomMapEntry(
        room="!r:example.com", source_origin_label="S", dest_origin_label="X"
    )
    assert a == b
    assert a != c


def test_unlabeled_entry_dict_equals_legacy_flat_dict() -> None:
    """A normalised map with only unlabeled entries equals a ``dict[str, str]``."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
                "0": _structured_entry("!room0:example.com"),
                "1": _structured_entry("!room1:example.com"),
            },
        },
    )
    assert rc.channel_room_map is not None
    assert rc.channel_room_map == {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }


def test_labeled_entry_dict_does_not_equal_legacy_flat_dict() -> None:
    """Once any entry has a label, the normalised map no longer equals
    the legacy ``dict[str, str]`` shape."""
    rc = RouteConfig.from_dict(
        "t",
        {
            **_BASE_MATRIX_SOURCE,
            "channel_room_map": {
                "0": _structured_entry("!room0:example.com", source_origin_label="X"),
                "1": _structured_entry("!room1:example.com"),
            },
        },
    )
    assert rc.channel_room_map is not None
    assert rc.channel_room_map != {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }
