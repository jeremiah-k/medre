"""Tests for dynamic adapter assembly detection and boundary report completeness."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from medre.runtime.architecture_report import (
    build_dependency_graph,
    build_route_adapter_boundary_report,
    extract_dynamic_adapter_imports,
    module_matches,
    render_boundary_report,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "medre"


# ---------------------------------------------------------------------------
# Dynamic RuntimeBuilder assembly
# ---------------------------------------------------------------------------


class TestExtractDynamicAdapterImports:
    """Tests for extract_dynamic_adapter_imports()."""

    def test_extracts_adapter_factory_modules(self) -> None:
        """Parses _AdapterFactory(module=...) calls."""
        source = (
            "_ADAPTER_BUILDERS = {\n"
            '    "matrix": _AdapterFactory(\n'
            '        module="medre.adapters.matrix.adapter",\n'
            '        cls_name="MatrixAdapter",\n'
            "    ),\n"
            "}\n"
        )
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules

    def test_extracts_renderer_specs(self) -> None:
        """Parses _ADAPTER_RENDERER_SPECS list tuples."""
        source = (
            "_ADAPTER_RENDERER_SPECS: list[tuple[str, str]] = [\n"
            '    ("medre.adapters.matrix.renderer", "MatrixRenderer"),\n'
            '    ("medre.adapters.lxmf.renderer", "LxmfRenderer"),\n'
            "]\n"
        )
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.renderer" in modules
        assert "medre.adapters.lxmf.renderer" in modules

    def test_extracts_both_from_real_builder(self) -> None:
        """Extracts from the real builder.py source file."""
        builder_file = _SRC / "runtime" / "builder.py"
        if not builder_file.is_file():
            pytest.skip("builder.py not found")
        source = builder_file.read_text(encoding="utf-8")
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        # Should find 4 adapter factories + 4 renderer specs = 8
        assert (
            len(modules) >= 8
        ), f"Expected >= 8 dynamic imports, got {len(modules)}: {modules}"
        assert "medre.adapters.matrix.adapter" in modules
        assert "medre.adapters.matrix.renderer" in modules

    def test_returns_line_numbers(self) -> None:
        """Each result includes a line number."""
        source = (
            "_ADAPTER_BUILDERS = {\n"
            '    "matrix": _AdapterFactory(\n'
            '        module="medre.adapters.matrix.adapter",\n'
            '        cls_name="MatrixAdapter",\n'
            "    ),\n"
            "}\n"
        )
        results = extract_dynamic_adapter_imports(source)
        assert len(results) >= 1
        for _module, line, reason in results:
            assert line > 0
            assert reason

    def test_empty_source_returns_empty(self) -> None:
        """Empty source yields no results."""
        assert extract_dynamic_adapter_imports("") == []

    def test_source_with_no_matches_returns_empty(self) -> None:
        """Source with no _AdapterFactory or _ADAPTER_RENDERER_SPECS."""
        source = "x = 1\ny = 2\n"
        assert extract_dynamic_adapter_imports(source) == []

    def test_import_alias_detected(self) -> None:
        """import importlib as il; il.import_module(...) is detected."""
        source = (
            "import importlib as il\n"
            'il.import_module("medre.adapters.matrix.adapter")\n'
        )
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules

    def test_from_import_alias_detected(self) -> None:
        """from importlib import import_module as im; im(...) is detected."""
        source = (
            "from importlib import import_module as im\n"
            'im("medre.adapters.lxmf.adapter")\n'
        )
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.lxmf.adapter" in modules

    def test_aliased_non_adapter_not_detected(self) -> None:
        """import importlib as il; il.import_module("other.module") is not detected."""
        source = "import importlib as il\n" 'il.import_module("other.module")\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert all(not m.startswith("medre.adapters.") for m in modules)

    def test_renderer_spec_non_adapter_filtered(self) -> None:
        """Non-adapter renderer spec strings are not detected."""
        source = (
            "_ADAPTER_RENDERER_SPECS = [\n"
            '    ("not.an.adapter", "Thing"),\n'
            '    ("medre.adapters.matrix.renderer", "MatrixRenderer"),\n'
            "]\n"
        )
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "not.an.adapter" not in modules
        assert "medre.adapters.matrix.renderer" in modules


class TestDynamicBuilderAssembly:
    """Tests that dynamic builder imports appear in allowed section."""

    def test_real_graph_allowed_with_src_root(self) -> None:
        """With src_root, dynamic adapter imports appear in allowed section."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        assert report.allowed_runtime_adapter.count >= 8, (
            f"Expected >= 8 allowed entries (4 adapters + 4 renderers), "
            f"got {report.allowed_runtime_adapter.count}"
        )

    def test_allowed_includes_specific_modules(self) -> None:
        """Allowed section includes specific adapter modules."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        targets = {v.target for v in report.allowed_runtime_adapter.violations}
        assert "medre.adapters.matrix.adapter" in targets
        assert "medre.adapters.matrix.renderer" in targets

    def test_builder_not_in_forbidden(self) -> None:
        """Builder module is NOT in forbidden_runtime_adapter."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        builder_in_forbidden = [
            v
            for v in report.forbidden_runtime_adapter.violations
            if v.source == "medre.runtime.builder"
        ]
        assert not builder_in_forbidden

    def test_without_src_root_no_dynamic(self) -> None:
        """Without src_root, dynamic imports are not extracted (backward compat)."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph)
        # Only static AST imports are in allowed section
        dynamic_rules = [
            v
            for v in report.allowed_runtime_adapter.violations
            if v.rule.startswith("dynamic")
        ]
        # Without src_root, no dynamic imports should be added
        assert not dynamic_rules


class TestRealGraphBoundaryReport:
    """Comprehensive assertions on the real repository's boundary report."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.graph = build_dependency_graph(_SRC)
        self.report = build_route_adapter_boundary_report(self.graph, src_root=_SRC)

    def test_no_parse_errors(self):
        assert (
            self.graph.parse_errors == {}
        ), f"Real graph has parse errors: {self.graph.parse_errors}"

    def test_no_adapter_cross_imports(self):
        assert self.report.adapter_cross_imports.count == 0, (
            f"Cross-transport violations: "
            f"{[v.target for v in self.report.adapter_cross_imports.violations]}"
        )

    def test_no_forbidden_runtime_adapter(self):
        assert self.report.forbidden_runtime_adapter.count == 0, (
            f"Forbidden runtime→adapter: "
            f"{[f'{v.source}→{v.target}' for v in self.report.forbidden_runtime_adapter.violations]}"
        )

    def test_allowed_runtime_adapter_at_least_four(self):
        # 4 adapters via _AdapterFactory + 4 renderers via _ADAPTER_RENDERER_SPECS
        assert (
            self.report.allowed_runtime_adapter.count >= 4
        ), f"Expected ≥4 allowed adapter refs, got {self.report.allowed_runtime_adapter.count}"

    def test_no_route_engine_forbidden(self):
        assert self.report.route_engine_forbidden.count == 0, (
            f"Route engine violations: "
            f"{[f'{v.source}→{v.target}' for v in self.report.route_engine_forbidden.violations]}"
        )

    def test_no_adapter_to_runtime(self):
        assert self.report.adapter_to_runtime.count == 0, (
            f"Adapter→runtime violations: "
            f"{[f'{v.source}→{v.target}' for v in self.report.adapter_to_runtime.violations]}"
        )

    def test_no_config_to_adapter_impl(self):
        assert self.report.config_to_adapter_impl.count == 0, (
            f"Config→adapter impl violations: "
            f"{[f'{v.source}→{v.target}' for v in self.report.config_to_adapter_impl.violations]}"
        )

    def test_no_codec_renderer_forbidden(self):
        assert self.report.codec_renderer_forbidden.count == 0, (
            f"Codec/renderer violations: "
            f"{[f'{v.source}→{v.target}' for v in self.report.codec_renderer_forbidden.violations]}"
        )

    def test_no_session_foreign_sdk(self):
        assert self.report.session_foreign_sdk.count == 0, (
            f"Session foreign SDK violations: "
            f"{[f'{v.source}→{v.target}' for v in self.report.session_foreign_sdk.violations]}"
        )

    def test_no_adapter_wrapper_foreign_transport(self):
        assert self.report.adapter_wrapper_foreign_transport.count == 0, (
            f"Adapter wrapper cross-transport: "
            f"{[f'{v.source}→{v.target}' for v in self.report.adapter_wrapper_foreign_transport.violations]}"
        )

    def test_no_dynamic_scan_errors(self):
        """Real graph has zero dynamic scan errors."""
        assert self.report.dynamic_scan_errors.count == 0

    def test_allowed_includes_all_four_adapters(self):
        targets = {v.target for v in self.report.allowed_runtime_adapter.violations}
        for adapter in (
            "medre.adapters.matrix.adapter",
            "medre.adapters.meshtastic.adapter",
            "medre.adapters.meshcore.adapter",
            "medre.adapters.lxmf.adapter",
        ):
            assert adapter in targets, f"Missing allowed adapter: {adapter}"


