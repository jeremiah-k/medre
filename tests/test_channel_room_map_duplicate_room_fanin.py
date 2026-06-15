"""Duplicate-room fan-in behaviour for ``channel_room_map`` routes.

Wave 1 moved the duplicate-room check out of the config parser
(:mod:`medre.config.routes`) and into runtime route expansion
(:mod:`medre.runtime.route_engine`).  Config parsing now *accepts*
duplicate Matrix room values inside a single ``channel_room_map``.
Whether duplicates are legal is decided at expansion time, once the
adapter platforms and the route's directionality are known:

* Duplicate rooms are **safe** when the only leg they create is
  Meshtastic → Matrix (fan-in): the inbound radio channel disambiguates
  the source event.
* Duplicate rooms are **ambiguous** when the route also creates a
  Matrix → Meshtastic leg, because a Matrix event arriving from the
  shared room could target multiple Meshtastic channels with no way to
  pick one.  The runtime rejects this with :class:`RouteValidationError`.

This file exercises the full directionality matrix, the per-channel
origin-label behaviour on the allowed fan-in path, and the unchanged
"duplicate channel keys are still rejected" guarantee at config parse
time.
"""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.routes import (
    ChannelRoomMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.runtime.route_engine import (
    RouteValidationError,
    build_runtime_routes,
)

# ---------------------------------------------------------------------------
# Adapter-platform mappings for the two source/dest orientations.
# ---------------------------------------------------------------------------

_PLATFORMS_MATRIX_SOURCE: dict[str, str] = {
    "matrix_adapter": "matrix",
    "mesh_adapter": "meshtastic",
}
_PLATFORMS_MESH_SOURCE: dict[str, str] = {
    "mesh_adapter": "meshtastic",
    "matrix_adapter": "matrix",
}

_SHARED_ROOM = "!shared:example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_routes(rc: RouteConfig, platforms: dict[str, str]) -> list:
    rcs = RouteConfigSet(routes=(rc,))
    return build_runtime_routes(rcs, platforms)


def _mesh_source_route(
    *,
    directionality: RouteDirectionality,
    channel_room_map: dict[str, ChannelRoomMapEntry],
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
) -> RouteConfig:
    """A route with Meshtastic as source, Matrix as dest."""
    return RouteConfig(
        route_id="fanin",
        source_adapters=("mesh_adapter",),
        dest_adapters=("matrix_adapter",),
        directionality=directionality,
        channel_room_map=channel_room_map,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )


def _matrix_source_route(
    *,
    directionality: RouteDirectionality,
    channel_room_map: dict[str, ChannelRoomMapEntry],
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
) -> RouteConfig:
    """A route with Matrix as source, Meshtastic as dest."""
    return RouteConfig(
        route_id="fanout",
        source_adapters=("matrix_adapter",),
        dest_adapters=("mesh_adapter",),
        directionality=directionality,
        channel_room_map=channel_room_map,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )


# ===========================================================================
# 1. Allowed: Meshtastic → Matrix fan-in with duplicate rooms
# ===========================================================================


def test_mesh_source_same_room_source_to_dest_allowed() -> None:
    """Two Meshtastic channels fanning into one Matrix room: allowed.

    ``source_to_dest`` with a Meshtastic source produces only
    Meshtastic→Matrix legs, so duplicate target rooms are unambiguous
    (the inbound channel disambiguates the source).  Each channel
    expands into its own ``meshtastic_to_matrix`` leg with the
    deterministic ID ``{route_id}__ch{ch}__meshtastic_to_matrix``.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM, source_origin_label="Ops"),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM, source_origin_label="Tactical"),
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
    )
    routes = _build_routes(rc, _PLATFORMS_MESH_SOURCE)

    # Two legs, one per channel, both targeting the same Matrix room.
    assert len(routes) == 2
    by_id = {r.id: r for r in routes}
    expected_ids = {
        "fanin__ch0__meshtastic_to_matrix",
        "fanin__ch1__meshtastic_to_matrix",
    }
    assert set(by_id) == expected_ids

    for ch, label in (("0", "Ops"), ("1", "Tactical")):
        leg = by_id[f"fanin__ch{ch}__meshtastic_to_matrix"]
        # Source side: Meshtastic adapter, channel-scoped.
        assert leg.source.adapter == "mesh_adapter"
        assert leg.source.channel == ch
        assert leg.source.origin_label == label
        # Target side: Matrix adapter pointing at the shared room.
        assert len(leg.targets) == 1
        assert leg.targets[0].adapter == "matrix_adapter"
        assert leg.targets[0].channel == _SHARED_ROOM


def test_per_channel_origin_labels_distinct_for_shared_room() -> None:
    """Each channel's leg carries its OWN source_origin_label.

    Even though both legs target the same Matrix room, the per-channel
    labels must not bleed across channels — channel 0 keeps its label,
    channel 1 keeps its own.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM, source_origin_label="Alpha"),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM, source_origin_label="Beta"),
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
    )
    routes = _build_routes(rc, _PLATFORMS_MESH_SOURCE)

    by_channel = {r.source.channel: r for r in routes}
    assert by_channel["0"].source.origin_label == "Alpha"
    assert by_channel["1"].source.origin_label == "Beta"
    # Sanity: both target the shared room.
    assert all(r.targets[0].channel == _SHARED_ROOM for r in routes)


