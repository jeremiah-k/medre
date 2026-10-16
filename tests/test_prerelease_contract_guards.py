"""Static guards for prerelease shape and documentation contracts.

These checks preserve the mechanical anti-regression value that used to live
inside the generated current-state inventory.  The generated inventory and
historical audit snapshots are intentionally gone; the durable guards belong
next to the contracts they protect instead of depending on generated prose.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from tests.helpers.source_locations import source_file

_ROOT = Path(__file__).resolve().parents[1]


def _parse(relative: str) -> ast.Module:
    path = source_file(_ROOT, relative)
    return ast.parse(path.read_text(encoding="utf-8"), filename=relative)


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one class {name}, found {len(matches)}"
    return matches[0]


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert (
        len(matches) == 1
    ), f"expected exactly one method {cls.name}.{name}, found {len(matches)}"
    return matches[0]


def test_canonical_event_shape_conversion_registry_does_not_return() -> None:
    """Prerelease canonical events have one current shape, not migrations."""
    tree = _parse("src/medre/core/events/schema.py")
    assert not any(
        isinstance(node, ast.Name) and node.id == "MIGRATION_REGISTRY"
        for node in ast.walk(tree)
    ), "canonical event shape-conversion registry unexpectedly present"


def test_abandoned_development_shape_entry_points_do_not_return() -> None:
    """Removed prerelease entry points stay removed after audit-doc pruning."""
    replay = _parse("src/medre/core/engine/replay/rendering.py")
    stage = _method(_class(replay, "_ReplayRenderingMixin"), "_stage_render")
    explicit_methods = {
        call.args[0].value
        for call in ast.walk(stage)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_explicit_pipeline_method"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert "render_event" not in explicit_methods
    assert "render_replay_event" in explicit_methods

    runner = _class(
        _parse("src/medre/core/engine/pipeline/runner.py"), "PipelineRunner"
    )
    runner_methods = {
        node.name
        for node in runner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "ingress_handler" not in runner_methods

    smoke = _parse("src/medre/cli/smoke_commands.py")
    abandoned_fallbacks = [
        call
        for call in ast.walk(smoke)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "commands"
        and call.func.attr == "get"
        and len(call.args) >= 2
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "commands_text"
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "commands"
    ]
    assert (
        not abandoned_fallbacks
    ), "smoke command rendering must not accept the abandoned commands mapping shape"


def test_routing_schema_keeps_context_map_structured_only() -> None:
    """Machine config schema must not re-admit bare-string context mappings."""
    schema_path = _ROOT / "docs" / "schemas" / "routing-config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    base_route = {
        "route_id": "mapped",
        "source_adapters": ["source"],
        "dest_adapters": ["dest"],
    }
    for context_map, valid in (
        ({"ctx": {"dest_context": "room"}}, True),
        ({"ctx": "room"}, False),
        ({"ctx": {"dest_context": "room", "unknown": True}}, False),
        ("room", False),
        (["room"], False),
        (None, False),
    ):
        route = {**base_route, "context_map": context_map}
        # Cover both public route forms without depending on how the schema
        # expresses their constraints (refs, conditionals, or inline objects).
        assert validator.is_valid(route) is valid
        assert validator.is_valid({"routes": [route]}) is valid


def test_routing_schema_enforces_context_map_route_boundaries() -> None:
    """Machine schema must mirror context-map cardinality/addressing rules."""
    from jsonschema import Draft202012Validator

    schema_path = _ROOT / "docs" / "schemas" / "routing-config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    base_route: dict[str, Any] = {
        "route_id": "mapped",
        "source_adapters": ["source"],
        "dest_adapters": ["dest"],
        "context_map": {"ctx": {"dest_context": "room"}},
    }

    # Exercise both the top-level RouteConfig arm and RouteConfigRef through
    # RouteConfigSet so their conditional constraints cannot drift apart.
    def errors(route: dict[str, Any]) -> tuple[list[Any], list[Any]]:
        return (
            list(validator.iter_errors(route)),
            list(validator.iter_errors({"routes": [route]})),
        )

    assert errors(base_route) == ([], [])

    invalid_variants = (
        {**base_route, "source_adapters": ["source", "other"]},
        {**base_route, "dest_adapters": ["dest", "other"]},
        {**base_route, "source_channel": "legacy-source"},
        {**base_route, "dest_channel": "legacy-dest"},
        {**base_route, "source_room": "!source:example.org"},
        {**base_route, "dest_room": "!dest:example.org"},
        {
            **base_route,
            "dest_destination": {
                "kind": "lxmf_destination",
                "destination_hash": "21c0c1b9aabbccddeeff001122334455",
            },
        },
    )
    for route in invalid_variants:
        standalone_errors, set_errors = errors(route)
        assert standalone_errors, route
        assert set_errors, route