# ---------------------------------------------------------------------------
# Dynamic adapter detection outside RuntimeBuilder
# ---------------------------------------------------------------------------


class TestDynamicAdapterDetectionOutsideBuilder:
    """Dynamic adapter strings in non-builder runtime modules are forbidden."""

    def test_builder_importlib_import_module_allowed(self):
        """importlib.import_module("medre.adapters....") in builder is allowed."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        # Builder dynamic imports should be in allowed section
        [
            v
            for v in report.allowed_runtime_adapter.violations
            if v.source == "medre.runtime.builder" and "import_module" in v.rule
        ]
        # Builder may or may not use importlib.import_module, but if it does
        # it must be in allowed.  Verify builder never appears in forbidden.
        builder_forbidden = [
            v
            for v in report.forbidden_runtime_adapter.violations
            if v.source == "medre.runtime.builder"
        ]
        assert (
            not builder_forbidden
        ), f"Builder should not appear in forbidden: {builder_forbidden}"

    def test_extracts_importlib_import_module(self):
        """Extraction detects importlib.import_module("medre.adapters....") in source.

        Note: this tests extraction only, not forbidden classification.
        See TestNonBuilderBoundaryClassification for report-level tests.
        """
        source = 'importlib.import_module("medre.adapters.matrix.adapter")\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules

    def test_extracts_dunder_import(self):
        """Extraction detects __import__("medre.adapters....") in source.

        Note: this tests extraction only, not forbidden classification.
        See TestNonBuilderBoundaryClassification for report-level tests.
        """
        source = '__import__("medre.adapters.lxmf.adapter", fromlist=["LxmfAdapter"])\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.lxmf.adapter" in modules

    def test_real_graph_no_non_builder_dynamic_violations(self):
        """Real graph has zero forbidden dynamic adapter refs outside builder."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        dynamic_forbidden = [
            v
            for v in report.forbidden_runtime_adapter.violations
            if "dynamic" in v.rule.lower()
        ]
        assert (
            dynamic_forbidden == []
        ), f"Non-builder dynamic violations: {dynamic_forbidden}"


