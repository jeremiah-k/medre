"""Architecture dependency graph and boundary reports.

Pure AST-based analysis. Does NOT import any project modules.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ImportEdge:
    """A single import edge from one module to another."""

    source: str  # dotted module path relative to src/medre
    target: str  # the imported module (fully qualified)
    line: int
    kind: str  # "import" or "import_from"
    is_type_checking: bool = False


@dataclass
class ModuleInfo:
    """Information about a single Python module."""

    module: str
    file: str
    imports: list[ImportEdge] = field(default_factory=list)
    layer: str = ""


# Layer classification
_LAYER_ORDER = [
    "config",
    "core",
    "interop",
    "runtime",
    "adapters",
    "cli",
    "plugins",
    "observability",
]


def _classify_layer(module: str) -> str:
    """Classify a medre.* module into a layer."""
    parts = module.split(".") if module.startswith("medre.") else module.split(".")
    if len(parts) >= 2:
        top = parts[1]
        if top in _LAYER_ORDER:
            return top
    return "other"


def _resolve_name(node: ast.AST) -> str | None:
    """Try to get a readable name from an AST node for call detection."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _resolve_relative(level: int, module: str | None, file_path: str) -> str:
    """Resolve a relative import to an absolute module name."""
    if level == 0:
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
        return ".".join(package_parts) if package_parts else module or ""
    except (ValueError, IndexError):
        return module or ""


def _is_type_checking(node: ast.AST) -> bool:
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