def test_dest_to_source_with_matrix_source_only_creates_mesh_to_matrix() -> None:
    """``dest_to_source`` with a Matrix source produces mesh→matrix legs.

    The *other* allowed direction: with Matrix declared as source and
    Meshtastic as dest, ``dest_to_source`` reverses the flow into
    Meshtastic→Matrix (the inbound channel disambiguates).  Duplicate
    rooms are legal here.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),
    }
    rc = _matrix_source_route(
        directionality=RouteDirectionality.DEST_TO_SOURCE,
        channel_room_map=crm,
    )
    routes = _build_routes(rc, _PLATFORMS_MATRIX_SOURCE)
    # dest_to_source with matrix source → only meshtastic_to_matrix legs.
    assert len(routes) == 2
    assert all("meshtastic_to_matrix" in r.id for r in routes)
    assert all(r.source.adapter == "mesh_adapter" for r in routes)
    assert all(r.targets[0].channel == _SHARED_ROOM for r in routes)


# ===========================================================================
# 2. Rejected: any route that creates a Matrix → Meshtastic leg
# ===========================================================================


def test_matrix_source_same_room_source_to_dest_rejected() -> None:
    """Matrix source + source_to_dest → matrix→mesh leg → REJECTED.

    The duplicate rooms make Matrix→Meshtastic routing ambiguous: a
    Matrix event from the shared room could target multiple Meshtastic
    channels.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),
    }
    rc = _matrix_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
    )
    with pytest.raises(
        RouteValidationError,
        match=r"Matrix→Meshtastic",
    ) as exc_info:
        _build_routes(rc, _PLATFORMS_MATRIX_SOURCE)
    assert "ambiguous" in str(exc_info.value)
    assert _SHARED_ROOM in str(exc_info.value)


def test_mesh_source_same_room_dest_to_source_rejected() -> None:
    """Meshtastic source + dest_to_source → reverse matrix→mesh → REJECTED.

    Even though the source adapter is Meshtastic, ``dest_to_source``
    reverses the flow: the reverse leg becomes Matrix→Meshtastic,
    which is ambiguous for a shared room.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.DEST_TO_SOURCE,
        channel_room_map=crm,
    )
    with pytest.raises(RouteValidationError, match=r"Matrix→Meshtastic"):
        _build_routes(rc, _PLATFORMS_MESH_SOURCE)


@pytest.mark.parametrize(
    "route_factory, platforms",
    [
        # Matrix declared as source: forward = matrix→mesh.
        (_matrix_source_route, _PLATFORMS_MATRIX_SOURCE),
        # Meshtastic declared as source: reverse = matrix→mesh.
        (_mesh_source_route, _PLATFORMS_MESH_SOURCE),
    ],
    ids=["matrix_source", "mesh_source"],
)
def test_bidirectional_same_room_rejected_either_order(
    route_factory, platforms
) -> None:
    """``bidirectional`` always creates a matrix→mesh leg → REJECTED.

    Regardless of which adapter is declared as the source, a
    bidirectional channel_room_map route produces both legs, so the
    Matrix→Meshtastic leg always exists.  Duplicate rooms are therefore
    always ambiguous for bidirectional maps.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),
    }
    rc = route_factory(
        directionality=RouteDirectionality.BIDIRECTIONAL,
        channel_room_map=crm,
    )
    with pytest.raises(RouteValidationError, match=r"Matrix→Meshtastic"):
        _build_routes(rc, platforms)


def test_rejection_message_lists_duplicate_rooms() -> None:
    """The RouteValidationError lists the sorted duplicate room IDs."""
    crm = {
        "0": ChannelRoomMapEntry(room="!aaa:example.com"),
        "1": ChannelRoomMapEntry(room="!aaa:example.com"),
        "2": ChannelRoomMapEntry(room="!bbb:example.com"),
        "3": ChannelRoomMapEntry(room="!bbb:example.com"),
    }
    rc = _matrix_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
    )
    with pytest.raises(RouteValidationError) as exc_info:
        _build_routes(rc, _PLATFORMS_MATRIX_SOURCE)
    msg = str(exc_info.value)
    # Sorted duplicate rooms appear in the message.
    assert "!aaa:example.com" in msg
    assert "!bbb:example.com" in msg
    assert "Route 'fanout'" in msg


# ===========================================================================
# 3. Config parsing: duplicate rooms allowed, duplicate channels rejected
# ===========================================================================


