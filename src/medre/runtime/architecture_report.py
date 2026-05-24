"""Architecture dependency graph and boundary reports.

AST-based analysis, delegates shared AST walking to architecture_ast.
"""

from __future__ import annotations

import ast as _ast
from dataclasses import dataclass, field
from pathlib import Path

from medre.runtime.architecture_ast import (
    extract_aliases,
    normalize_import_records_for_graph,
    resolve_call_name,
    runtime_scope_imports,
)


def module_matches(module: str, prefix: str) -> bool:
    """Check if module equals prefix or is a direct child (prefix.xxx)."""
    return module == prefix or module.startswith(prefix + ".")


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
    parts = module.split(".")
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
    records = normalize_import_records_for_graph(records)
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
    parse_errors: dict[str, str] = field(default_factory=dict)


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
        except SyntaxError as exc:
            graph.parse_errors[module] = str(exc)
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
    if graph.parse_errors:
        lines.append("")
        lines.append("== Parse Errors ==")
        for mod, err in sorted(graph.parse_errors.items()):
            lines.append(f"  {mod}: {err}")
    return "\n".join(lines)


def build_dependency_graph_report(
    graph: ArchitectureGraph,
    forbidden_rules: dict[str, tuple[str, ...]] | None = None,
) -> DependencyGraphReport:
    """Build a structured DependencyGraphReport from an ArchitectureGraph.

    Args:
        graph: The dependency graph to analyze.
        forbidden_rules: Mapping of module_prefix -> forbidden import prefixes.
            Defaults to core, config, and route_engine rules if not provided.

    Returns:
        DependencyGraphReport with populated violations and summaries.
    """
    if forbidden_rules is None:
        forbidden_rules = {
            "medre.core": _CORE_FORBIDDEN,
            "medre.config": _CONFIG_FORBIDDEN,
            "medre.runtime.route_engine": _ROUTE_ENGINE_FORBIDDEN,
        }

    all_violations: dict[str, list[BoundaryViolation]] = {}
    for prefix, forbidden in forbidden_rules.items():
        by_mod = check_forbidden_imports_by_module(graph, prefix, forbidden)
        for mod, viols in by_mod.items():
            all_violations.setdefault(mod, []).extend(viols)

    # Layer summary
    layer_summary: dict[str, int] = {}
    for info in graph.modules.values():
        layer_summary[info.layer] = layer_summary.get(info.layer, 0) + 1

    total_edges = sum(len(m.imports) for m in graph.modules.values())

    return DependencyGraphReport(
        modules=graph.modules,
        forbidden_imports_by_module=all_violations,
        layer_summary=layer_summary,
        total_edges=total_edges,
        parse_errors=graph.parse_errors,
    )


