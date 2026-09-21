"""Static hygiene checks for the test suite itself."""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent
_TEST_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _iter_collectable_test_functions(tree: ast.Module):
    """Yield module tests and test-class methods that pytest can collect."""
    for node in tree.body:
        if isinstance(node, _TEST_NODE_TYPES) and node.name.startswith("test_"):
            yield node
            continue
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
            continue
        for member in node.body:
            if isinstance(member, _TEST_NODE_TYPES) and member.name.startswith("test_"):
                yield member


def _effective_test_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """Return a test body without its optional leading docstring."""
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    return body


def _is_placeholder_statement(statement: ast.stmt) -> bool:
    """Return True for statements that provide no executable test behavior."""
    if isinstance(statement, ast.Pass):
        return True
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def test_test_functions_are_not_empty_placeholders() -> None:
    """Every collected-style test function must contain executable behavior."""
    violations: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in _iter_collectable_test_functions(tree):
            body = _effective_test_body(node)
            if not body or all(_is_placeholder_statement(stmt) for stmt in body):
                relative = path.relative_to(_TESTS_ROOT)
                violations.append(f"{relative}:{node.lineno}:{node.name}")

    assert violations == [], "empty placeholder tests:\n" + "\n".join(violations)
