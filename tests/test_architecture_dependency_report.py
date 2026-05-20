"""Tests for the architecture dependency graph and boundary reports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from medre.runtime.architecture_report import (
    _CONFIG_FORBIDDEN,
    _CORE_FORBIDDEN,
    _ROUTE_ENGINE_FORBIDDEN,
    ArchitectureGraph,
    ImportEdge,
    ModuleInfo,
    RouteAdapterBoundaryReport,
    _is_type_checking,
    _resolve_name,
    _resolve_relative,
    build_dependency_graph,
    build_route_adapter_boundary_report,
    check_forbidden_imports,
    module_path_for,
    parse_file,
    render_boundary_report,
    render_dependency_report,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "medre"


class TestModulePathFor:
    """Tests for module_path_for()."""

    def test_regular_module(self) -> None:
        p = _SRC / "core" / "events" / "canonical.py"
        assert module_path_for(p, _SRC) == "medre.core.events.canonical"

    def test_init_module(self) -> None:
        p = _SRC / "core" / "events" / "__init__.py"
        assert module_path_for(p, _SRC) == "medre.core.events"

    def test_nested_init(self) -> None:
        p = _SRC / "config" / "adapters" / "__init__.py"
        assert module_path_for(p, _SRC) == "medre.config.adapters"


class TestParseFile:
    """Tests for parse_file()."""

    def test_parse_imports(self) -> None:
        py_file = _SRC / "config" / "model.py"
        edges = parse_file(py_file)
        modules = {e.target for e in edges}
        # Should find some medre.config.adapters.* imports
        assert any("medre.config.adapters" in m for m in modules)

    def test_skips_function_body_imports(self) -> None:
        py_file = _SRC / "config" / "model.py"
        edges = parse_file(py_file)
        # The deferred import of RouteConfigSet inside a function should NOT appear
        assert not any("_RCS" in e.target for e in edges)

    def test_includes_type_checking_as_marked(self) -> None:
        py_file = _SRC / "core" / "engine" / "pipeline.py"
        edges = parse_file(py_file)
        type_checking = [e for e in edges if e.is_type_checking]
        assert any("CapacityController" in e.target for e in type_checking)


class TestBuildGraph:
    """Tests for build_dependency_graph()."""

    def test_graph_contains_modules(self) -> None:
        graph = build_dependency_graph(_SRC)
        assert "medre.config.model" in graph.modules
        assert "medre.core.events.canonical" in graph.modules

    def test_graph_has_edges(self) -> None:
        graph = build_dependency_graph(_SRC)
        config_model = graph.modules.get("medre.config.model")
        assert config_model is not None
        assert len(config_model.imports) > 0


class TestCoreBoundary:
    """Core modules must not import runtime, adapters, SDKs, or CLI."""

    def test_core_no_forbidden_imports(self) -> None:
        graph = build_dependency_graph(_SRC)
        violations = check_forbidden_imports(graph, "medre.core", _CORE_FORBIDDEN)
        assert not violations, "Core modules have forbidden imports:\n" + "\n".join(
            f"  {m}: {t} (line {ln})" for m, t, ln in violations
        )


class TestConfigBoundary:
    """Config modules must not import adapters or SDKs."""

    def test_config_no_forbidden_imports(self) -> None:
        graph = build_dependency_graph(_SRC)
        violations = check_forbidden_imports(graph, "medre.config", _CONFIG_FORBIDDEN)
        assert not violations, "Config modules have forbidden imports:\n" + "\n".join(
            f"  {m}: {t} (line {ln})" for m, t, ln in violations
        )


class TestAdapterReuseBoundary:
    """Codec/renderer modules must not import runtime, storage, SDKs."""

    _ADAPTER_FORBIDDEN = (
        "medre.runtime",
        "medre.cli",
        "medre.core.engine",
        "medre.core.storage",
    )

    def test_codec_renderer_no_forbidden(self) -> None:
        graph = build_dependency_graph(_SRC)
        codec_renderer_modules = [
            m
            for m in graph.modules
            if any(m.endswith(s) for s in [".codec", ".renderer"])
            and m.startswith("medre.adapters.")
        ]
        violations: list[tuple[str, str, int]] = []
        for mod in codec_renderer_modules:
            for module, target, line in check_forbidden_imports(
                graph, mod, self._ADAPTER_FORBIDDEN
            ):
                violations.append((module, target, line))
        assert (
            not violations
        ), "Codec/renderer modules have forbidden imports:\n" + "\n".join(
            f"  {m}: {t} (line {ln})" for m, t, ln in violations
        )


class TestReportDeterminism:
    """Reports must be deterministic."""

    def test_render_is_deterministic(self) -> None:
        graph1 = build_dependency_graph(_SRC)
        graph2 = build_dependency_graph(_SRC)
        report1 = render_dependency_report(graph1)
        report2 = render_dependency_report(graph2)
        assert report1 == report2


# ---------------------------------------------------------------------------
# New tests covering previously-uncovered lines
# ---------------------------------------------------------------------------


class TestResolveName:
    """Tests for _resolve_name() — lines 48-59."""

    def test_ast_name_returns_id(self) -> None:
        """ast.Name node returns its .id."""
        node = ast.Name(id="foo", ctx=ast.Load())
        assert _resolve_name(node) == "foo"

    def test_ast_attribute_chain(self) -> None:
        """ast.Attribute chain like a.b.c returns dotted name."""
        # Build: a.b.c  →  Attribute(value=Attribute(value=Name('a'), attr='b'), attr='c')
        inner = ast.Attribute(
            value=ast.Name(id="a", ctx=ast.Load()), attr="b", ctx=ast.Load()
        )
        outer = ast.Attribute(value=inner, attr="c", ctx=ast.Load())
        assert _resolve_name(outer) == "a.b.c"

    def test_non_name_non_attribute_returns_none(self) -> None:
        """Non-Name, non-Attribute node returns None."""
        node = ast.Constant(value=42)
        assert _resolve_name(node) is None

    def test_attribute_with_non_name_base(self) -> None:
        """Attribute whose base is NOT ast.Name (e.g. a Call) — while loop
        ends without appending a base id, so only the attr parts are joined."""
        call = ast.Call(
            func=ast.Name(id="func", ctx=ast.Load()),
            args=[],
            keywords=[],
        )
        attr = ast.Attribute(value=call, attr="method", ctx=ast.Load())
        result = _resolve_name(attr)
        # Only "method" collected; base is a Call, not Name, so no base id appended
        assert result == "method"


class TestResolveRelativeValueError:
    """Tests for _resolve_relative() ValueError branch — lines 71-72."""

    def test_path_without_src_returns_module_or_empty(self) -> None:
        """When file_path has no 'src' in its path parts, returns module or ''."""
        # level > 0, no 'src' in path
        assert _resolve_relative(1, None, "/tmp/foo/bar.py") == ""
        assert _resolve_relative(1, "somemod", "/tmp/foo/bar.py") == "somemod"


class TestIsTypeChecking:
    """Tests for _is_type_checking() — lines 89-98."""

    def test_name_type_checking(self) -> None:
        """if TYPE_CHECKING: (ast.Name test) → True."""
        node = ast.If(
            test=ast.Name(id="TYPE_CHECKING", ctx=ast.Load()),
            body=[ast.Pass()],
            orelse=[],
        )
        assert _is_type_checking(node) is True

    def test_attribute_typing_type_checking(self) -> None:
        """if typing.TYPE_CHECKING: (ast.Attribute test) → True."""
        node = ast.If(
            test=ast.Attribute(
                value=ast.Name(id="typing", ctx=ast.Load()),
                attr="TYPE_CHECKING",
                ctx=ast.Load(),
            ),
            body=[ast.Pass()],
            orelse=[],
        )
        assert _is_type_checking(node) is True

    def test_regular_if_is_not_type_checking(self) -> None:
        """Regular if x: → False."""
        node = ast.If(
            test=ast.Name(id="x", ctx=ast.Load()),
            body=[ast.Pass()],
            orelse=[],
        )
        assert _is_type_checking(node) is False

    def test_non_if_node_is_not_type_checking(self) -> None:
        """Non-If node → False."""
        assert _is_type_checking(ast.Pass()) is False


class TestParseFileTypeCheckingElseBranch:
    """Tests for parse_file() handling of TYPE_CHECKING else branches."""

    def test_else_branch_not_marked_type_checking(self, tmp_path: Path) -> None:
        """Imports in the else branch of `if TYPE_CHECKING:` should have
        is_type_checking=False, while the if-body imports should have
        is_type_checking=True."""
        src = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import medre.adapters.matrix.adapter\n"
            "else:\n"
            "    import medre.runtime.builder\n"
        )
        py_file = tmp_path / "test_mod.py"
        py_file.write_text(src, encoding="utf-8")

        edges = parse_file(py_file)

        tc_edge = next(
            (e for e in edges if e.target == "medre.adapters.matrix.adapter"), None
        )
        runtime_edge = next(
            (e for e in edges if e.target == "medre.runtime.builder"), None
        )

        assert tc_edge is not None, "TYPE_CHECKING import not found"
        assert tc_edge.is_type_checking is True

        assert runtime_edge is not None, "else-branch import not found"
        assert runtime_edge.is_type_checking is False


class TestParseFileResolvedBranch:
    """Tests for parse_file() 'if resolved:' branch — line 136."""

    def test_relative_import_adds_parent_edge(self, tmp_path: Path) -> None:
        """from .module import name produces edges for both the specific
        import and the parent module."""
        # Create src/medre/pkg/__init__.py and src/medre/pkg/sub.py
        pkg = tmp_path / "src" / "medre" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        sub = pkg / "sub.py"
        sub.write_text("from .sibling import SomeName\n", encoding="utf-8")

        edges = parse_file(sub)
        targets = [e.target for e in edges]

        # Specific import: medre.pkg.sibling.SomeName
        assert "medre.pkg.sibling.SomeName" in targets
        # Parent module edge: medre.pkg.sibling
        assert "medre.pkg.sibling" in targets

        # Both edges should be import_from kind
        for e in edges:
            assert e.kind == "import_from"


class TestBuildDependencyGraphEdgeCases:
    """Tests for build_dependency_graph() edge cases — lines 183-196."""

    def test_syntax_error_file_skipped_gracefully(self, tmp_path: Path) -> None:
        """A .py file with invalid syntax doesn't crash the graph builder;
        module is present but has empty imports."""
        # Mimic src/medre/ structure
        medre_dir = tmp_path / "src" / "medre"
        medre_dir.mkdir(parents=True)
        (medre_dir / "__init__.py").write_text("", encoding="utf-8")

        bad_file = medre_dir / "broken.py"
        bad_file.write_text("def f(\n", encoding="utf-8")  # invalid syntax

        # Also add a valid file so there's something to compare
        good_file = medre_dir / "good.py"
        good_file.write_text("import os\n", encoding="utf-8")

        graph = build_dependency_graph(medre_dir)

        # broken module should be in the graph
        assert "medre.broken" in graph.modules
        # but with empty imports (SyntaxError was caught)
        assert graph.modules["medre.broken"].imports == []

        # good module should have its imports
        assert len(graph.modules["medre.good"].imports) > 0


class TestBuildRouteAdapterBoundaryReport:
    """Tests for build_route_adapter_boundary_report() returning structured data."""

    @staticmethod
    def _make_graph(*modules: ModuleInfo) -> ArchitectureGraph:
        """Helper to build a graph from given ModuleInfo entries."""
        g = ArchitectureGraph()
        for m in modules:
            g.modules[m.module] = m
        return g

    def test_empty_graph(self) -> None:
        """Empty graph produces report with zero counts."""
        report = build_route_adapter_boundary_report(ArchitectureGraph())
        assert isinstance(report, RouteAdapterBoundaryReport)
        assert report.runtime_to_adapter.count == 0
        assert report.route_engine_forbidden.count == 0
        assert report.adapter_to_runtime.count == 0
        assert report.config_to_adapter_impl.count == 0

    def test_runtime_importing_adapter(self) -> None:
        """Runtime module importing an adapter is reported."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.runtime.engine",
                file="runtime/engine.py",
                imports=[
                    ImportEdge(
                        source="medre.runtime.engine",
                        target="medre.adapters.mesh.send",
                        line=10,
                        kind="import_from",
                        is_type_checking=False,
                    ),
                ],
                layer="runtime",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        assert isinstance(report, RouteAdapterBoundaryReport)
        assert report.runtime_to_adapter.count == 1
        assert report.runtime_to_adapter.violations[0].source == "medre.runtime.engine"
        assert (
            report.runtime_to_adapter.violations[0].target == "medre.adapters.mesh.send"
        )
        assert report.runtime_to_adapter.violations[0].line == 10

    def test_route_engine_forbidden_imports(self) -> None:
        """Route engine with forbidden imports is reported."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.runtime.route_engine",
                file="runtime/route_engine.py",
                imports=[
                    ImportEdge(
                        source="medre.runtime.route_engine",
                        target="medre.adapters",
                        line=5,
                        kind="import",
                        is_type_checking=False,
                    ),
                ],
                layer="runtime",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        assert report.route_engine_forbidden.count == 1
        assert report.route_engine_forbidden.violations[0].target == "medre.adapters"

    def test_adapter_importing_runtime(self) -> None:
        """Adapter module importing runtime is reported."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.adapters.matrix.codec",
                file="adapters/matrix/codec.py",
                imports=[
                    ImportEdge(
                        source="medre.adapters.matrix.codec",
                        target="medre.runtime.builder",
                        line=7,
                        kind="import_from",
                        is_type_checking=False,
                    ),
                ],
                layer="adapters",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        assert report.adapter_to_runtime.count == 1
        assert (
            report.adapter_to_runtime.violations[0].source
            == "medre.adapters.matrix.codec"
        )
        assert report.adapter_to_runtime.violations[0].target == "medre.runtime.builder"

    def test_config_importing_adapter_implementation(self) -> None:
        """Config module importing adapter implementation (not config.adapters) is reported."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.config.model",
                file="config/model.py",
                imports=[
                    ImportEdge(
                        source="medre.config.model",
                        target="medre.adapters.mesh.handler",
                        line=12,
                        kind="import_from",
                        is_type_checking=False,
                    ),
                ],
                layer="config",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        assert report.config_to_adapter_impl.count == 1
        assert (
            report.config_to_adapter_impl.violations[0].target
            == "medre.adapters.mesh.handler"
        )

    def test_type_checking_imports_not_counted(self) -> None:
        """TYPE_CHECKING imports are excluded from boundary reports."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.runtime.engine",
                file="runtime/engine.py",
                imports=[
                    ImportEdge(
                        source="medre.runtime.engine",
                        target="medre.adapters.mesh.send",
                        line=10,
                        kind="import_from",
                        is_type_checking=True,
                    ),
                ],
                layer="runtime",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        assert report.runtime_to_adapter.count == 0

    def test_render_empty_report(self) -> None:
        """render_boundary_report produces expected string format for empty graph."""
        report = build_route_adapter_boundary_report(ArchitectureGraph())
        text = render_boundary_report(report)
        assert "Route/Adapter Boundary Report" in text
        assert "Runtime → Adapter imports: 0" in text
        assert "Route engine forbidden imports: 0" in text
        assert "Adapter → Runtime imports: 0" in text
        assert "Config → Adapter implementation imports: 0" in text

    def test_render_report_with_violations(self) -> None:
        """render_boundary_report includes violation details."""
        graph = self._make_graph(
            ModuleInfo(
                module="medre.runtime.engine",
                file="runtime/engine.py",
                imports=[
                    ImportEdge(
                        source="medre.runtime.engine",
                        target="medre.adapters.mesh.send",
                        line=10,
                        kind="import_from",
                        is_type_checking=False,
                    ),
                ],
                layer="runtime",
            ),
        )
        report = build_route_adapter_boundary_report(graph)
        text = render_boundary_report(report)
        assert "Runtime → Adapter imports: 1" in text
        assert "medre.runtime.engine -> medre.adapters.mesh.send" in text


