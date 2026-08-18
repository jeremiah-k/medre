"""Source-level helpers for pytest marker and ``addopts`` boundaries."""

from __future__ import annotations

import ast
import shlex
import tomllib
from itertools import product
from pathlib import Path


def _pytest_marker_name(node: ast.AST) -> str | None:
    """Return the marker name for a direct ``pytest.mark`` expression."""
    if isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return None
    mark = node.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return None
    root = mark.value
    if not isinstance(root, ast.Name) or root.id != "pytest":
        return None
    return node.attr


def _markers_in_expression(
    node: ast.AST,
    assignments: dict[str, ast.AST],
    seen_names: frozenset[str] = frozenset(),
) -> set[str]:
    markers = {
        marker
        for child in ast.walk(node)
        if (marker := _pytest_marker_name(child)) is not None
    }
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Name)
            and child.id in assignments
            and child.id not in seen_names
        ):
            markers.update(
                _markers_in_expression(
                    assignments[child.id],
                    assignments,
                    seen_names | {child.id},
                )
            )
    return markers


def _assigned_value(statement: ast.stmt, target_name: str) -> ast.AST | None:
    if isinstance(statement, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == target_name
        for target in statement.targets
    ):
        return statement.value
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == target_name
    ):
        return statement.value
    return None


def _pytestmark_values(statements: list[ast.stmt]) -> list[ast.AST]:
    values: list[ast.AST] = []
    for statement in statements:
        value = _assigned_value(statement, "pytestmark")
        if value is not None:
            values.append(value)
        if isinstance(statement, ast.ClassDef):
            values.extend(_pytestmark_values(statement.body))
    return values


def declared_pytest_markers(path: Path) -> frozenset[str]:
    """Return markers declared by module ``pytestmark`` or decorators.

    Parsing the module prevents marker-looking comments, docstrings, and string
    constants from satisfying the opt-in boundary checks.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    markers: set[str] = set()
    assignments = {
        target.id: statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assignments.update(
        {
            statement.target.id: statement.value
            for statement in tree.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        }
    )

    for value in _pytestmark_values(tree.body):
        markers.update(_markers_in_expression(value, assignments))

    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                marker = _pytest_marker_name(decorator)
                if marker is not None:
                    markers.add(marker)

    return frozenset(markers)


def pytest_addopts_marker_expression(path: Path) -> str:
    """Return the marker expression supplied by pytest's parsed ``addopts``."""
    with path.open("rb") as pyproject:
        config = tomllib.load(pyproject)
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    if not isinstance(addopts, str):
        raise TypeError("pytest addopts must be a string")

    args = shlex.split(addopts)
    expressions: list[str] = []
    for index, argument in enumerate(args):
        if argument == "-m":
            if index + 1 >= len(args):
                raise ValueError("pytest addopts has '-m' without an expression")
            expressions.append(args[index + 1])
        elif argument.startswith("-m="):
            expressions.append(argument[3:])
    if len(expressions) != 1:
        raise ValueError("pytest addopts must contain exactly one '-m' expression")
    return expressions[0]


def marker_is_explicitly_excluded(expression: str, marker: str) -> bool:
    """Return whether *expression* rejects every selection of *marker*."""
    tree = ast.parse(expression, mode="eval")
    names = sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})
    if marker not in names:
        return False
    other_names = [name for name in names if name != marker]
    for enabled in product((False, True), repeat=len(other_names)):
        values = dict(zip(other_names, enabled, strict=True))
        values[marker] = True
        if _evaluate_marker_expression(tree.body, values):
            return False
    return True


def _evaluate_marker_expression(node: ast.AST, values: dict[str, bool]) -> bool:
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate_marker_expression(node.operand, values)
    if isinstance(node, ast.BoolOp):
        evaluated = (
            _evaluate_marker_expression(value, values) for value in node.values
        )
        if isinstance(node.op, ast.And):
            return all(evaluated)
        if isinstance(node.op, ast.Or):
            return any(evaluated)
    raise ValueError(f"unsupported pytest marker expression: {ast.dump(node)}")
