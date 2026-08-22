"""Architecture guards for retry lifecycle ownership boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_RETRY = _REPO / "src" / "medre" / "runtime" / "retry.py"
_LIFECYCLE = (
    _REPO
    / "src"
    / "medre"
    / "core"
    / "engine"
    / "pipeline"
    / "delivery_lifecycle.py"
)

_ALLOWED_WORKER_STORAGE_CALLS = frozenset(
    {
        "claim_due_outbox_items",
        "count_outbox_by_status",
        "delivery_status",
        "get",
    }
)


def _source_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _self_storage_calls(tree: ast.AST) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "_storage"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            calls.add(node.func.attr)
    return calls


def _self_emit_literals(tree: ast.AST) -> set[str]:
    events: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_emit"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        events.add(node.args[0].value)
    return events


def test_retry_worker_direct_storage_surface_is_read_claim_only() -> None:
    calls = _self_storage_calls(_source_tree(_RETRY))
    mutations = {
        name
        for name in calls
        if name.startswith(("mark_", "append_", "create_", "finalize_", "release_"))
    }

    assert not mutations, f"worker calls storage mutations directly: {sorted(mutations)}"
    assert calls == _ALLOWED_WORKER_STORAGE_CALLS


def test_retry_worker_storage_protocol_matches_direct_surface() -> None:
    tree = _source_tree(_RETRY)
    protocol = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RetryWorkerStorage"
    )
    methods = {
        node.name
        for node in protocol.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert methods == _ALLOWED_WORKER_STORAGE_CALLS


def test_retry_failure_evidence_queries_live_in_lifecycle_authority() -> None:
    retry_source = _RETRY.read_text(encoding="utf-8")
    lifecycle_source = _LIFECYCLE.read_text(encoding="utf-8")

    assert "_check_dead_lettered" not in retry_source
    assert "self._storage.list_receipts_for_event" not in retry_source
    assert "self._storage.list_receipts_for_plan" not in retry_source
    assert "finalize_retry_attempt_error" in lifecycle_source
    assert "reconcile_retry_claim" in lifecycle_source
    assert "storage.list_receipts_for_plan" in lifecycle_source


def test_retry_worker_retains_operational_orchestration_authority() -> None:
    source = _RETRY.read_text(encoding="utf-8")
    events = _self_emit_literals(_source_tree(_RETRY))

    assert "self._capacity.acquire_delivery" in source
    assert "self._capacity.release_delivery" in source
    assert "self.state.failed" in source
    assert "self.state.dead_lettered" in source
    assert "retry_failed" in events
    assert "retry_dead_lettered" in events


def test_retry_claim_reconciliation_precedes_transport_dispatch() -> None:
    tree = _source_tree(_RETRY)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_retry_outbox_item"
    )
    reconcile_lines = [
        node.lineno
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reconcile_retry_claim"
    ]
    dispatch_lines = [
        node.lineno
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "deliver_to_target"
    ]

    assert reconcile_lines, "reconcile_retry_claim not called in _retry_outbox_item"
    assert dispatch_lines, "deliver_to_target not called in _retry_outbox_item"
    assert max(reconcile_lines) < min(dispatch_lines)


def test_retry_worker_uses_separate_storage_view_for_lifecycle_authority() -> None:
    tree = _source_tree(_RETRY)
    delegated = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (
            isinstance(owner, ast.Attribute)
            and owner.attr == "_lifecycle"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            continue
        delegated += 1
        assert node.args
        storage_arg = node.args[0]
        assert isinstance(storage_arg, ast.Attribute)
        assert storage_arg.attr == "_lifecycle_storage"
        assert isinstance(storage_arg.value, ast.Name)
        assert storage_arg.value.id == "self"

    assert delegated > 0


def test_retry_worker_never_calls_lifecycle_storage_directly() -> None:
    tree = _source_tree(_RETRY)
    direct_calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "_lifecycle_storage"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            direct_calls.add(node.func.attr)

    message = f"RetryWorker calls lifecycle storage directly: {sorted(direct_calls)}"
    assert not direct_calls, message


def test_retry_worker_constructor_requires_combined_storage_backend() -> None:
    tree = _source_tree(_RETRY)
    backend = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RetryWorkerBackend"
    )
    base_names = {base.id for base in backend.bases if isinstance(base, ast.Name)}
    assert base_names == {"RetryWorkerStorage", "DeliveryLifecycleStorage", "Protocol"}

    worker = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RetryWorker"
    )
    init = next(
        node
        for node in worker.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    storage_arg = next(arg for arg in init.args.args if arg.arg == "storage")
    assert isinstance(storage_arg.annotation, ast.Name)
    assert storage_arg.annotation.id == "RetryWorkerBackend"
