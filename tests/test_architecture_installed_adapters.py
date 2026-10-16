"""Real source inventories prevent empty adapter boundary proofs."""

from pathlib import Path

from medre.adapter_registry import iter_adapter_specs
from medre.runtime.architecture_report import (
    _transport_for,
    build_dependency_graph,
    build_route_adapter_boundary_report,
)


def test_shared_fake_modules_have_no_phantom_transport():
    assert _transport_for("medre.adapters.fakes.transport") is None
    assert _transport_for("medre.adapters.fakes.presentation") is None


def test_installed_namespace_sources_are_part_of_boundary_graph():
    source = Path(__file__).resolve().parents[1] / "src/medre"
    graph = build_dependency_graph(source, include_installed_adapters=True)
    assert "medre.adapters.fakes.transport" in graph.modules
    assert not graph.parse_errors
    for spec in iter_adapter_specs():
        assert spec.adapter.module in graph.modules
    report = build_route_adapter_boundary_report(graph, src_root=source)
    assert not report.adapter_cross_imports.violations
    assert not report.route_engine_forbidden.violations
