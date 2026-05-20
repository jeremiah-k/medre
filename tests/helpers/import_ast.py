"""Shared AST helpers for import-boundary testing.

Extracted from tests/test_architectural_boundaries.py to keep that file
under 1500 lines.
"""

from __future__ import annotations

import _ast
import ast
from pathlib import Path


def _resolve_relative(file_path: str, level: int, module: str) -> str:
    """Resolve a relative import to an absolute module name."""
    if level == 0:
        return module or ""
    if not file_path:
        # No file context available — fall back to the raw module name.
        return module or ""
    parts = Path(file_path).resolve().parent.parts
    try:
        src_idx = parts.index("src") if "src" in parts else -1
        if src_idx >= 0:
            package_parts = list(parts[src_idx + 1 :])
        else:
            return module or ""
    except (ValueError, IndexError):
        return module or ""
    # Go up 'level' package levels.
    base = package_parts[: len(package_parts) - level] if level <= len(package_parts) else []
    if module:
        return ".".join(base) + "." + module if base else module
    return ".".join(base)


def collect_imports_from_node(
    node: _ast.AST,
    *,
    file_path: str = "",
) -> list[tuple[str, int]]:
    """Extract ``(module_name, line_no)`` pairs from an import/import-from node."""
    result: list[tuple[str, int]] = []
    if isinstance(node, _ast.Import):
        for alias in node.names:
            result.append((alias.name, node.lineno))
    elif isinstance(node, _ast.ImportFrom):
        mod = _resolve_relative(file_path, node.level, node.module or "")
        for alias in node.names:
            result.append((f"{mod}.{alias.name}", node.lineno))
        result.append((mod, node.lineno))
    return result


def top_level_imports(source: str, *, file_path: str = "") -> list[tuple[str, int]]:
    """Return ``(module_name, line_no)`` for all top-level import/from-import nodes.

    Only visits ``ast.Import`` and ``ast.ImportFrom`` nodes that are direct
    children of the module body (i.e. *not* nested inside functions or classes).
    """
    tree = ast.parse(source)
    result: list[tuple[str, int]] = []
    for node in ast.iter_child_nodes(tree):
        result.extend(collect_imports_from_node(node, file_path=file_path))
    return result


def all_imports(source: str, *, file_path: str = "") -> list[tuple[str, int]]:
    """Return ``(module_name, line_no)`` for *all* import nodes in the tree."""
    tree = ast.parse(source)
    result: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        result.extend(collect_imports_from_node(node, file_path=file_path))
    return result


def _is_type_checking_block(parent: _ast.AST) -> bool:
    """Check whether *parent* is an ``if TYPE_CHECKING:`` block."""
    if not isinstance(parent, _ast.If):
        return False
    test = parent.test
    if isinstance(test, _ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if (
        isinstance(test, _ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, _ast.Name)
        and test.value.id == "typing"
    ):
        return True
    return False


def runtime_imports(source: str, *, file_path: str = "") -> list[tuple[str, int]]:
    """Return imports that execute at module load time.

    Excludes imports guarded by ``if TYPE_CHECKING:`` blocks and imports
    inside function/method bodies (deferred imports).
    """
    tree = ast.parse(source)
    result: list[tuple[str, int]] = []

    def _walk_runtime_scope(node: _ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (_ast.Import, _ast.ImportFrom)):
                result.extend(collect_imports_from_node(child, file_path=file_path))
            elif isinstance(child, _ast.If):
                if _is_type_checking_block(child):
                    continue
                _walk_runtime_scope(child)
            elif isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            else:
                _walk_runtime_scope(child)

    _walk_runtime_scope(tree)
    return result


def check_banned_ast(
    imports: list[tuple[str, int]],
    banned_prefixes: tuple[str, ...],
    *,
    rel_path: str,
) -> list[str]:
    """Return violation descriptions for imports matching any banned prefix."""
    violations: list[str] = []
    for mod, lineno in imports:
        for prefix in banned_prefixes:
            if mod == prefix or mod.startswith(prefix + "."):
                violations.append(f"{rel_path}:{lineno}: imports {mod}")
                break
    return violations