class TestCheckForbiddenImportsInnerLoop:
    """Tests for check_forbidden_imports() inner loop — lines 338-340."""

    def test_allow_type_checking_false_flags_type_checking_imports(self) -> None:
        """With allow_type_checking=False, TYPE_CHECKING imports are still flagged."""
        graph = ArchitectureGraph()
        graph.modules["medre.core.engine"] = ModuleInfo(
            module="medre.core.engine",
            file="core/engine.py",
            imports=[
                ImportEdge(
                    source="medre.core.engine",
                    target="medre.adapters.mesh",
                    line=5,
                    kind="import_from",
                    is_type_checking=True,
                ),
            ],
            layer="core",
        )
        violations = check_forbidden_imports(
            graph,
            "medre.core",
            ("medre.adapters",),
            allow_type_checking=False,
        )
        assert len(violations) == 1
        assert violations[0] == ("medre.core.engine", "medre.adapters.mesh", 5)

    def test_exact_match_not_just_prefix(self) -> None:
        """Exact match (target == forbidden) works, not just prefix match."""
        graph = ArchitectureGraph()
        graph.modules["medre.core.engine"] = ModuleInfo(
            module="medre.core.engine",
            file="core/engine.py",
            imports=[
                ImportEdge(
                    source="medre.core.engine",
                    target="medre.runtime",
                    line=3,
                    kind="import",
                    is_type_checking=False,
                ),
            ],
            layer="core",
        )
        violations = check_forbidden_imports(
            graph,
            "medre.core",
            ("medre.runtime",),
        )
        assert len(violations) == 1
        assert violations[0][1] == "medre.runtime"