def test_config_from_dict_allows_duplicate_rooms() -> None:
    """``RouteConfig.from_dict`` accepts duplicate room values.

    The old ``seen_rooms`` check that raised ``ConfigValidationError``
    with ``"duplicate room"`` at parse time has been removed.  Duplicate
    rooms are now validated at expansion time, where directionality is
    known.
    """
    data = {
        "source_adapters": ["mesh_adapter"],
        "dest_adapters": ["matrix_adapter"],
        "directionality": "source_to_dest",
        "channel_room_map": {
            "0": _SHARED_ROOM,
            "1": _SHARED_ROOM,
        },
    }
    rc = RouteConfig.from_dict("fanin_route", data)
    assert rc.channel_room_map is not None
    assert set(rc.channel_room_map.keys()) == {"0", "1"}
    # Both entries normalise to the same room value.
    assert rc.channel_room_map["0"] == _SHARED_ROOM
    assert rc.channel_room_map["1"] == _SHARED_ROOM


def test_duplicate_channel_keys_still_rejected() -> None:
    """Duplicate CHANNEL keys remain a config-parse error.

    Only duplicate ROOM *values* were relaxed; duplicate channel keys
    (e.g. string ``"1"`` and int ``1`` both normalising to ``"1"``) are
    still rejected at parse time.
    """
    data = {
        "source_adapters": ["mesh_adapter"],
        "dest_adapters": ["matrix_adapter"],
        "channel_room_map": {
            "1": "!room_one:example.com",
            1: "!room_one_dup:example.com",
        },
    }
    with pytest.raises(ConfigValidationError, match="duplicate channel"):
        RouteConfig.from_dict("bad", data)


# ===========================================================================
# 4. Baseline: unique rooms still expand correctly
# ===========================================================================


def test_unique_rooms_baseline_expands() -> None:
    """A normal channel_room_map with distinct rooms expands as before.

    Regression guard: the new duplicate-room check must be a no-op when
    there are no duplicates, regardless of directionality.
    """
    crm = {
        "0": ChannelRoomMapEntry(room="!room0:example.com"),
        "1": ChannelRoomMapEntry(room="!room1:example.com"),
    }
    rc = _matrix_source_route(
        directionality=RouteDirectionality.BIDIRECTIONAL,
        channel_room_map=crm,
    )
    routes = _build_routes(rc, _PLATFORMS_MATRIX_SOURCE)
    # 2 channels × bidirectional = 4 routes.
    assert len(routes) == 4
    ids = sorted(r.id for r in routes)
    assert ids == [
        "fanout__ch0__matrix_to_meshtastic",
        "fanout__ch0__meshtastic_to_matrix",
        "fanout__ch1__matrix_to_meshtastic",
        "fanout__ch1__meshtastic_to_matrix",
    ]


# ===========================================================================
# 5. Per-entry label precedence on the allowed fan-in path
#
# When duplicate rooms are allowed (Meshtastic→Matrix fan-in), per-entry
# ``source_origin_label`` overrides the route-level label; an explicit
# empty string suppresses fallback; ``None`` falls back to route-level.
# ===========================================================================


def test_per_entry_label_overrides_route_level_on_fanin() -> None:
    """Per-entry source_origin_label wins over route-level on its leg."""
    crm = {
        "0": ChannelRoomMapEntry(
            room=_SHARED_ROOM, source_origin_label="Channel 0 Label"
        ),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),  # no entry label
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
        source_origin_label="Route Default",
    )
    routes = _build_routes(rc, _PLATFORMS_MESH_SOURCE)
    by_channel = {r.source.channel: r for r in routes}
    # Channel 0: explicit entry label wins.
    assert by_channel["0"].source.origin_label == "Channel 0 Label"
    # Channel 1: entry label is None → route-level fallback.
    assert by_channel["1"].source.origin_label == "Route Default"


def test_explicit_empty_entry_label_suppresses_route_level_fallback() -> None:
    """An explicit ``""`` entry label is preserved (suppresses fallback).

    Per the ``source_origin_label`` semantics, ``None`` means "fall
    back", while an explicit empty string means "suppress the fallback
    for this entry".  The fan-in path must honour the same precedence
    rules as the unique-room path.
    """
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM, source_origin_label=""),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),  # falls back
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
        source_origin_label="Route Default",
    )
    routes = _build_routes(rc, _PLATFORMS_MESH_SOURCE)
    by_channel = {r.source.channel: r for r in routes}
    # Channel 0: explicit "" preserved, not replaced by the route label.
    assert by_channel["0"].source.origin_label == ""
    # Channel 1: None → route-level label.
    assert by_channel["1"].source.origin_label == "Route Default"


def test_no_labels_no_route_label_fanin_legs_are_none() -> None:
    """With no entry labels and no route label, fan-in legs have ``None``."""
    crm = {
        "0": ChannelRoomMapEntry(room=_SHARED_ROOM),
        "1": ChannelRoomMapEntry(room=_SHARED_ROOM),
    }
    rc = _mesh_source_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        channel_room_map=crm,
    )
    routes = _build_routes(rc, _PLATFORMS_MESH_SOURCE)
    assert all(r.source.origin_label is None for r in routes)
