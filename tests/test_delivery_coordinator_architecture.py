"""Architecture guards for the delivery-coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path


_RUNNER = Path("src/medre/core/engine/pipeline/runner.py")
_COORDINATOR = Path("src/medre/core/engine/pipeline/delivery_coordinator.py")


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _attribute_calls(node: ast.AST) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            calls.append((child.lineno, child.func.attr))
    return calls


def test_runner_fanout_is_only_a_delivery_coordinator_boundary() -> None:
    """PipelineRunner keeps routing/ingress ownership, not per-target phases."""
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    fanout = _function(tree, "_deliver_to_targets_fan_out")
    attrs = [name for _, name in _attribute_calls(fanout)]

    assert attrs == ["deliver_many"]


def test_delivery_coordinator_does_not_write_storage_state_directly() -> None:
    """Persistence transitions remain delegated to lifecycle/outbox authorities."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    storage_calls: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "_storage"
        ):
            storage_calls.add(node.func.attr)

    assert storage_calls == {"list_receipts_for_event"}


def test_capacity_release_is_outermost_owned_delivery_cleanup() -> None:
    """Capacity release stays outside outbox creation/finalization failures."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    scoped = _function(tree, "_deliver_one_scoped")
    try_nodes = [node for node in scoped.body if isinstance(node, ast.Try)]
    assert len(try_nodes) == 1
    owned = try_nodes[0]

    body_calls = _attribute_calls(ast.Module(body=owned.body, type_ignores=[]))
    final_calls = _attribute_calls(ast.Module(body=owned.finalbody, type_ignores=[]))
    assert "create_for_delivery" in {name for _, name in body_calls}
    assert "release_delivery" in {name for _, name in final_calls}



def test_preflight_order_is_explicit_and_stable() -> None:
    """Suppression precedence remains visible in one coordinator method."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    preflight = _function(tree, "_preflight_outcome")
    loop = next(node for node in ast.walk(preflight) if isinstance(node, ast.For))
    assert isinstance(loop.iter, ast.Tuple)
    checks = [
        element.attr
        for element in loop.iter.elts
        if isinstance(element, ast.Attribute)
    ]
    assert checks == [
        "_replay_duplicate_outcome",
        "_route_trace_loop_outcome",
        "_self_loop_outcome",
        "_policy_outcome",
        "_capability_outcome",
        "_plan_skip_outcome",
    ]

def test_outbox_cleanup_is_inside_capacity_owned_boundary() -> None:
    """Outbox cleanup runs before the outer capacity-release finally."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    execute = _function(tree, "_execute_owned_delivery")
    calls = _attribute_calls(execute)
    cancel_line = next(line for line, name in calls if name == "cancel_renewal")
    finalize_line = next(line for line, name in calls if name == "finalize_outcome")
    assert cancel_line < finalize_line

    scoped = _function(tree, "_deliver_one_scoped")
    owned = next(node for node in scoped.body if isinstance(node, ast.Try))
    body_names = {name for _, name in _attribute_calls(ast.Module(body=owned.body, type_ignores=[]))}
    final_names = {name for _, name in _attribute_calls(ast.Module(body=owned.finalbody, type_ignores=[]))}
    assert "_execute_owned_delivery" in body_names
    assert "release_delivery" in final_names