def parse_file(py_file: Path) -> list[ImportEdge]:
    """Parse a single Python file and return its import edges (runtime-scope only)."""
    source = py_file.read_text(encoding="utf-8")
    tree = ast.parse(source)
    edges: list[ImportEdge] = []

    def _walk(node: ast.AST, in_type_checking: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                # Record TYPE_CHECKING imports too, but mark them
                _walk(child, in_type_checking=True)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    edges.append(
                        ImportEdge(
                            source="",
                            target=alias.name,
                            line=child.lineno,
                            kind="import",
                            is_type_checking=in_type_checking,
                        )
                    )
            elif isinstance(child, ast.ImportFrom):
                resolved = _resolve_relative(child.level, child.module, str(py_file))
                for alias in child.names:
                    full = f"{resolved}.{alias.name}" if resolved else alias.name
                    edges.append(
                        ImportEdge(
                            source="",
                            target=full,
                            line=child.lineno,
                            kind="import_from",
                            is_type_checking=in_type_checking,
                        )
                    )
                if resolved:
                    edges.append(
                        ImportEdge(
                            source="",
                            target=resolved,
                            line=child.lineno,
                            kind="import_from",
                            is_type_checking=in_type_checking,
                        )
                    )
            else:
                _walk(child, in_type_checking)

    _walk(tree)
    return edges


def module_path_for(py_file: Path, src_root: Path) -> str:
    """Convert a file path under src_root to a dotted module name.

    Prepends ``medre.`` because *src_root* is expected to be
    ``src/medre/`` — the resulting module name includes the top-level
    package.
    """
    rel = py_file.relative_to(src_root)
    parts = list(rel.parts)
    # Remove .py extension from last part
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    # Remove __init__
    parts = [p for p in parts if p != "__init__"]
    return "medre." + ".".join(parts)


@dataclass
class ArchitectureGraph:
    """Full dependency graph for the medre project."""

    modules: dict[str, ModuleInfo] = field(default_factory=dict)


def build_dependency_graph(src_root: Path) -> ArchitectureGraph:
    """Build a dependency graph from source files under src_root.

    Scans all .py files recursively. Pure AST-based; does NOT import modules.
    """
    graph = ArchitectureGraph()
    py_files = sorted(src_root.rglob("*.py"))
    for py_file in py_files:
        module = module_path_for(py_file, src_root)
        if not module.startswith("medre."):
            continue
        info = ModuleInfo(
            module=module,
            file=str(py_file.relative_to(src_root)),
            layer=_classify_layer(module),
        )
        try:
            edges = parse_file(py_file)
            for edge in edges:
                edge.source = module
            info.imports = edges
        except SyntaxError:
            pass  # Skip files with syntax errors
        graph.modules[module] = info
    return graph


def render_dependency_report(graph: ArchitectureGraph) -> str:
    """Render a human-readable dependency report."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("MEDRE Architecture Dependency Report")
    lines.append("=" * 70)
    lines.append("")

    # Group by layer
    by_layer: dict[str, list[ModuleInfo]] = {}
    for info in graph.modules.values():
        by_layer.setdefault(info.layer, []).append(info)

    for layer in _LAYER_ORDER:
        if layer not in by_layer:
            continue
        lines.append(f"\n--- {layer} layer ({len(by_layer[layer])} modules) ---")
        for info in sorted(by_layer[layer], key=lambda m: m.module):
            ext_imports = [
                e
                for e in info.imports
                if e.target.startswith("medre.") and not e.is_type_checking
            ]
            if ext_imports:
                lines.append(f"  {info.module}:")
                for imp in sorted(ext_imports, key=lambda x: x.target):
                    lines.append(f"    -> {imp.target} (line {imp.line})")

    lines.append("\n" + "=" * 70)
    lines.append(f"Total modules: {len(graph.modules)}")
    lines.append(
        f"Total import edges: {sum(len(m.imports) for m in graph.modules.values())}"
    )
    return "\n".join(lines)


# Convenience: forbidden import check helpers

_CORE_FORBIDDEN = (
    "medre.runtime",
    "medre.adapters",
    "medre.cli",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "meshcore",
    "RNS",
    "lxmf",
)

_ROUTE_ENGINE_FORBIDDEN = (
    "medre.adapters",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "medre.runtime.builder",
)

_CONFIG_FORBIDDEN = (
    "medre.adapters",
    "medre.runtime.builder",
    "medre.runtime.route_engine",
    "medre.core.engine",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "meshcore",
    "RNS",
    "lxmf",
)


def build_route_adapter_boundary_report(graph: ArchitectureGraph) -> str:
    """Build a report of route/adapter boundary violations."""
    lines: list[str] = []
    lines.append("Route/Adapter Boundary Report")
    lines.append("-" * 40)

    # Which runtime modules import adapter implementations
    runtime_adapter_imports = []
    for mod, info in graph.modules.items():
        if mod.startswith("medre.runtime."):
            for edge in info.imports:
                if (
                    edge.target.startswith("medre.adapters.")
                    and not edge.is_type_checking
                ):
                    runtime_adapter_imports.append((mod, edge.target, edge.line))

    lines.append(f"\nRuntime → Adapter imports: {len(runtime_adapter_imports)}")
    for mod, target, line in sorted(runtime_adapter_imports):
        lines.append(f"  {mod} -> {target} (line {line})")

    # Route engine check
    route_engine_violations = []
    route_engine = graph.modules.get("medre.runtime.route_engine")
    if route_engine:
        for edge in route_engine.imports:
            for f in _ROUTE_ENGINE_FORBIDDEN:
                target = edge.target
                if target == f or target.startswith(f + "."):
                    route_engine_violations.append((edge.target, edge.line))

    lines.append(f"\nRoute engine forbidden imports: {len(route_engine_violations)}")
    for target, line in route_engine_violations:
        lines.append(f"  {target} (line {line})")

    # Adapter → runtime imports
    adapter_runtime_imports = []
    for mod, info in graph.modules.items():
        if mod.startswith("medre.adapters."):
            for edge in info.imports:
                if (
                    edge.target.startswith("medre.runtime.")
                    and not edge.is_type_checking
                ):
                    adapter_runtime_imports.append((mod, edge.target, edge.line))

    lines.append(f"\nAdapter → Runtime imports: {len(adapter_runtime_imports)}")
    for mod, target, line in sorted(adapter_runtime_imports):
        lines.append(f"  {mod} -> {target} (line {line})")

    # Config → adapter implementation imports
    config_adapter_impl_imports = []
    for mod, info in graph.modules.items():
        if mod.startswith("medre.config."):
            for edge in info.imports:
                # Allow config.adapters.* dataclass imports
                if (
                    edge.target.startswith("medre.adapters.")
                    and not edge.target.startswith("medre.config.adapters.")
                    and not edge.is_type_checking
                ):
                    config_adapter_impl_imports.append((mod, edge.target, edge.line))

    lines.append(
        f"\nConfig → Adapter implementation imports: {len(config_adapter_impl_imports)}"
    )
    for mod, target, line in config_adapter_impl_imports:
        lines.append(f"  {mod} -> {target} (line {line})")

    return "\n".join(lines)


def check_forbidden_imports(
    graph: ArchitectureGraph,
    module_prefix: str,
    forbidden_prefixes: tuple[str, ...],
    *,
    allow_type_checking: bool = True,
) -> list[tuple[str, str, int]]:
    """Check all modules matching *module_prefix* for forbidden imports.

    Returns list of (module, forbidden_import, line_number).
    """
    violations: list[tuple[str, str, int]] = []
    for mod, info in graph.modules.items():
        if not mod.startswith(module_prefix):
            continue
        for edge in info.imports:
            if allow_type_checking and edge.is_type_checking:
                continue
            for forbidden in forbidden_prefixes:
                target = edge.target
                if target == forbidden or target.startswith(forbidden + "."):
                    violations.append((mod, target, edge.line))
                    break
    return violations
