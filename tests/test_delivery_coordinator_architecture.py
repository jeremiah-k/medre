"""Architecture guards for the delivery-coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "src/medre/core/engine/pipeline/runner.py"
_COORDINATOR = _REPO_ROOT / "src/medre/core/engine/pipeline/delivery_coordinator.py"


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected one async def {name}, found {len(matches)}"
    return matches[0]


def _attribute_calls(node: ast.AST) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            calls.append((child.lineno, child.func.attr))
    return calls


def _direct_awaited_attribute_calls(statements: list[ast.stmt]) -> list[str]:
    """Return awaited attribute calls that are direct sibling statements."""
    calls: list[str] = []
    for statement in statements:
        value: ast.expr | None = None
        if isinstance(statement, ast.Expr):
            value = statement.value
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
        if not isinstance(value, ast.Await):
            continue
        awaited = value.value
        if isinstance(awaited, ast.Call) and isinstance(awaited.func, ast.Attribute):
            calls.append(awaited.func.attr)
    return calls


def test_runner_fanout_is_only_a_delivery_coordinator_boundary() -> None:
    """PipelineRunner keeps routing/ingress ownership, not per-target phases."""
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    fanout = _function(tree, "_deliver_to_targets_fan_out")
    calls = [
        child
        for child in ast.walk(fanout)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    ]
    assert len(calls) == 1
    call = calls[0]
    assert call.func.attr == "deliver_many"
    assert isinstance(call.func.value, ast.Attribute)
    assert isinstance(call.func.value.value, ast.Name)
    assert call.func.value.value.id == "self"
    assert call.func.value.attr == "_delivery_coordinator"


def test_runner_capacity_wiring_targets_delivery_coordinator_only() -> None:
    """Capacity wiring has one delivery authority after coordinator extraction."""
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    setters = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "set_capacity_controller"
    ]
    assert len(setters) == 1, "expected one set_capacity_controller method"
    setter = setters[0]
    assignments = [
        node
        for node in ast.walk(setter)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert assignments == []

    calls = [
        child
        for child in ast.walk(setter)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    ]
    assert len(calls) == 1
    call = calls[0]
    assert call.func.attr == "set_capacity_controller"
    assert isinstance(call.func.value, ast.Attribute)
    assert isinstance(call.func.value.value, ast.Name)
    assert call.func.value.value.id == "self"
    assert call.func.value.attr == "_delivery_coordinator"


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

    assert storage_calls == {"delivery_status", "list_receipts_for_delivery"}


def test_capacity_release_is_outermost_owned_delivery_cleanup() -> None:
    """Capacity release stays unconditional inside the ownership guard."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    owned_delivery = _function(tree, "_deliver_one_after_replay_gate")
    try_nodes = [node for node in owned_delivery.body if isinstance(node, ast.Try)]
    assert len(try_nodes) == 1
    owned = try_nodes[0]

    body_calls = _attribute_calls(ast.Module(body=owned.body, type_ignores=[]))
    assert "create_for_delivery" in {name for _, name in body_calls}

    assert len(owned.finalbody) == 1
    ownership_guard = owned.finalbody[0]
    assert isinstance(ownership_guard, ast.If)
    assert ownership_guard.orelse == []
    assert _direct_awaited_attribute_calls(ownership_guard.body) == ["release_delivery"]


def test_preflight_order_is_explicit_and_stable() -> None:
    """Suppression precedence remains visible in one coordinator method."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    preflight = _function(tree, "_preflight_outcome")
    loop = next(node for node in ast.walk(preflight) if isinstance(node, ast.For))
    assert isinstance(loop.iter, ast.Tuple)
    checks = [
        element.attr for element in loop.iter.elts if isinstance(element, ast.Attribute)
    ]
    assert checks == [
        "_replay_duplicate_outcome",
        "_route_trace_loop_outcome",
        "_self_loop_outcome",
        "_policy_outcome",
        "_capability_outcome",
        "_plan_skip_outcome",
    ]


def test_incomplete_identity_gate_precedes_lifecycle_entry() -> None:
    """Adapterless targets are suppressed before replay reads or outbox work."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    scoped = _function(tree, "_deliver_one_scoped")
    scoped_awaits = sorted(
        (
            (call.lineno, call.value.func.attr)
            for call in ast.walk(scoped)
            if isinstance(call, ast.Await)
            and isinstance(call.value, ast.Call)
            and isinstance(call.value.func, ast.Attribute)
        )
    )
    assert scoped_awaits[0][1] == "_incomplete_identity_outcome"

    admitted = _function(tree, "_deliver_one_after_replay_gate")
    admitted_awaits = sorted(
        (
            (call.lineno, call.value.func.attr)
            for call in ast.walk(admitted)
            if isinstance(call, ast.Await)
            and isinstance(call.value, ast.Call)
            and isinstance(call.value.func, ast.Attribute)
        )
    )
    assert [name for _, name in admitted_awaits[:2]] == [
        "_load_replay_authority",
        "_preflight_outcome",
    ]


def test_outbox_cleanup_is_inside_capacity_owned_boundary() -> None:
    """Lease cancellation and outbox finalization are unconditional siblings."""
    tree = ast.parse(_COORDINATOR.read_text(encoding="utf-8"))
    execute = _function(tree, "_execute_owned_delivery")
    execute_tries = [node for node in execute.body if isinstance(node, ast.Try)]
    assert len(execute_tries) == 1
    cleanup = execute_tries[0]
    assert _direct_awaited_attribute_calls(cleanup.finalbody) == [
        "cancel_renewal",
        "finalize_outcome",
    ]

    owned_delivery = _function(tree, "_deliver_one_after_replay_gate")
    owned = next(node for node in owned_delivery.body if isinstance(node, ast.Try))
    body_names = {
        name
        for _, name in _attribute_calls(ast.Module(body=owned.body, type_ignores=[]))
    }
    assert "_execute_owned_delivery" in body_names

    assert len(owned.finalbody) == 1
    ownership_guard = owned.finalbody[0]
    assert isinstance(ownership_guard, ast.If)
    assert _direct_awaited_attribute_calls(ownership_guard.body) == ["release_delivery"]