class TestRuntimeBuilderOnlyAssembly:
    """Only medre.runtime.builder may import from medre.adapters.*."""

    _ALLOWED_RUNTIME_ADAPTER_IMPORTS = frozenset(
        {
            "medre.runtime.builder",
        }
    )

    def test_only_builder_imports_adapters(self) -> None:
        """No runtime module except builder imports adapter implementations."""
        graph = build_dependency_graph(_SRC)

        violations = []
        for mod, info in graph.modules.items():
            if not mod.startswith("medre.runtime."):
                continue
            if mod in self._ALLOWED_RUNTIME_ADAPTER_IMPORTS:
                continue
            for edge in info.imports:
                if edge.is_type_checking:
                    continue
                if edge.target.startswith("medre.adapters."):
                    violations.append((mod, edge.target, edge.line))

        assert (
            not violations
        ), "Runtime modules importing adapters (only builder allowed):\n" + "\n".join(
            f"  {m} -> {t} (line {ln})" for m, t, ln in violations
        )

    def test_builder_actually_imports_adapters(self) -> None:
        """Sanity check: builder.py does import from adapters (may be deferred)."""
        builder_file = _SRC / "runtime" / "builder.py"
        assert builder_file.is_file(), "runtime/builder.py not found"

        source = builder_file.read_text(encoding="utf-8")
        import re

        adapter_refs = re.findall(r"\bfrom medre\.adapters\b", source)
        assert len(adapter_refs) > 0, (
            "medre.runtime.builder has no adapter imports — "
            "if this is intentional, update the test"
        )

    def test_route_engine_no_adapter_imports(self) -> None:
        """Route engine must not import adapter implementations."""
        graph = build_dependency_graph(_SRC)

        route_engine = graph.modules.get("medre.runtime.route_engine")
        if route_engine is None:
            pytest.skip("route_engine module not found")

        for edge in route_engine.imports:
            if edge.is_type_checking:
                continue
            for f in _ROUTE_ENGINE_FORBIDDEN:
                if edge.target == f or edge.target.startswith(f + "."):
                    pytest.fail(
                        f"route_engine imports forbidden: {edge.target} (line {edge.line})"
                    )