def render_dependency_graph_report(report: DependencyGraphReport) -> str:
    """Render a DependencyGraphReport as a human-readable string."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("MEDRE Dependency Graph Report")
    lines.append("=" * 70)
    lines.append("")

    lines.append(f"Total modules: {len(report.modules)}")
    lines.append(f"Total edges: {report.total_edges}")
    lines.append("")

    # Layer summary
    lines.append("--- Layer Summary ---")
    for layer, count in sorted(report.layer_summary.items()):
        lines.append(f"  {layer}: {count} modules")
    lines.append("")

    # Violations
    total_violations = sum(len(v) for v in report.forbidden_imports_by_module.values())
    lines.append(f"--- Violations ({total_violations} total) ---")
    if report.forbidden_imports_by_module:
        for mod in sorted(report.forbidden_imports_by_module):
            for v in report.forbidden_imports_by_module[mod]:
                lines.append(f"  {v.source} -> {v.target} (line {v.line}): {v.rule}")
    else:
        lines.append("  (none)")

    # Parse errors
    if report.parse_errors:
        lines.append("")
        lines.append(f"== Parse Errors ({len(report.parse_errors)}) ==")
        for mod, err in sorted(report.parse_errors.items()):
            lines.append(f"  {mod}: {err}")

    return "\n".join(lines)


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
    "LXMF",
)

_ROUTE_ENGINE_FORBIDDEN = (
    "medre.adapters",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "meshcore",
    "RNS",
    "lxmf",
    "LXMF",
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
    "LXMF",
)

# Forbidden prefixes for codec/renderer modules
_CODEC_RENDERER_FORBIDDEN = (
    "nio",
    "meshtastic",
    "aiohttp",
    "bleak",
    "serial",
    "serial_asyncio",
    "meshcore",
    "RNS",
    "lxmf",
    "LXMF",
    "medre.runtime",
    "medre.core.engine",
    "medre.core.storage",
    "medre.cli",
)

# Canonical set of transport SDK package names — single source of truth.
_SDK_PACKAGES = (
    "nio",
    "meshtastic",
    "meshcore",
    "RNS",
    "lxmf",
    "LXMF",
    "aiohttp",
    "bleak",
    "serial",
    "serial_asyncio",
)

# Import-line prefixes derived from _SDK_PACKAGES.
_BANNED_SDK_IMPORT_PREFIXES = tuple(
    s for sdk in _SDK_PACKAGES for s in (f"import {sdk}", f"from {sdk}")
)


@dataclass
class BoundaryViolation:
    """A single import boundary violation."""

    source: str  # module doing the import
    target: str  # module being imported
    line: int
    rule: str = ""  # which rule was violated


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
    parse_errors: dict[str, str] = field(default_factory=dict)


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
    dynamic_scan_errors: BoundarySection

    # Backward-compatible aliases
    @property
    def runtime_to_adapter(self) -> BoundarySection:
        return self.forbidden_runtime_adapter


# Per-transport allowed SDKs for session modules.
SESSION_ALLOWED_SDKS: dict[str, tuple[str, ...]] = {
    "matrix": ("nio", "aiohttp"),
    "meshtastic": ("meshtastic", "serial", "serial_asyncio"),
    "meshcore": ("meshcore", "bleak", "serial", "serial_asyncio"),
    "lxmf": ("RNS", "LXMF", "lxmf"),
}

_FAKE_TO_TRANSPORT = {
    "fake_matrix": "matrix",
    "fake_meshtastic": "meshtastic",
    "fake_meshcore": "meshcore",
    "fake_lxmf": "lxmf",
}


def _transport_for(module: str) -> str | None:
    """Extract transport name from an adapter module path.

    E.g. ``medre.adapters.matrix.codec`` → ``matrix``.

    Fake transports like ``medre.adapters.fakes.lxmf`` are normalised
    to their canonical transport name (``lxmf``).
    """
    prefix = "medre.adapters."
    if not module.startswith(prefix):
        return None
    rest = module[len(prefix) :]
    parts = rest.split(".")
    transport = parts[0] if parts else None
    if transport and transport in _FAKE_TO_TRANSPORT:
        transport = _FAKE_TO_TRANSPORT[transport]
    return transport


def _extract_string_kwargs(call_node: _ast.Call, param_name: str) -> str | None:
    """Extract a string keyword argument from an AST Call node."""
    for kw in call_node.keywords:
        if (
            kw.arg == param_name
            and isinstance(kw.value, _ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _collect_adapter_strings(
    node: _ast.AST, lineno: int, results: list[tuple[str, int, str]]
) -> None:
    """Recursively collect medre.adapters.* string constants from an AST node."""
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        if node.value.startswith("medre.adapters."):
            results.append(
                (node.value, lineno, f"dynamic registry literal: {node.value}")
            )
    elif isinstance(node, (_ast.List, _ast.Tuple, _ast.Set)):
        for elt in node.elts:
            _collect_adapter_strings(elt, lineno, results)
    elif isinstance(node, _ast.Dict):
        for key in node.keys:
            if key is not None:
                _collect_adapter_strings(key, lineno, results)
        for val in node.values:
            _collect_adapter_strings(val, lineno, results)


def extract_dynamic_adapter_imports(source: str) -> list[tuple[str, int, str]]:
    """Extract dynamic adapter module strings from builder source.

    Parses AST for ``_AdapterFactory(module="medre.adapters....")`` calls,
    ``_ADAPTER_RENDERER_SPECS`` list literals, ``importlib.import_module()``
    calls, and ``__import__()`` calls.

    Alias-aware: recognizes aliased imports such as
    ``import importlib as il`` or ``from importlib import import_module as im``.

    Only string-literal arguments are detected; variable or concatenated
    arguments are invisible to this static analysis.

    Conservatively detects bare ``import_module(...)`` calls without import
    context — these may be false positives if a local function shadows the name.

    Returns list of ``(module_path, line_number, reason)``.
    """
    tree = _ast.parse(source)
    aliases = extract_aliases(tree)
    results: list[tuple[str, int, str]] = []

    for node in _ast.walk(tree):
        # _AdapterFactory(module="medre.adapters....", ...)
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name) and func.id == "_AdapterFactory":
                module = _extract_string_kwargs(node, "module")
                if module:
                    results.append(
                        (module, node.lineno, f"dynamic builder assembly: {module}")
                    )
            # __import__("medre.adapters....", ...)
            elif (
                isinstance(func, _ast.Name)
                and resolve_call_name(func.id, aliases) == "__import__"
            ):
                if (
                    node.args
                    and isinstance(node.args[0], _ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.startswith("medre.adapters.")
                ):
                    mod_name = node.args[0].value
                    results.append(
                        (mod_name, node.lineno, f"dynamic __import__: {mod_name}")
                    )
            # importlib.import_module("medre.adapters....")
            elif (
                isinstance(func, _ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, _ast.Name)
                and resolve_call_name(func.value.id, aliases) == "importlib"
            ) or (
                # from importlib import import_module; import_module("...")
                isinstance(func, _ast.Name)
                and resolve_call_name(func.id, aliases) == "importlib.import_module"
            ):
                if (
                    node.args
                    and isinstance(node.args[0], _ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.startswith("medre.adapters.")
                ):
                    mod_name = node.args[0].value
                    results.append(
                        (mod_name, node.lineno, f"dynamic import_module: {mod_name}")
                    )

    # Also find _ADAPTER_RENDERER_SPECS tuples (can be Assign or AnnAssign)
    for node in _ast.walk(tree):
        target_name = None
        value_node = None
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, _ast.Name)
                    and target.id == "_ADAPTER_RENDERER_SPECS"
                ):
                    target_name = target.id
                    value_node = node.value
                    break
        elif isinstance(node, _ast.AnnAssign):
            if (
                isinstance(node.target, _ast.Name)
                and node.target.id == "_ADAPTER_RENDERER_SPECS"
            ):
                target_name = node.target.id
                value_node = node.value
        if target_name and value_node and isinstance(value_node, _ast.List):
            for elt in value_node.elts:
                if isinstance(elt, _ast.Tuple) and len(elt.elts) >= 1:
                    if (
                        isinstance(elt.elts[0], _ast.Constant)
                        and isinstance(elt.elts[0].value, str)
                        and elt.elts[0].value.startswith("medre.adapters.")
                    ):
                        results.append(
                            (
                                elt.elts[0].value,
                                elt.lineno,
                                f"dynamic renderer spec: {elt.elts[0].value}",
                            )
                        )

    # Detect adapter module strings in registry-like assignments
    _REGISTRY_NAME_PARTS = frozenset(
        {
            "ADAPTER",
            "RENDERER",
            "FACTORY",
            "REGISTRY",
            "SPEC",
            "SPECS",
            "BUILDER",
        }
    )

    for node in _ast.walk(tree):
        target_name = None
        value_node = None
        node_lineno = 0
        if isinstance(node, _ast.Assign):
            node_lineno = node.lineno
            for target in node.targets:
                if isinstance(target, _ast.Name):
                    upper = target.id.upper()
                    if (
                        any(part in upper for part in _REGISTRY_NAME_PARTS)
                        and target.id != "_ADAPTER_RENDERER_SPECS"
                        and target.id != "_AdapterFactory"
                    ):
                        target_name = target.id
                        value_node = node.value
                        break
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            node_lineno = node.lineno
            upper = node.target.id.upper()
            if (
                any(part in upper for part in _REGISTRY_NAME_PARTS)
                and node.target.id != "_ADAPTER_RENDERER_SPECS"
            ):
                target_name = node.target.id
                value_node = node.value
        if target_name and value_node is not None:
            _collect_adapter_strings(value_node, node_lineno, results)

    return results


def build_route_adapter_boundary_report(
    graph: ArchitectureGraph,
    *,
    src_root: Path | None = None,
) -> RouteAdapterBoundaryReport:
    """Build a v2 structured report of route/adapter boundary violations."""
    # --- Allowed Runtime → Adapter Assembly ---
    allowed: list[BoundaryViolation] = []
    scan_errors: list[BoundaryViolation] = []
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

    # --- Dynamic RuntimeBuilder Assembly ---
    builder_info = graph.modules.get("medre.runtime.builder")
    if builder_info and src_root is not None:
        builder_file = src_root / builder_info.file
        if builder_file.exists():
            try:
                source = builder_file.read_text(encoding="utf-8")
                dynamic = extract_dynamic_adapter_imports(source)
                for target, line, reason in dynamic:
                    allowed.append(
                        BoundaryViolation(
                            source="medre.runtime.builder",
                            target=target,
                            line=line,
                            rule=reason,
                        )
                    )
            except (SyntaxError, OSError) as exc:
                scan_errors.append(
                    BoundaryViolation(
                        source="medre.runtime.builder",
                        target=f"<scan error: {builder_file}>",
                        line=0,
                        rule=f"dynamic scan error: {exc}",
                    )
                )

    # --- Forbidden Runtime → Adapter Imports ---
    forbidden: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if mod != "medre.runtime" and not mod.startswith("medre.runtime."):
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

    # --- Non-builder runtime dynamic adapter refs ---
    if src_root is not None:
        for mod, info in graph.modules.items():
            if (
                mod != "medre.runtime" and not mod.startswith("medre.runtime.")
            ) or mod == "medre.runtime.builder":
                continue
            mod_file = src_root / info.file
            if mod_file.exists():
                try:
                    source = mod_file.read_text(encoding="utf-8")
                    dynamic = extract_dynamic_adapter_imports(source)
                    for target, line, reason in dynamic:
                        forbidden.append(
                            BoundaryViolation(
                                source=mod,
                                target=target,
                                line=line,
                                rule=f"forbidden dynamic: {reason}",
                            )
                        )
                except (SyntaxError, OSError) as exc:
                    scan_errors.append(
                        BoundaryViolation(
                            source=mod,
                            target=f"<scan error: {mod_file}>",
                            line=0,
                            rule=f"dynamic scan error: {exc}",
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
                if module_matches(target, f):
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
            if edge.target.startswith("medre.adapters.") and not edge.target.startswith(
                "medre.config.adapters."
            ):
                config_adapter_impl_imports.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule="forbidden: config -> adapter impl",
                    )
                )

    # --- Adapter → Foreign Transport Imports ---
    adapter_cross: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        transport = _transport_for(mod)
        if transport is None:
            continue
        if mod.endswith(".adapter"):
            continue  # adapter wrapper importing its own submodules is fine
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            if not edge.target.startswith("medre.adapters."):
                continue
            target_transport = _transport_for(edge.target)
            if target_transport is not None and target_transport != transport:
                adapter_cross.append(
                    BoundaryViolation(
                        source=mod,
                        target=edge.target,
                        line=edge.line,
                        rule=f"adapter foreign transport: {target_transport}",
                    )
                )

    # --- Codec/Renderer → Forbidden Imports ---
    codec_renderer_violations: list[BoundaryViolation] = []
    for mod, info in graph.modules.items():
        if not mod.endswith((".codec", ".renderer")) or not mod.startswith(
            "medre.adapters."
        ):
            continue
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            for f in _CODEC_RENDERER_FORBIDDEN:
                target = edge.target
                if module_matches(target, f):
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
    for mod, info in graph.modules.items():
        if not mod.endswith(".session") or not mod.startswith("medre.adapters."):
            continue
        own_transport = _transport_for(mod)
        allowed_sdks = SESSION_ALLOWED_SDKS.get(own_transport or "", ())
        for edge in info.imports:
            if edge.is_type_checking:
                continue
            target = edge.target
            # Check SDK imports from other transports
            for sdk in _SDK_PACKAGES:
                if module_matches(target, sdk):
                    # Allow if SDK belongs to own transport
                    if not any(
                        module_matches(target, allowed) for allowed in allowed_sdks
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
    for v in allowed:
        assembly.append(
            BoundaryViolation(
                source=v.source,
                target=v.target,
                line=v.line,
                rule="allowed: RuntimeBuilder adapter assembly",
            )
        )
    for v in forbidden:
        assembly.append(
            BoundaryViolation(
                source=v.source,
                target=v.target,
                line=v.line,
                rule="violation: non-builder runtime assembly point",
            )
        )

    def sort_key(v):
        return (v.source, v.target, v.line)

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
            title="Adapter → Foreign Transport Imports",
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
        dynamic_scan_errors=BoundarySection(
            title="Dynamic Scan Errors",
            violations=sorted(scan_errors, key=sort_key),
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
        report.dynamic_scan_errors,
    ]

    # Track which sections represent allowed (not violations) entries
    allowed_titles = {
        "Allowed Runtime → Adapter Assembly",
        "Runtime Assembly Points",
    }

    for section in sections:
        if section.title == "Dynamic Scan Errors":
            count_label = "errors"
        elif section.title in allowed_titles:
            count_label = "entries"
        else:
            count_label = "violations"
        lines.append(f"--- {section.title} ({section.count} {count_label}) ---")
        if section.violations:
            for v in section.violations:
                lines.append(f"  {v.source} -> {v.target} (line {v.line}): {v.rule}")
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
        if not module_matches(mod, module_prefix):
            continue
        for edge in info.imports:
            if allow_type_checking and edge.is_type_checking:
                continue
            for forbidden in forbidden_prefixes:
                target = edge.target
                if module_matches(target, forbidden):
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
        if not module_matches(mod, module_prefix):
            continue
        for edge in info.imports:
            if allow_type_checking and edge.is_type_checking:
                continue
            for forbidden in forbidden_prefixes:
                target = edge.target
                if module_matches(target, forbidden):
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
