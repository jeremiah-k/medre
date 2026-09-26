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

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_NATIVE_METADATA_VERSION_SOURCES: dict[str, tuple[str, str]] = {
    "matrix": (
        "src/medre/adapters/matrix/event_shape.py",
        "MATRIX_NATIVE_SCHEMA_VERSION",
    ),
    "meshtastic": (
        "src/medre/adapters/meshtastic/event_shape.py",
        "MESHTASTIC_NATIVE_SCHEMA_VERSION",
    ),
    "meshcore": (
        "src/medre/adapters/meshcore/event_shape.py",
        "MESHCORE_NATIVE_SCHEMA_VERSION",
    ),
    "lxmf": (
        "src/medre/adapters/lxmf/event_shape.py",
        "LXMF_NATIVE_SCHEMA_VERSION",
    ),
}


def _parse(relative: str) -> ast.Module:
    path = _ROOT / relative
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


def _integer_constant(relative: str, name: str) -> int:
    tree = _parse(relative)
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
        ):
            return value.value
    raise AssertionError(f"{relative}: missing integer constant {name}")


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

    renderer = _class(_parse("src/medre/adapters/lxmf/renderer.py"), "LxmfRenderer")
    init = _method(renderer, "__init__")
    arg_names = {
        arg.arg
        for arg in (*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs)
    }
    assert (
        "relay_prefix" not in arg_names
    ), "LXMF relay prefixes are resolved from target adapter config at render time"


@pytest.mark.parametrize(
    ("transport", "accessor"),
    (
        ("meshtastic", "meshtastic_namespace"),
        ("meshcore", "meshcore_namespace"),
        ("lxmf", "lxmf_namespace"),
    ),
)
def test_transport_attribution_reads_versioned_namespaces_only(
    transport: str, accessor: str
) -> None:
    """Attribution readers use strict version-aware namespace accessors."""
    tree = _parse(f"src/medre/adapters/{transport}/attribution.py")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert any(
        isinstance(call.func, ast.Name) and call.func.id == accessor for call in calls
    )
    assert not any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "native_data"
        and call.func.attr == "get"
        for call in calls
    ), f"{transport} attribution must not bypass {accessor} with native_data.get(...)"
    assert not any(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "native_data"
        for node in ast.walk(tree)
    ), f"{transport} attribution must not bypass {accessor} with native_data[...]"


@pytest.mark.parametrize("transport", sorted(_NATIVE_METADATA_VERSION_SOURCES))
def test_native_metadata_source_schema_and_example_versions_match(
    transport: str,
) -> None:
    """Source, JSON Schema, and example share one native-metadata version."""
    source_rel, constant = _NATIVE_METADATA_VERSION_SOURCES[transport]
    source_version = _integer_constant(source_rel, constant)

    schema_path = (
        _ROOT / "docs" / "schemas" / f"{transport}-native-metadata.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    versioned_defs = [
        value
        for value in schema.get("$defs", {}).values()
        if isinstance(value, dict)
        and isinstance(value.get("properties"), dict)
        and "schema_version" in value["properties"]
    ]
    assert (
        len(versioned_defs) == 1
    ), f"{schema_path.name}: expected exactly one versioned definition"
    version_property = versioned_defs[0]["properties"]["schema_version"]
    schema_version = version_property.get("const")
    assert isinstance(schema_version, int) and not isinstance(schema_version, bool)

    example_path = (
        _ROOT
        / "docs"
        / "schemas"
        / "examples"
        / f"{transport}-native-metadata-example.json"
    )
    example = json.loads(example_path.read_text(encoding="utf-8"))
    native = example.get(transport)
    assert isinstance(
        native, dict
    ), f"{example_path.name}: missing {transport!r} namespace"
    example_version = native.get("schema_version")
    assert isinstance(example_version, int) and not isinstance(example_version, bool)

    assert source_version == schema_version == example_version, (
        f"{transport}: native-metadata schema-version drift "
        f"source={source_version} schema={schema_version} example={example_version}"
    )


def test_routing_schema_keeps_context_map_structured_only() -> None:
    """Machine config schema must not re-admit bare-string context mappings."""
    schema_path = _ROOT / "docs" / "schemas" / "routing-config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    entry = schema.get("$defs", {}).get("ContextMapEntry")
    assert isinstance(entry, dict)
    assert entry.get("type") == "object"
    assert entry.get("additionalProperties") is False
    assert {"dest_context", "dest_destination"} <= set(entry.get("properties", {}))

    occurrences: list[dict[str, Any]] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            context_map = value.get("context_map")
            if isinstance(context_map, dict):
                occurrences.append(context_map)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    assert occurrences, "routing schema contains no context_map contract"

    expected_ref = "#/$defs/ContextMapEntry"
    for occurrence in occurrences:
        # Conditional schema branches may narrow context_map to null when
        # another addressing authority is active.  That is not a second
        # mapping representation and must not be mistaken for one.
        if occurrence == {"type": "null"}:
            continue

        variants = occurrence.get("oneOf")
        assert isinstance(variants, list), (
            "context_map must be either the structured mapping contract "
            "or a conditional null-only narrowing"
        )
        object_variants = [
            variant
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") == "object"
        ]
        assert len(object_variants) == 1
        assert object_variants[0].get("additionalProperties") == {"$ref": expected_ref}
        assert not any(
            isinstance(variant, dict) and variant.get("type") == "string"
            for variant in variants
        ), "bare-string context_map compatibility unexpectedly returned"
