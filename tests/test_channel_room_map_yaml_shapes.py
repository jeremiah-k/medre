"""YAML parsing and validation for structured ``channel_room_map`` entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.routes import ChannelRoomMapEntry, RouteConfigSet


def _routes_from_yaml(yaml_text: str) -> RouteConfigSet:
    data = parse_yaml_config(yaml_text, source="<test>")
    return RouteConfigSet.from_dict(data)


def _crm_entry(rcs: RouteConfigSet, route_id: str, channel: str) -> ChannelRoomMapEntry:
    route = next(route for route in rcs.routes if route.route_id == route_id)
    assert route.channel_room_map is not None
    return route.channel_room_map[channel]


def _yaml_with_crm(crm_yaml_block: str) -> str:
    return (
        "runtime:\n"
        "  name: crm_yaml_test\n"
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [matrix_adapter]\n"
        "    dest_adapters: [mesh_adapter]\n"
        "    directionality: bidirectional\n"
        "    channel_room_map:\n" + crm_yaml_block
    )


def test_structured_entry_loads_through_yaml_parser() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops Channel"\n'
        '        dest_origin_label: "Radio 0"\n'
    )
    entry = _crm_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry == ChannelRoomMapEntry(
        room="!room0:example.com",
        source_origin_label="Ops Channel",
        dest_origin_label="Radio 0",
    )


def test_structured_entry_with_only_room_has_no_labels() -> None:
    yaml_text = _yaml_with_crm('      "0":\n' '        room: "!room0:example.com"\n')
    entry = _crm_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry == ChannelRoomMapEntry(room="!room0:example.com")


def test_quoted_matrix_room_id_loads_verbatim() -> None:
    yaml_text = _yaml_with_crm('      "0":\n' '        room: "!roomA:example.org"\n')
    entry = _crm_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.room == "!roomA:example.org"


def test_explicit_empty_source_label_is_preserved() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: ""\n'
    )
    entry = _crm_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.source_origin_label == ""


def test_explicit_null_source_label_is_none() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        source_origin_label: null\n"
    )
    entry = _crm_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.source_origin_label is None


def test_integer_channel_key_is_normalized() -> None:
    yaml_text = _yaml_with_crm(
        "      0:\n"
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
    )
    route = _routes_from_yaml(yaml_text).routes[0]
    assert route.channel_room_map is not None
    assert set(route.channel_room_map) == {"0"}
    assert route.channel_room_map["0"].source_origin_label == "Ops"


def test_mixed_integer_and_string_channel_keys_are_normalized() -> None:
    yaml_text = _yaml_with_crm(
        "      0:\n"
        '        room: "!room0:example.com"\n'
        '      "1":\n'
        '        room: "!room1:example.com"\n'
    )
    route = _routes_from_yaml(yaml_text).routes[0]
    assert route.channel_room_map is not None
    assert set(route.channel_room_map) == {"0", "1"}


def test_bare_room_string_is_rejected() -> None:
    yaml_text = _yaml_with_crm('      "0": "!room0:example.com"\n')
    with pytest.raises(ConfigValidationError, match="must be a table"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_unknown_key_is_rejected() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        bogus_key: "bad"\n'
    )
    with pytest.raises(ConfigValidationError, match="unknown key"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_missing_room_is_rejected() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n' '        source_origin_label: "No Room"\n'
    )
    with pytest.raises(ConfigValidationError, match="missing required 'room'"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_alias_room_is_rejected() -> None:
    yaml_text = _yaml_with_crm('      "0":\n' '        room: "#room:example.com"\n')
    with pytest.raises(ConfigValidationError, match="room alias"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_noncanonical_room_is_rejected() -> None:
    yaml_text = _yaml_with_crm('      "0":\n' '        room: "not_a_room"\n')
    with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
        _routes_from_yaml(yaml_text)


@pytest.mark.parametrize("field", ["source_origin_label", "dest_origin_label"])
@pytest.mark.parametrize("value", ["true", "42"])
def test_structured_entry_nonstring_label_is_rejected(field: str, value: str) -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        f"        {field}: {value}\n"
    )
    with pytest.raises(ConfigValidationError, match="must be a string"):
        _routes_from_yaml(yaml_text)


def test_load_config_full_path_uses_structured_entries(tmp_path: Path) -> None:
    from medre.config.loader import load_config

    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
        '        dest_origin_label: "Radio"\n'
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    config, _, _ = load_config(str(config_path))
    assert config.routes.routes[0].channel_room_map == {
        "0": ChannelRoomMapEntry(
            room="!room0:example.com",
            source_origin_label="Ops",
            dest_origin_label="Radio",
        )
    }
