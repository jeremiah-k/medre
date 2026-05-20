"""AST-based import and call analysis for architecture boundary tests.

Provides helpers to:
- Parse Python source files
- Collect imports that execute at module load time (runtime scope)
- Collect all imports (including function-local)
- Collect top-level function calls (to detect blocking I/O)
- Match import prefixes for banned-import checks
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ImportRecord:
    """Single import found in source code."""
    module: str
    lineno: int
    kind: str  # "import" or "import_from"
    file: str | None = None


@dataclass
class CallRecord:
    """Single function call found in source code."""
    func: str
    lineno: int
    file: str | None = None


def parse_python(path: str | Path) -> ast.Module:
    """Parse a Python source file and return its AST."""
    source = Path(path).read_text(encoding="utf-8")
    return ast.parse(source)


def _resolve_relative(level: int, module: str | None, file_path: str | None) -> str:
    """Resolve a relative import to an absolute module name.

    Args:
        level: Number of dots in the relative import (1 for '.', 2 for '..', etc.)
        module: The module part after the dots (may be None/empty)
        file_path: The path of the file containing the import

    Returns:
        The resolved absolute module name, or the raw module string if
        file_path is not available or resolution fails.
    """
    if level == 0 or not file_path:
        return module or ""

    try:
        fpath = Path(file_path).resolve()
        parts = list(fpath.parent.parts)

        # Find the 'src' directory in the path to determine package root
        try:
            src_idx = parts.index("src")
        except ValueError:
            return module or ""

        # Package parts are everything after 'src/medre'
        if src_idx + 1 < len(parts):
            package_parts = parts[src_idx + 1:]
        else:
            return module or ""

        # Go up 'level - 1' levels (level counts dots; file's own dir is 1 level)
        # Remove from the end: go up (level - 1) directories
        if level > 1 and level - 1 <= len(package_parts):
            package_parts = package_parts[:-(level - 1)]
        elif level > 1:
            # Can't go up that far — just use what's left
            package_parts = []

        if module:
            base = ".".join(package_parts)
            return f"{base}.{module}" if base else module
        return ".".join(package_parts) if package_parts else ""
    except (ValueError, IndexError):
        return module or ""


def runtime_scope_imports(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Return imports that execute at module load time.

    Includes:
    - Module-level imports
    - Imports inside module-level if/try/with/for/while/match/class blocks

    Excludes:
    - Imports inside function/method bodies (deferred)
    - Imports inside ``if TYPE_CHECKING:`` blocks

    Args:
        tree: Parsed AST module
        file_path: Optional file path for resolving relative imports

    Returns:
        List of ImportRecord instances with resolved module names and line numbers
    """
    result: list[ImportRecord] = []

    def _is_type_checking(node: ast.AST) -> bool:
        """Check if *node* is an ``if TYPE_CHECKING:`` block."""
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        ):
            return True
        return False

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        result.append(ImportRecord(
                            module=alias.name,
                            lineno=child.lineno,
                            kind="import",
                            file=file_path,
                        ))
                elif isinstance(child, ast.ImportFrom):
                    resolved = _resolve_relative(
                        child.level, child.module, file_path
                    )
                    for alias in child.names:
                        full = f"{resolved}.{alias.name}" if resolved else alias.name
                        result.append(ImportRecord(
                            module=full,
                            lineno=child.lineno,
                            kind="import_from",
                            file=file_path,
                        ))
                    if resolved:
                        result.append(ImportRecord(
                            module=resolved,
                            lineno=child.lineno,
                            kind="import_from",
                            file=file_path,
                        ))
            elif isinstance(child, ast.If):
                if _is_type_checking(child):
                    continue
                _walk(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            else:
                _walk(child)

    _walk(tree)
    return result


def all_imports(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Return ALL imports in the tree, including function-local ones.

    Args:
        tree: Parsed AST module
        file_path: Optional file path for resolving relative imports

    Returns:
        List of ImportRecord instances
    """
    result: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append(ImportRecord(
                    module=alias.name,
                    lineno=node.lineno,
                    kind="import",
                    file=file_path,
                ))
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative(
                node.level, node.module, file_path
            )
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(ImportRecord(
                    module=full,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
            if resolved:
                result.append(ImportRecord(
                    module=resolved,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
    return result


def top_level_calls(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[CallRecord]:
    """Return function calls at module level (not inside function bodies).

    Excludes calls inside TYPE_CHECKING blocks.

    Args:
        tree: Parsed AST module
        file_path: Optional file path for attribution

    Returns:
        List of CallRecord instances
    """
    result: list[CallRecord] = []

    def _is_type_checking(node: ast.AST) -> bool:
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        ):
            return True
        return False

    def _get_call_name(node: ast.Call) -> str:
        """Extract a readable function name from a Call node."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return f"{_get_attribute_chain(node.func)}"
        return ast.dump(node.func)

    def _get_attribute_chain(node: ast.Attribute) -> str:
        """Build a dot-separated chain for attribute access."""
        parts = [node.attr]
        current = node.value
        while isinstance(current, ast.Attribute):
            parts.insert(0, current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.insert(0, current.id)
        return ".".join(parts)

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                result.append(CallRecord(
                    func=_get_call_name(child),
                    lineno=child.lineno,
                    file=file_path,
                ))
                _walk(child)
            elif isinstance(child, ast.If):
                if _is_type_checking(child):
                    continue
                _walk(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                _walk(child)

    _walk(tree)
    return result


def import_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    """Check if *module* matches any of the banned *prefixes*.

    Supports:
    - Exact match: module == prefix
    - Submodule match: module.startswith(prefix + ".")
    """
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def find_relative_imports(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Find all relative imports in the AST and return them resolved.

    Returns imports where level > 0, with resolved absolute module names.
    """
    result: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            resolved = _resolve_relative(node.level, node.module, file_path)
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(ImportRecord(
                    module=full,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
            if resolved:
                result.append(ImportRecord(
                    module=resolved,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
    return result


def extract_aliases(tree: ast.Module) -> dict[str, str]:
    """Extract import aliases from module-level imports.

    Returns a dict mapping local name -> fully qualified name, e.g.:
      import subprocess as sp  ->  {"sp": "subprocess"}
      from subprocess import run  ->  {"run": "subprocess.run"}
    """
    aliases: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            base = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = f"{base}.{alias.name}"
    return aliases


def top_level_imports(
    source: str,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Return imports that are direct children of the module body.

    Only visits Import and ImportFrom nodes that are direct children
    of the module body (i.e. *not* nested inside functions, classes,
    or other blocks).

    Args:
        source: Raw Python source string
        file_path: Optional file path for attribution

    Returns:
        List of ImportRecord instances
    """
    tree = ast.parse(source)
    result: list[ImportRecord] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append(ImportRecord(
                    module=alias.name,
                    lineno=node.lineno,
                    kind="import",
                    file=file_path,
                ))
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative(
                node.level, node.module, file_path
            )
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(ImportRecord(
                    module=full,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
            if resolved:
                result.append(ImportRecord(
                    module=resolved,
                    lineno=node.lineno,
                    kind="import_from",
                    file=file_path,
                ))
    return result
