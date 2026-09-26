"""YAML parsing and validation for structured ``context_map`` entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.routes import ContextMapEntry, RouteConfigSet


def _routes_from_yaml(yaml_text: str) -> RouteConfigSet:
    data = parse_yaml_config(yaml_text, source="<test>")
    return RouteConfigSet.from_dict(data)


def _map_entry(rcs: RouteConfigSet, route_id: str, context: str) -> ContextMapEntry:
    route = next(route for route in rcs.routes if route.route_id == route_id)
    assert route.context_map is not None
    return route.context_map[context]


def _yaml_with_context_map(context_map_yaml_block: str) -> str:
    return (
        "runtime:\n"
        "  name: context_map_yaml_test\n"
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [radio_adapter]\n"
        "    dest_adapters: [chat_adapter]\n"
        "    directionality: bidirectional\n"
        "    context_map:\n" + context_map_yaml_block
    )


def test_structured_entry_loads_through_yaml_parser() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        '        source_origin_label: "Ops Channel"\n'
        '        dest_origin_label: "Radio 0"\n'
    )
    entry = _map_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry == ContextMapEntry(
        dest_context="!room0:example.com",
        source_origin_label="Ops Channel",
        dest_origin_label="Radio 0",
    )


def test_structured_entry_with_only_dest_context_has_no_labels() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n' '        dest_context: "!room0:example.com"\n'
    )
    entry = _map_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry == ContextMapEntry(dest_context="!room0:example.com")


def test_quoted_context_loads_verbatim() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n' '        dest_context: "!roomA:example.org"\n'
    )
    entry = _map_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.dest_context == "!roomA:example.org"


def test_explicit_empty_source_label_is_preserved() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        '        source_origin_label: ""\n'
    )
    entry = _map_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.source_origin_label == ""


def test_explicit_null_source_label_is_none() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        "        source_origin_label: null\n"
    )
    entry = _map_entry(_routes_from_yaml(yaml_text), "bridge", "0")
    assert entry.source_origin_label is None


def test_integer_context_key_is_normalized() -> None:
    yaml_text = _yaml_with_context_map(
        "      0:\n"
        '        dest_context: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
    )
    route = _routes_from_yaml(yaml_text).routes[0]
    assert route.context_map is not None
    assert set(route.context_map) == {"0"}
    assert route.context_map["0"].source_origin_label == "Ops"


def test_mixed_integer_and_string_context_keys_are_normalized() -> None:
    yaml_text = _yaml_with_context_map(
        "      0:\n"
        '        dest_context: "!room0:example.com"\n'
        '      "1":\n'
        '        dest_context: "!room1:example.com"\n'
    )
    route = _routes_from_yaml(yaml_text).routes[0]
    assert route.context_map is not None
    assert set(route.context_map) == {"0", "1"}


def test_bare_context_string_is_rejected() -> None:
    yaml_text = _yaml_with_context_map('      "0": "!room0:example.com"\n')
    with pytest.raises(ConfigValidationError, match="must be a table"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_unknown_key_is_rejected() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        '        bogus_key: "bad"\n'
    )
    with pytest.raises(ConfigValidationError, match="unknown key"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_without_dest_side_is_rejected() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n' '        source_origin_label: "No Dest"\n'
    )
    with pytest.raises(ConfigValidationError, match="exactly one of"):
        _routes_from_yaml(yaml_text)


def test_structured_entry_with_both_dest_sides_is_rejected() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        "        dest_destination:\n"
        "          kind: lxmf_destination\n"
        '          destination_hash: "21c0c1b9aabbccddeeff001122334455"\n'
    )
    with pytest.raises(ConfigValidationError, match="exactly one of"):
        _routes_from_yaml(yaml_text)


def test_unstripped_context_key_is_rejected() -> None:
    yaml_text = _yaml_with_context_map(
        '      " 0":\n' '        dest_context: "!room0:example.com"\n'
    )
    with pytest.raises(ConfigValidationError, match="stripped/normalized"):
        _routes_from_yaml(yaml_text)


def test_unstripped_dest_context_value_is_rejected() -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n' '        dest_context: "  !room0:example.com"\n'
    )
    with pytest.raises(ConfigValidationError, match="stripped/normalized"):
        _routes_from_yaml(yaml_text)


def test_non_string_dest_context_is_rejected() -> None:
    yaml_text = _yaml_with_context_map('      "0":\n' "        dest_context: 42\n")
    with pytest.raises(ConfigValidationError, match="must be a string"):
        _routes_from_yaml(yaml_text)


def test_structured_destination_entry_loads_through_yaml_parser() -> None:
    yaml_text = (
        "runtime:\n"
        "  name: context_map_yaml_test\n"
        "routes:\n"
        "  lxmf_out:\n"
        "    source_adapters: [chat_adapter]\n"
        "    dest_adapters: [lxmf_adapter]\n"
        "    directionality: source_to_dest\n"
        "    context_map:\n"
        '      "!room0:example.com":\n'
        "        dest_destination:\n"
        "          kind: lxmf_destination\n"
        '          destination_hash: "21c0c1b9aabbccddeeff001122334455"\n'
        "          destination_name: bob\n"
    )
    route = _routes_from_yaml(yaml_text).routes[0]
    assert route.context_map is not None
    entry = route.context_map["!room0:example.com"]
    assert entry.dest_context is None
    assert entry.dest_destination is not None
    assert entry.dest_destination.kind == "lxmf_destination"
    assert entry.dest_destination.destination_hash == (
        "21c0c1b9aabbccddeeff001122334455"
    )
    assert entry.dest_destination.destination_name == "bob"


@pytest.mark.parametrize("field", ["source_origin_label", "dest_origin_label"])
@pytest.mark.parametrize("value", ["true", "42"])
def test_structured_entry_nonstring_label_is_rejected(field: str, value: str) -> None:
    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        f"        {field}: {value}\n"
    )
    with pytest.raises(ConfigValidationError, match="must be a string"):
        _routes_from_yaml(yaml_text)


def test_load_config_full_path_uses_structured_entries(tmp_path: Path) -> None:
    from medre.config.loader import load_config

    yaml_text = _yaml_with_context_map(
        '      "0":\n'
        '        dest_context: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
        '        dest_origin_label: "Radio"\n'
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    config, _, _ = load_config(str(config_path))
    assert config.routes.routes[0].context_map == {
        "0": ContextMapEntry(
            dest_context="!room0:example.com",
            source_origin_label="Ops",
            dest_origin_label="Radio",
        )
    }
