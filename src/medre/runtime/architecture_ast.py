"""Shared AST primitives for import analysis.

Used by both architecture_report.py (production) and test helpers.
Does NOT import any medre.* modules.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ImportRecord:
    """Single import found in source code."""

    module: str
    lineno: int
    kind: str  # "import" or "import_from"
    file: str | None = None
    is_type_checking: bool = False


@dataclass
class CallRecord:
    """Single function call found in source code."""

    func: str
    lineno: int
    file: str | None = None


def resolve_relative(level: int, module: str | None, file_path: str | None) -> str:
    """Resolve a relative import to absolute module name."""
    if level == 0 or not file_path:
        return module or ""
    try:
        fpath = Path(file_path).resolve()
        parts = list(fpath.parent.parts)
        try:
            src_idx = parts.index("src")
        except ValueError:
            return module or ""
        if src_idx + 1 < len(parts):
            package_parts = parts[src_idx + 1 :]
        else:
            return module or ""
        if level > 1 and level - 1 <= len(package_parts):
            package_parts = package_parts[: -(level - 1)]
        elif level > 1:
            package_parts = []
        if module:
            base = ".".join(package_parts)
            return f"{base}.{module}" if base else module
        return ".".join(package_parts) if package_parts else ""
    except (ValueError, IndexError):
        return module or ""


def is_type_checking(node: ast.AST) -> bool:
    """Check if node is an if TYPE_CHECKING block."""
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


def parse_python(path: str | Path) -> ast.Module:
    """Parse a Python source file and return its AST."""
    source = Path(path).read_text(encoding="utf-8")
    return ast.parse(source)


def runtime_scope_imports(
    tree: ast.Module,
    file_path: str | None = None,
    *,
    record_type_checking: bool = False,
) -> list[ImportRecord]:
    """Return runtime-scope imports (module-level, not in function bodies).

    Args:
        tree: Parsed AST module
        file_path: Optional file path for resolving relative imports
        record_type_checking: If True, TYPE_CHECKING imports are recorded with
            is_type_checking=True. If False, TYPE_CHECKING imports are skipped.

    Returns:
        List of ImportRecord instances
    """
    result: list[ImportRecord] = []

    def _walk(node: ast.AST, in_type_checking: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and is_type_checking(child):
                if record_type_checking:
                    # Walk if-body with in_type_checking=True
                    _walk(
                        ast.Module(body=child.body, type_ignores=[]),
                        in_type_checking=True,
                    )
                    # Walk else-body with prior flag
                    if child.orelse:
                        _walk(
                            ast.Module(body=child.orelse, type_ignores=[]),
                            in_type_checking=in_type_checking,
                        )
                else:
                    # Skip TYPE_CHECKING body, but still process runtime else-branch
                    if child.orelse:
                        _walk(
                            ast.Module(body=child.orelse, type_ignores=[]),
                            in_type_checking=in_type_checking,
                        )
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    result.append(
                        ImportRecord(
                            module=alias.name,
                            lineno=child.lineno,
                            kind="import",
                            file=file_path,
                            is_type_checking=in_type_checking,
                        )
                    )
            elif isinstance(child, ast.ImportFrom):
                resolved = resolve_relative(child.level, child.module, file_path)
                for alias in child.names:
                    full = f"{resolved}.{alias.name}" if resolved else alias.name
                    result.append(
                        ImportRecord(
                            module=full,
                            lineno=child.lineno,
                            kind="import_from",
                            file=file_path,
                            is_type_checking=in_type_checking,
                        )
                    )
                if resolved:
                    result.append(
                        ImportRecord(
                            module=resolved,
                            lineno=child.lineno,
                            kind="import_from",
                            file=file_path,
                            is_type_checking=in_type_checking,
                        )
                    )
            else:
                _walk(child, in_type_checking)

    _walk(tree)
    return result


def all_imports(tree: ast.Module, file_path: str | None = None) -> list[ImportRecord]:
    """Return ALL imports including function-local."""
    result: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append(
                    ImportRecord(
                        module=alias.name,
                        lineno=node.lineno,
                        kind="import",
                        file=file_path,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            resolved = resolve_relative(node.level, node.module, file_path)
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(
                    ImportRecord(
                        module=full,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
            if resolved:
                result.append(
                    ImportRecord(
                        module=resolved,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
    return result


def top_level_calls(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[CallRecord]:
    """Return function calls at module level (not inside function bodies)."""
    result: list[CallRecord] = []

    def _get_call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return _get_attribute_chain(node.func)
        return ast.dump(node.func)

    def _get_attribute_chain(node: ast.Attribute) -> str:
        parts = [node.attr]
        current = node.value
        while isinstance(current, ast.Attribute):
            parts.insert(0, current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.insert(0, current.id)
        elif isinstance(current, ast.Call):
            # e.g. Path("x").read_text() → prefix with "Path"
            parts.insert(0, _get_call_name(current))
        return ".".join(parts)

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                result.append(
                    CallRecord(
                        func=_get_call_name(child),
                        lineno=child.lineno,
                        file=file_path,
                    )
                )
                _walk(child)
            elif isinstance(child, ast.If) and is_type_checking(child):
                # Ignore TYPE_CHECKING body but keep runtime else-branch
                for stmt in child.orelse:
                    _walk(stmt)
                continue
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                _walk(child)

    _walk(tree)
    return result


def import_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    """Check if module matches any prefix (exact or submodule)."""
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def find_relative_imports(
    tree: ast.Module,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Find all relative imports with resolved absolute names."""
    result: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            resolved = resolve_relative(node.level, node.module, file_path)
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(
                    ImportRecord(
                        module=full,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
            if resolved:
                result.append(
                    ImportRecord(
                        module=resolved,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
    return result


def extract_aliases(
    tree: ast.Module,
    file_path: str | None = None,
) -> dict[str, str]:
    """Extract import aliases from module-level runtime-scope imports.

    Uses the same traversal rules as runtime_scope_imports():
    - includes imports inside try/except, with, if, for, while, class bodies
    - excludes function/async function/lambda bodies
    - excludes TYPE_CHECKING bodies
    - includes TYPE_CHECKING else branches

    Returns dict mapping local name -> fully qualified name.
    """
    aliases: dict[str, str] = {}

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        local = alias.asname or alias.name
                        aliases[local] = alias.name
                elif isinstance(child, ast.ImportFrom):
                    base = resolve_relative(child.level, child.module, file_path)
                    for alias in child.names:
                        local = alias.asname or alias.name
                        aliases[local] = f"{base}.{alias.name}" if base else alias.name
            elif isinstance(child, ast.If) and is_type_checking(child):
                for stmt in child.orelse:
                    _walk(stmt)
                continue
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                _walk(child)

    _walk(tree)
    return aliases


def top_level_imports(
    source: str,
    file_path: str | None = None,
) -> list[ImportRecord]:
    """Return imports that are direct children of the module body."""
    tree = ast.parse(source)
    result: list[ImportRecord] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append(
                    ImportRecord(
                        module=alias.name,
                        lineno=node.lineno,
                        kind="import",
                        file=file_path,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            resolved = resolve_relative(node.level, node.module, file_path)
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                result.append(
                    ImportRecord(
                        module=full,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
            if resolved:
                result.append(
                    ImportRecord(
                        module=resolved,
                        lineno=node.lineno,
                        kind="import_from",
                        file=file_path,
                    )
                )
    return result


def resolve_call_name(func_name: str, aliases: dict[str, str]) -> str:
    """Resolve an aliased call name to its fully qualified form.

    Follows alias chains recursively with cycle detection.

    Handles:
      Path.read_text + {"Path": "pathlib.Path"} -> "pathlib.Path.read_text"
      pl.Path.write_text + {"pl": "pathlib"} -> "pathlib.Path.write_text"
      sp.run + {"sp": "subprocess"} -> "subprocess.run"
      run + {"run": "subprocess.run"} -> "subprocess.run"
      obj.read_text (no alias) -> "obj.read_text"
      P.read_text + {"P": "Path", "Path": "pathlib.Path"} -> "pathlib.Path.read_text"
      runner + {"runner": "run", "run": "subprocess.run"} -> "subprocess.run"
      a.x + {"a": "b", "b": "pkg.mod"} -> "pkg.mod.x"

    Args:
        func_name: The call name as extracted by top_level_calls().
        aliases: Module-level alias mapping from extract_aliases().

    Returns:
        Resolved fully qualified name, or func_name unchanged if no alias matches.
    """
    if "." not in func_name:
        seen: set[str] = set()
        current = func_name
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current

    parts = func_name.split(".")
    root = parts[0]
    if root in aliases:
        seen: set[str] = set()
        current = root
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current + "." + ".".join(parts[1:])
    return func_name
