"""Programmatic route construction parity and expansion guards."""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.routes import (
    ChannelRoomMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDestinationConfig,
    RouteDirectionality,
)
from medre.runtime.route_engine import RouteValidationError, build_runtime_routes


def test_string_bidirectional_expands_both_directions() -> None:
    rc = RouteConfig(
        route_id="mx_bridge",
        source_adapters=("main",),
        dest_adapters=("radio",),
        directionality="bidirectional",  # type: ignore[arg-type]
    )
    assert rc.directionality is RouteDirectionality.BIDIRECTIONAL
    routes = build_runtime_routes(
        RouteConfigSet(routes=(rc,)),
        {"main": "matrix", "radio": "meshtastic"},
    )
    assert len(routes) == 2
    by_id = {route.id: route for route in routes}
    assert set(by_id) == {"mx_bridge", "mx_bridge__rev_0"}
    assert by_id["mx_bridge"].source.adapter == "main"
    assert {target.adapter for target in by_id["mx_bridge"].targets} == {"radio"}
    assert by_id["mx_bridge__rev_0"].source.adapter == "radio"
    assert {target.adapter for target in by_id["mx_bridge__rev_0"].targets} == {"main"}


def test_unknown_directionality_raises_loudly() -> None:
    rc = RouteConfig(
        route_id="tampered",
        source_adapters=("main",),
        dest_adapters=("radio",),
    )
    object.__setattr__(rc, "directionality", "sideways")
    with pytest.raises(RouteValidationError, match="unrecognized directionality"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix", "radio": "meshtastic"},
        )


def test_channel_room_map_unknown_directionality_raises_loudly() -> None:
    rc = RouteConfig(
        route_id="tampered_map",
        source_adapters=("main",),
        dest_adapters=("radio",),
        channel_room_map={"0": ChannelRoomMapEntry(room="!room:example.com")},
    )
    object.__setattr__(rc, "directionality", "sideways")
    with pytest.raises(RouteValidationError, match="unrecognized directionality"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix", "radio": "meshtastic"},
        )


def test_construction_coerces_plain_directionality_strings() -> None:
    for raw, expected in (
        ("source_to_dest", RouteDirectionality.SOURCE_TO_DEST),
        ("dest_to_source", RouteDirectionality.DEST_TO_SOURCE),
        ("bidirectional", RouteDirectionality.BIDIRECTIONAL),
    ):
        rc = RouteConfig(
            route_id="coerce_dir",
            source_adapters=("main",),
            dest_adapters=("radio",),
            directionality=raw,  # type: ignore[arg-type]
        )
        assert rc.directionality is expected


def test_construction_accepts_directionality_enum_idempotently() -> None:
    rc = RouteConfig(
        route_id="enum_dir",
        source_adapters=("main",),
        dest_adapters=("radio",),
        directionality=RouteDirectionality.BIDIRECTIONAL,
    )
    assert rc.directionality is RouteDirectionality.BIDIRECTIONAL


def test_construction_invalid_directionality_has_route_context() -> None:
    with pytest.raises(ConfigValidationError, match="invalid directionality"):
        RouteConfig(
            route_id="bad_dir",
            source_adapters=("main",),
            dest_adapters=("radio",),
            directionality="sideways",  # type: ignore[arg-type]
        )


def test_construction_unhashable_directionality_is_config_error() -> None:
    with pytest.raises(ConfigValidationError, match="invalid directionality"):
        RouteConfig(
            route_id="bad_dir_type",
            source_adapters=("main",),
            dest_adapters=("radio",),
            directionality=["sideways"],  # type: ignore[arg-type]
        )


def test_construction_aliases_rooms_to_channels() -> None:
    rc = RouteConfig(
        route_id="alias_dir",
        source_adapters=("main",),
        dest_adapters=("radio",),
        source_room="!room:example.com",
        dest_channel="0",
    )
    assert rc.source_channel == "!room:example.com"
    assert rc.dest_channel == "0"


def test_construction_conflicting_room_and_channel_raises() -> None:
    with pytest.raises(ConfigValidationError, match="source_room"):
        RouteConfig(
            route_id="alias_conflict",
            source_adapters=("main",),
            dest_adapters=("radio",),
            source_room="!room-a:example.com",
            source_channel="!room-b:example.com",
        )


def test_expansion_carries_room_channel_to_reverse_target() -> None:
    rc = RouteConfig(
        route_id="mx_bridge",
        source_adapters=("main",),
        dest_adapters=("radio",),
        source_room="!room:example.com",
        dest_channel="0",
        directionality="bidirectional",  # type: ignore[arg-type]
    )
    routes = build_runtime_routes(
        RouteConfigSet(routes=(rc,)),
        {"main": "matrix", "radio": "meshtastic"},
    )
    reverse = {route.id: route for route in routes}["mx_bridge__rev_0"]
    assert reverse.source.adapter == "radio"
    assert reverse.source.channel == "0"
    assert {target.adapter: target.channel for target in reverse.targets} == {
        "main": "!room:example.com"
    }


def test_standard_expansion_rejects_empty_source_adapters() -> None:
    rc = RouteConfig(
        route_id="empty_source",
        source_adapters=(),
        dest_adapters=("radio",),
    )
    with pytest.raises(RouteValidationError, match="source_adapters must not be empty"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"radio": "meshtastic"},
        )


def test_standard_expansion_rejects_empty_dest_adapters() -> None:
    rc = RouteConfig(
        route_id="empty_dest",
        source_adapters=("main",),
        dest_adapters=(),
    )
    with pytest.raises(RouteValidationError, match="dest_adapters must not be empty"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix"},
        )


def test_programmatic_structured_destination_requires_single_dest_adapter() -> None:
    rc = RouteConfig(
        route_id="structured_fanout",
        source_adapters=("main",),
        dest_adapters=("lx_a", "lx_b"),
        dest_destination=RouteDestinationConfig(
            kind="lxmf_destination",
            destination_hash="ab" * 16,
        ),
    )
    with pytest.raises(RouteValidationError, match="requires exactly one dest adapter"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix", "lx_a": "lxmf", "lx_b": "lxmf"},
        )


def test_programmatic_structured_destination_rejects_channel_selector() -> None:
    rc = RouteConfig(
        route_id="structured_conflict",
        source_adapters=("main",),
        dest_adapters=("lx_a",),
        dest_channel="legacy-destination",
        dest_destination=RouteDestinationConfig(
            kind="lxmf_destination",
            destination_hash="ab" * 16,
        ),
    )
    with pytest.raises(RouteValidationError, match="mutually exclusive"):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix", "lx_a": "lxmf"},
        )


def test_programmatic_channel_room_map_rejects_selector_conflict() -> None:
    rc = RouteConfig(
        route_id="map_conflict",
        source_adapters=("main",),
        dest_adapters=("radio",),
        source_channel="!room:example.com",
        channel_room_map={"0": ChannelRoomMapEntry(room="!room:example.com")},
    )
    with pytest.raises(
        RouteValidationError, match="channel_room_map is mutually exclusive"
    ):
        build_runtime_routes(
            RouteConfigSet(routes=(rc,)),
            {"main": "matrix", "radio": "meshtastic"},
        )