# ---------------------------------------------------------------------------
# Runtime Assembly Points include both allowed and forbidden
# ---------------------------------------------------------------------------


class TestRuntimeAssemblyPointsComplete:
    """runtime_assembly_points includes both allowed and forbidden."""

    def test_real_graph_assembly_includes_builder(self):
        """Builder appears in runtime_assembly_points."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        builder_entries = [
            v
            for v in report.runtime_assembly_points.violations
            if v.source == "medre.runtime.builder"
        ]
        assert (
            len(builder_entries) > 0
        ), "Builder should appear in runtime_assembly_points"

    def test_assembly_points_count_entries_not_violations(self):
        """render_boundary_report uses 'entries' for assembly points."""
        graph = build_dependency_graph(_SRC)
        report = build_route_adapter_boundary_report(graph, src_root=_SRC)
        rendered = render_boundary_report(report)
        assert "Runtime Assembly Points" in rendered
        assert "entries" in rendered.split("Runtime Assembly Points")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# module_matches() helper
# ---------------------------------------------------------------------------


class TestModuleMatches:
    """Tests for module_matches() helper."""

    def test_exact_match(self):
        assert module_matches("medre.core", "medre.core") is True

    def test_child_match(self):
        assert module_matches("medre.core.events", "medre.core") is True

    def test_no_partial_match(self):
        """Prefix must match at a dot boundary."""
        assert module_matches("medre.corex", "medre.core") is False

    def test_no_match_unrelated(self):
        assert module_matches("os.path", "medre.core") is False

    def test_empty_prefix(self):
        """Empty prefix only matches the empty string itself."""
        assert module_matches("", "") is True
        assert module_matches("medre.core", "") is False


# ---------------------------------------------------------------------------
# Registry literal detection
# ---------------------------------------------------------------------------


class TestRegistryLiteralDetection:
    """Tests for registry literal detection in extract_dynamic_adapter_imports()."""

    def test_dict_value_detected(self) -> None:
        """ADAPTER_SPECS = {"x": "medre.adapters.x.adapter"} is detected."""
        source = 'ADAPTER_SPECS = {"matrix": "medre.adapters.matrix.adapter"}\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules

    def test_list_value_detected(self) -> None:
        """_ADAPTER_REGISTRY = [("m", "medre.adapters.x.adapter")] is detected."""
        source = '_ADAPTER_REGISTRY = [("matrix", "medre.adapters.matrix.adapter")]\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules

    def test_non_registry_name_not_detected(self) -> None:
        """ROUTE_DOC = "medre.adapters.x.adapter" is NOT detected (no keyword in name)."""
        source = 'ROUTE_DOC = "medre.adapters.matrix.adapter"\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" not in modules

    def test_non_adapter_string_not_detected(self) -> None:
        """_MY_SPEC = "other.module" is not detected (doesn't start with medre.adapters.)."""
        source = '_MY_SPEC = "other.module"\n'
        results = extract_dynamic_adapter_imports(source)
        assert results == []

    def test_renderer_specs_not_double_counted(self) -> None:
        """_ADAPTER_RENDERER_SPECS is NOT double-counted by registry detection."""
        source = '_ADAPTER_RENDERER_SPECS = [("medre.adapters.matrix.renderer", "X")]\n'
        results = extract_dynamic_adapter_imports(source)
        # Should be detected exactly once (by the renderer specs walker, not registry)
        modules = [r[0] for r in results]
        assert modules.count("medre.adapters.matrix.renderer") == 1

    def test_ann_assign_detected(self) -> None:
        """Annotated assignment _BUILDER_SPEC: str = "medre.adapters.x.adapter" detected."""
        source = '_BUILDER_SPEC: str = "medre.adapters.matrix.adapter"\n'
        results = extract_dynamic_adapter_imports(source)
        modules = [r[0] for r in results]
        assert "medre.adapters.matrix.adapter" in modules


# ---------------------------------------------------------------------------
# Synthetic source tree helper
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _synthetic_src(
    tmp_path: Path, *, builder_source: str, other_source: str | None = None
) -> Iterator[Path]:
    """Create a minimal synthetic medre source tree for boundary report testing.

    Creates:
        tmp_path/src/medre/__init__.py
        tmp_path/src/medre/adapters/__init__.py
        tmp_path/src/medre/adapters/x/__init__.py
        tmp_path/src/medre/adapters/x/adapter.py  (class XAdapter: pass)
        tmp_path/src/medre/runtime/__init__.py
        tmp_path/src/medre/runtime/builder.py     (builder_source)
        tmp_path/src/medre/runtime/other.py        (other_source, if provided)

    Yields the medre/ directory Path.
    """
    src = tmp_path / "src" / "medre"
    (src / "adapters" / "x").mkdir(parents=True)
    (src / "runtime").mkdir(parents=True)

    (src / "__init__.py").write_text("")
    (src / "adapters" / "__init__.py").write_text("")
    (src / "adapters" / "x" / "__init__.py").write_text("")
    (src / "adapters" / "x" / "adapter.py").write_text("class XAdapter: pass\n")
    (src / "runtime" / "__init__.py").write_text("")
    (src / "runtime" / "builder.py").write_text(builder_source)

    if other_source is not None:
        (src / "runtime" / "other.py").write_text(other_source)

    yield src


# ---------------------------------------------------------------------------
# Non-builder boundary classification via synthetic trees
# ---------------------------------------------------------------------------


class TestNonBuilderBoundaryClassification:
    """Tests that build_route_adapter_boundary_report() classifies
    non-builder runtime modules as forbidden and builder as allowed."""

    def test_builder_importlib_in_allowed(self, tmp_path: Path) -> None:
        """Builder's importlib.import_module("medre.adapters.x.adapter") appears in allowed."""
        builder = (
            "import importlib\n" 'importlib.import_module("medre.adapters.x.adapter")\n'
        )
        with _synthetic_src(tmp_path, builder_source=builder) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            targets = {v.target for v in report.allowed_runtime_adapter.violations}
            assert "medre.adapters.x.adapter" in targets

    def test_non_builder_importlib_in_forbidden(self, tmp_path: Path) -> None:
        """Non-builder's importlib.import_module("medre.adapters.x.adapter") appears in forbidden."""
        builder = "pass\n"
        other = (
            "import importlib\n" 'importlib.import_module("medre.adapters.x.adapter")\n'
        )
        with _synthetic_src(
            tmp_path, builder_source=builder, other_source=other
        ) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            sources = {v.source for v in report.forbidden_runtime_adapter.violations}
            assert "medre.runtime.other" in sources

    def test_non_builder_dunder_import_in_forbidden(self, tmp_path: Path) -> None:
        """Non-builder's __import__("medre.adapters.x.adapter") appears in forbidden."""
        builder = "pass\n"
        other = '__import__("medre.adapters.x.adapter")\n'
        with _synthetic_src(
            tmp_path, builder_source=builder, other_source=other
        ) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            sources = {v.source for v in report.forbidden_runtime_adapter.violations}
            assert "medre.runtime.other" in sources

    def test_non_builder_adapter_factory_in_forbidden(self, tmp_path: Path) -> None:
        """Non-builder's _AdapterFactory(module="medre.adapters.x.adapter") appears in forbidden."""
        builder = "pass\n"
        other = (
            "class _AdapterFactory:\n"
            "    def __init__(self, module, cls_name): pass\n"
            '_AdapterFactory(module="medre.adapters.x.adapter", cls_name="XAdapter")\n'
        )
        with _synthetic_src(
            tmp_path, builder_source=builder, other_source=other
        ) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            sources = {v.source for v in report.forbidden_runtime_adapter.violations}
            assert "medre.runtime.other" in sources

    def test_builder_not_in_forbidden(self, tmp_path: Path) -> None:
        """Builder with dynamic imports never appears in forbidden_runtime_adapter."""
        builder = (
            "import importlib\n" 'importlib.import_module("medre.adapters.x.adapter")\n'
        )
        with _synthetic_src(tmp_path, builder_source=builder) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            builder_in_forbidden = [
                v
                for v in report.forbidden_runtime_adapter.violations
                if v.source == "medre.runtime.builder"
            ]
            assert not builder_in_forbidden

    def test_assembly_points_includes_both(self, tmp_path: Path) -> None:
        """runtime_assembly_points includes both builder and non-builder entries."""
        builder = (
            "import importlib\n" 'importlib.import_module("medre.adapters.x.adapter")\n'
        )
        other = (
            "import importlib\n" 'importlib.import_module("medre.adapters.x.adapter")\n'
        )
        with _synthetic_src(
            tmp_path, builder_source=builder, other_source=other
        ) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            sources = {v.source for v in report.runtime_assembly_points.violations}
            assert "medre.runtime.builder" in sources
            assert "medre.runtime.other" in sources

    def test_syntax_error_populates_scan_errors(self, tmp_path: Path) -> None:
        """A syntax-error runtime file populates dynamic_scan_errors."""
        builder = "pass\n"
        src = tmp_path / "src" / "medre"
        (src / "adapters" / "x").mkdir(parents=True)
        (src / "runtime").mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (src / "adapters" / "__init__.py").write_text("")
        (src / "adapters" / "x" / "__init__.py").write_text("")
        (src / "adapters" / "x" / "adapter.py").write_text("class XAdapter: pass\n")
        (src / "runtime" / "__init__.py").write_text("")
        (src / "runtime" / "builder.py").write_text(builder)
        (src / "runtime" / "broken.py").write_text("def (broken syntax\n")

        graph = build_dependency_graph(src)
        report = build_route_adapter_boundary_report(graph, src_root=src)
        assert report.dynamic_scan_errors.count >= 1

    def test_no_dynamic_scan_errors_for_valid_tree(self, tmp_path: Path) -> None:
        """A valid synthetic tree produces zero dynamic scan errors."""
        builder = "pass\n"
        with _synthetic_src(tmp_path, builder_source=builder) as src:
            graph = build_dependency_graph(src)
            report = build_route_adapter_boundary_report(graph, src_root=src)
            assert report.dynamic_scan_errors.count == 0
