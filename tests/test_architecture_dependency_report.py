"""Tests for the architecture dependency graph and boundary reports."""
from __future__ import annotations

from pathlib import Path

import pytest

from medre.runtime.architecture_report import (
    _CORE_FORBIDDEN,
    _CONFIG_FORBIDDEN,
    _ROUTE_ENGINE_FORBIDDEN,
    ArchitectureGraph,
    ModuleInfo,
    build_dependency_graph,
    check_forbidden_imports,
    module_path_for,
    parse_file,
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
        assert not violations, (
            "Core modules have forbidden imports:\n" +
            "\n".join(f"  {m}: {t} (line {l})" for m, t, l in violations)
        )


class TestConfigBoundary:
    """Config modules must not import adapters or SDKs."""

    def test_config_no_forbidden_imports(self) -> None:
        graph = build_dependency_graph(_SRC)
        violations = check_forbidden_imports(graph, "medre.config", _CONFIG_FORBIDDEN)
        assert not violations, (
            "Config modules have forbidden imports:\n" +
            "\n".join(f"  {m}: {t} (line {l})" for m, t, l in violations)
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
            m for m in graph.modules
            if any(m.endswith(s) for s in [".codec", ".renderer"])
            and m.startswith("medre.adapters.")
        ]
        violations: list[tuple[str, str, int]] = []
        for mod in codec_renderer_modules:
            for module, target, line in check_forbidden_imports(
                graph, mod, self._ADAPTER_FORBIDDEN
            ):
                violations.append((module, target, line))
        assert not violations, (
            "Codec/renderer modules have forbidden imports:\n" +
            "\n".join(f"  {m}: {t} (line {l})" for m, t, l in violations)
        )


class TestReportDeterminism:
    """Reports must be deterministic."""

    def test_render_is_deterministic(self) -> None:
        graph1 = build_dependency_graph(_SRC)
        graph2 = build_dependency_graph(_SRC)
        report1 = render_dependency_report(graph1)
        report2 = render_dependency_report(graph2)
        assert report1 == report2
