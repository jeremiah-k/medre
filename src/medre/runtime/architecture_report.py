"""Architecture dependency graph and boundary reports.

AST-based analysis, delegates shared AST walking to architecture_ast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Re-export for backward compatibility with test imports
from medre.runtime.architecture_ast import (  # noqa: F401
    is_type_checking as _is_type_checking,
)
from medre.runtime.architecture_ast import (  # noqa: F401
    resolve_relative as _resolve_relative,
)
from medre.runtime.architecture_ast import (
    runtime_scope_imports,
)

import ast as _ast  # noqa: F401 — kept for re-export compatibility


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


def parse_file(py_file: Path) -> list[ImportEdge]:
    """Parse a single Python file and return its import edges (runtime-scope only)."""
    source = py_file.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    records = runtime_scope_imports(
        tree, file_path=str(py_file), record_type_checking=True
    )
    edges: list[ImportEdge] = []
    for rec in records:
        edges.append(
            ImportEdge(
                source="",  # filled later by build_dependency_graph
                target=rec.module,
                line=rec.lineno,
                kind=rec.kind,
                is_type_checking=rec.is_type_checking,
            )
        )
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
    return "medre" if not parts else "medre." + ".".join(parts)


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
        if module != "medre" and not module.startswith("medre."):
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

# Forbidden prefixes for codec/renderer modules
_CODEC_RENDERER_FORBIDDEN = (
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "meshcore",
    "RNS",
    "lxmf",
    "medre.runtime",
    "medre.core.engine",
    "medre.core.storage",
    "medre.cli",
)


@dataclass
class BoundaryViolation:
    """A single import boundary violation."""

    source: str      # module doing the import
    target: str      # module being imported
    line: int
    rule: str = ""   # which rule was violated


@dataclass
class BoundarySection:
    """One section of the boundary report."""

    title: str
    violations: list[BoundaryViolation]

    @property
    def count(self) -> int:
        return len(self.violations)


@dataclass
class DependencyGraphReport:
    """Structured dependency analysis with pre-computed violations."""

    modules: dict[str, ModuleInfo]
    forbidden_imports_by_module: dict[str, list[BoundaryViolation]]
    layer_summary: dict[str, int]
    total_edges: int


@dataclass
class RouteAdapterBoundaryReport:
    """Structured boundary report for route/adapter dependencies (v2)."""

    allowed_runtime_adapter: BoundarySection
    forbidden_runtime_adapter: BoundarySection
    route_engine_forbidden: BoundarySection
    adapter_to_runtime: BoundarySection
    config_to_adapter_impl: BoundarySection
    adapter_cross_imports: BoundarySection
    codec_renderer_forbidden: BoundarySection
    session_foreign_sdk: BoundarySection
    adapter_wrapper_foreign_transport: BoundarySection
    runtime_assembly_points: BoundarySection

    # Backward-compatible aliases
    @property
    def runtime_to_adapter(self) -> BoundarySection:
        return self.forbidden_runtime_adapter


def _transport_for(module: str) -> str | None:
    """Extract transport name from an adapter module path.

    E.g. ``medre.adapters.matrix.codec`` → ``matrix``.
    """
    prefix = "medre.adapters."
    if not module.startswith(prefix):
        return None
    rest = module[len(prefix):]
    parts = rest.split(".")
    return parts[0] if parts else None


_ADAPTER_INNER_MODULES = frozenset({"codec", "renderer", "session"})


def _is_adapter_inner(module: str) -> bool:
    """True if module is a codec/renderer/session inside an adapter transport."""
    prefix = "medre.adapters."
    if not module.startswith(prefix):
        return False
    rest = module[len(prefix):]
    parts = rest.split(".")
    return len(parts) >= 2 and parts[-1] in _ADAPTER_INNER_MODULES


def build_route_adapter_boundary_report(
    graph: ArchitectureGraph,
) -> RouteAdapterBoundaryReport:
    """Build a v2 structured report of route/adapter boundary violations."""
    # --- Allowed Runtime → Adapter Assembly ---
    allowed: list[BoundaryViolation] = []
    builder_info = graph.modules.get("medre.runtime.builder")
    if builder_info:
        for edge in builder_info.imports:
            if edge.target.startswith("medre.adapters.") and not edge.is_type_checking:
                allowed.append(
                    BoundaryViolation(
                        source="medre.runtime.builder",
                        target=edge.target,
                        line=edge.line,
                        rule="allowed: builder -> adapter assembly",
                    )
                )

    # --- Forbidden Runtime → Adapter Imports ---
    forbidden: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.startswith("medre.runtime."):
            continue
        if mod == "medre.runtime.builder":
            continue
        for edge in info.imports:
            if edge.target.startswith("medre.adapters.") and not edge.is_type_checking:
                forbidden.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule="forbidden: non-builder runtime -> adapter",
                    )
                )

    # --- Route Engine → Adapter/SDK Imports ---
    route_engine_violations: list[BoundaryViolation] = []
    route_engine = graph.modules.get("medre.runtime.route_engine")
    if route_engine:
        for edge in route_engine.imports:
            if edge.is_type_checking:
                continue
            for f in _ROUTE_ENGINE_FORBIDDEN:
                target = edge.target
                if target == f or target.startswith(f + "."):
                    route_engine_violations.append(
                        BoundaryViolation(
                            source="medre.runtime.route_engine",
                            target=target,
                            line=edge.line,
                            rule=f"forbidden prefix: {f}",
                        )
                    )

    # --- Adapter → Runtime Imports ---
    adapter_runtime_imports: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.startswith("medre.adapters."):
            continue
        for edge in info.imports:
            if edge.target.startswith("medre.runtime.") and not edge.is_type_checking:
                adapter_runtime_imports.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule="forbidden: adapter -> runtime",
                    )
                )

    # --- Config → Adapter Implementation Imports ---
    config_adapter_impl_imports: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.startswith("medre.config."):
            continue
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            # Allow config.adapters.* dataclass imports
            if (
                edge.target.startswith("medre.adapters.")
                and not edge.target.startswith("medre.config.adapters.")
            ):
                config_adapter_impl_imports.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule="forbidden: config -> adapter impl",
                    )
                )

    # --- Adapter Cross-Imports (same transport) ---
    adapter_cross: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        transport = _transport_for(mod)
        if transport is None:
            continue
        if mod.endswith(".adapter"):
            continue  # adapter wrapper importing its own submodules is fine
        for edge in info.imports:
            if not edge.target.startswith(f"medre.adapters.{transport}."):
                continue
            if edge.target == mod or edge.is_type_checking:
                continue
            target_rest = edge.target[len(f"medre.adapters.{transport}."):]
            if target_rest in _ADAPTER_INNER_MODULES:
                adapter_cross.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule=f"adapter cross-import ({transport})",
                    )
                )

    # --- Codec/Renderer → Forbidden Imports ---
    codec_renderer_violations: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not (
            mod.endswith(".codec") or mod.endswith(".renderer")
        ) or not mod.startswith("medre.adapters."):
            continue
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            for f in _CODEC_RENDERER_FORBIDDEN:
                target = edge.target
                if target == f or target.startswith(f + "."):
                    codec_renderer_violations.append(
                        BoundaryViolation(
                            source=mod,
                            target=target,
                            line=edge.line,
                            rule=f"codec/renderer forbidden: {f}",
                        )
                    )
                    break

    # --- Session → Foreign SDK/Transport Imports ---
    session_foreign: list[BoundaryViolation] = []
    _SDK_PREFIXES = (
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "serial_asyncio",
        "meshcore",
        "RNS",
        "lxmf",
    )
    # Per-transport allowed SDKs
    session_allowed_sdks = {
        "matrix": ("nio",),
        "meshtastic": ("meshtastic", "serial", "serial_asyncio"),
        "meshcore": ("meshcore",),
        "lxmf": ("RNS", "lxmf"),
    }
    for mod, info in graph.modules.items():
        if not mod.endswith(".session") or not mod.startswith("medre.adapters."):
            continue
        own_transport = _transport_for(mod)
        allowed_sdks = session_allowed_sdks.get(own_transport or "", ())
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            target = edge.target
            # Check SDK imports from other transports
            for sdk in _SDK_PREFIXES:
                if target == sdk or target.startswith(sdk + "."):
                    # Allow if SDK belongs to own transport
                    if not any(
                        target == allowed or target.startswith(allowed + ".")
                        for allowed in allowed_sdks
                    ):
                        session_foreign.append(
                            BoundaryViolation(
                                source=mod,
                                target=target,
                                line=edge.line,
                                rule=f"session foreign SDK import: {sdk}",
                            )
                        )
                    break
            # Check imports from other adapter transports
            if target.startswith("medre.adapters."):
                target_transport = _transport_for(target)
                if target_transport and target_transport != own_transport:
                    session_foreign.append(
                        BoundaryViolation(
                            source=mod,
                            target=target,
                            line=edge.line,
                            rule=f"session foreign transport: {target_transport}",
                        )
                    )

    # --- Adapter Wrapper → Foreign Transport Imports ---
    wrapper_foreign: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.endswith(".adapter") or not mod.startswith("medre.adapters."):
            continue
        own_transport = _transport_for(mod)
        if own_transport is None:
            continue
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            if edge.target.startswith("medre.adapters."):
                target_transport = _transport_for(edge.target)
                if target_transport and target_transport != own_transport:
                    wrapper_foreign.append(
                        BoundaryViolation(
                            source=mod,
                            target=edge.target,
                            line=edge.line,
                            rule=f"wrapper foreign transport: {target_transport}",
                        )
                    )

    # --- Runtime Assembly Points ---
    assembly: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.startswith("medre.runtime."):
            continue
        for edge in info.imports:
            if edge.target.startswith("medre.adapters.") and not edge.is_type_checking:
                if mod != "medre.runtime.builder":
                    assembly.append(
                        BoundaryViolation(
                            source=mod,
                            target=edge.target,
                            line=edge.line,
                            rule="violation: non-builder runtime assembly point",
                        )
                    )

    sort_key = lambda v: (v.source, v.target, v.line)
    return RouteAdapterBoundaryReport(
        allowed_runtime_adapter=BoundarySection(
            title="Allowed Runtime → Adapter Assembly",
            violations=sorted(allowed, key=sort_key),
        ),
        forbidden_runtime_adapter=BoundarySection(
            title="Forbidden Runtime → Adapter Imports",
            violations=sorted(forbidden, key=sort_key),
        ),
        route_engine_forbidden=BoundarySection(
            title="Route Engine → Adapter/SDK Imports",
            violations=sorted(route_engine_violations, key=sort_key),
        ),
        adapter_to_runtime=BoundarySection(
            title="Adapter → Runtime Imports",
            violations=sorted(adapter_runtime_imports, key=sort_key),
        ),
        config_to_adapter_impl=BoundarySection(
            title="Config → Adapter Implementation Imports",
            violations=sorted(config_adapter_impl_imports, key=sort_key),
        ),
        adapter_cross_imports=BoundarySection(
            title="Adapter Cross-Imports (same transport)",
            violations=sorted(adapter_cross, key=sort_key),
        ),
        codec_renderer_forbidden=BoundarySection(
            title="Codec/Renderer → Forbidden Imports",
            violations=sorted(codec_renderer_violations, key=sort_key),
        ),
        session_foreign_sdk=BoundarySection(
            title="Session → Foreign SDK/Transport Imports",
            violations=sorted(session_foreign, key=sort_key),
        ),
        adapter_wrapper_foreign_transport=BoundarySection(
            title="Adapter Wrapper → Foreign Transport Imports",
            violations=sorted(wrapper_foreign, key=sort_key),
        ),
        runtime_assembly_points=BoundarySection(
            title="Runtime Assembly Points",
            violations=sorted(assembly, key=sort_key),
        ),
    )


def render_boundary_report(report: RouteAdapterBoundaryReport) -> str:
    """Render a structured boundary report as a human-readable string."""
    lines: list[str] = []
    lines.append("== Route/Adapter Boundary Report ==")
    lines.append("")

    sections = [
        report.allowed_runtime_adapter,
        report.forbidden_runtime_adapter,
        report.route_engine_forbidden,
        report.adapter_to_runtime,
        report.config_to_adapter_impl,
        report.adapter_cross_imports,
        report.codec_renderer_forbidden,
        report.session_foreign_sdk,
        report.adapter_wrapper_foreign_transport,
        report.runtime_assembly_points,
    ]

    for section in sections:
        lines.append(f"--- {section.title} ---")
        if section.violations:
            for v in section.violations:
                lines.append(f"  {v.source} -> {v.target}")
        else:
            lines.append("  (none)")
        lines.append("")

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


def check_forbidden_imports_by_module(
    graph: ArchitectureGraph,
    module_prefix: str,
    forbidden_prefixes: tuple[str, ...],
    *,
    allow_type_checking: bool = True,
) -> dict[str, list[BoundaryViolation]]:
    """Like check_forbidden_imports but grouped by violating module."""
    violations: dict[str, list[BoundaryViolation]] = {}
    for mod, info in graph.modules.items():
        if not mod.startswith(module_prefix):
            continue
        for edge in info.imports:
            if allow_type_checking and edge.is_type_checking:
                continue
            for forbidden in forbidden_prefixes:
                target = edge.target
                if target == forbidden or target.startswith(forbidden + "."):
                    violations.setdefault(mod, []).append(
                        BoundaryViolation(
                            source=mod,
                            target=target,
                            line=edge.line,
                            rule=f"forbidden prefix: {forbidden}",
                        )
                    )
                    break
    return violations
