"""Synthetic lifecycle reports distinguish adapter instances from transports."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.runtime import drill

_PROGRAMMATIC_DRILLS = (
    "bad_route_config",
    "all_adapters_build_fail",
    "partial_degraded_startup",
    "all_adapters_start_fail",
)


@pytest.fixture(
    params=tuple(
        spec
        for spec in adapter_registry.iter_adapter_specs()
        if spec.fake_adapter is not None
    )
    or (None,),
    ids=lambda spec: spec.transport if spec else "no_provider",
)
def single_provider(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str:
    spec = request.param
    if spec is None:
        pytest.skip("requires an installed fake provider")
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    return spec.transport


def _validate_report(report: dict) -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/lifecycle-drill-report.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)


@pytest.mark.parametrize("name", _PROGRAMMATIC_DRILLS)
async def test_single_provider_reports_actual_instance_assignments(
    name: str, single_provider: str
) -> None:
    report = await drill.run_drill(name)
    assert report["status"] == "passed", report.get("fail_reasons")
    assert set(report["adapter_transports"].values()) == {single_provider}
    assert report["coverage_scope"] == "synthetic_adapter_instances"
    assert any("no cross-transport coverage" in item for item in report["limitations"])
    if name == "bad_route_config":
        assert report["known_adapter_ids"] == ["source"]
        assert report["adapter_transports"] == {"source": single_provider}
    _validate_report(report)


async def test_one_provider_still_builds_three_distinct_startup_instances(
    single_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = []
    original = drill.RuntimeBuilder.build

    def record_build(builder):
        app = original(builder)
        built.append(dict(app.adapters))
        return app

    monkeypatch.setattr(drill.RuntimeBuilder, "build", record_build)
    report = await drill.run_drill("partial_degraded_startup")
    assert report["status"] == "passed", report.get("fail_reasons")
    assert len(built) == 1
    assert set(built[0]) == {"alpha", "beta", "gamma"}
    assert len({id(instance) for instance in built[0].values()}) == 3
    assert report["adapter_transports"] == dict.fromkeys(built[0], single_provider)
    assert report["started_adapters"] == ["alpha", "gamma"]
    assert report["failed_adapters"] == ["beta"]
    assert report["boot_summary"]["adapters_total"] == 3


@pytest.mark.parametrize("name", _PROGRAMMATIC_DRILLS)
async def test_no_provider_returns_failed_report(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(()))
    report = await drill.run_drill(name)
    assert report["status"] == "failed"
    assert report["adapter_transports"] == {}
    assert "require an installed adapter" in report["fail_reasons"][0]
    _validate_report(report)


def test_explicit_assignments_survive_provider_order() -> None:
    specs = adapter_registry.iter_adapter_specs()
    if len(specs) < 2:
        pytest.skip("requires two providers to test mixed explicit assignments")
    expected = {"alpha": specs[1].transport, "beta": specs[0].transport}
    configs = drill._drill_adapters(expected)
    assert {
        adapter_id: transport for transport, adapter_id, _ in configs.all_configs()
    } == expected


def test_missing_provider_cli_returns_json_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from medre.cli.main import main

    monkeypatch.setattr(adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(()))
    with pytest.raises(SystemExit) as exc_info:
        main(["smoke", "--drill", "partial_degraded_startup", "--json"])
    assert exc_info.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    _validate_report(report)


@pytest.mark.parametrize("name", _PROGRAMMATIC_DRILLS)
async def test_provider_without_fake_returns_failed_report(name, monkeypatch):
    from tests.helpers.adapter_spec import make_adapter_spec

    no_fake = replace(make_adapter_spec("aa_no_fake"), fake_adapter=None)
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((no_fake,))
    )
    report = await drill.run_drill(name)
    assert report["status"] == "failed"
    assert report["adapter_transports"] == {}
    assert "fake implementation" in report["fail_reasons"][0]
    _validate_report(report)


def test_provider_selection_skips_optional_fake(monkeypatch):
    from tests.helpers.adapter_spec import make_adapter_spec

    specs = (
        replace(make_adapter_spec("aa_no_fake"), fake_adapter=None),
        make_adapter_spec("z_fake"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(specs)
    )
    assert drill._drill_transport() == "z_fake"


@pytest.mark.parametrize(
    "name", ["partial_degraded_startup", "all_adapters_start_fail"]
)
async def test_broken_fake_cannot_masquerade_as_forced_start_failure(name, monkeypatch):
    from medre.adapter_registry import SymbolRef
    from tests.helpers.adapter_spec import make_adapter_spec

    spec = replace(
        make_adapter_spec("broken_fake"), fake_adapter=SymbolRef("builtins", "object")
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    report = await drill.run_drill(name)
    assert report["status"] == "failed"
    assert "before failure injection" in report["fail_reasons"][0]


def test_report_schema_rejects_pass_without_provider_evidence():
    from jsonschema import ValidationError

    with pytest.raises(ValidationError, match="non-empty"):
        _validate_report(drill._base_report("bad_route_config", adapter_transports={}))


async def test_start_failure_requires_adapter_cleanup_evidence(
    single_provider, monkeypatch
):
    async def unobserved_stop(self):
        return None

    monkeypatch.setattr(drill._DrillFailingAdapter, "stop", unobserved_stop)
    report = await drill.run_drill("all_adapters_start_fail")
    assert report["status"] == "failed"
    assert "Failed-adapter cleanup was not observed" in report["fail_reasons"]
