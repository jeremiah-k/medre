"""Failure drills require fake execution, complete evidence, and cleanup."""

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import SymbolRef
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.config import loader
from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime import drill
from medre.runtime.app import MedreApp, RuntimeState
from tests.helpers.adapter_spec import make_adapter_spec

_CONFIGURED = drill.AVAILABLE_DRILLS[:7]
_SYNTHETIC = drill.AVAILABLE_DRILLS[7:]


@dataclass(frozen=True)
class ProbeConfig:
    adapter_id: str

    def validate(self):
        return self


def read_native(*args, **kwargs):
    return {}


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    spec = replace(
        make_adapter_spec("private_chat"),
        config=SymbolRef(__name__, "ProbeConfig"),
        fake_adapter=SymbolRef(
            "medre.adapters.fakes.transport", "FakeTransportAdapter"
        ),
        native_namespace_reader=SymbolRef(__name__, "read_native"),
        versioned_namespace_reader=SymbolRef(__name__, "read_native"),
        attribution_projector=SymbolRef(__name__, "read_native"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    monkeypatch.setattr(loader, "_KNOWN_TRANSPORTS", frozenset({spec.transport}))
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "storage:\n  backend: memory\n"
        "adapters:\n  private_chat:\n"
        "    source:\n      adapter_kind: fake\n"
        "    target:\n      adapter_kind: fake\n"
        "routes:\n  forward:\n    source_adapters: [source]\n"
        "    dest_adapters: [target]\n    directionality: source_to_dest\n"
    )
    return str(path)


@pytest.mark.parametrize("name", _CONFIGURED)
async def test_real_adapter_is_rejected_before_drill_build(
    name, config_path, monkeypatch
):
    path = Path(config_path)
    path.write_text(
        path.read_text().replace("adapter_kind: fake", "adapter_kind: real")
    )
    build = Mock(side_effect=AssertionError("drill must not build real adapters"))
    monkeypatch.setattr(drill.RuntimeBuilder, "build", build)
    report = await drill.run_drill(name, config_path=config_path)
    assert report["status"] == "failed"
    assert "requires adapter_kind: fake" in report["fail_reasons"][0]
    build.assert_not_called()


@pytest.mark.parametrize("name", (*_CONFIGURED, "partial_degraded_startup"))
async def test_shutdown_error_cannot_pass(name, config_path, monkeypatch):
    original = MedreApp.stop

    async def fail_after_stop(self):
        await original(self)
        raise RuntimeError("shutdown integrity fault")

    monkeypatch.setattr(MedreApp, "stop", fail_after_stop)
    report = await drill.run_drill(name, config_path=config_path)
    assert report["status"] == "failed", report
    assert any(
        "shutdown integrity fault" in reason for reason in report["fail_reasons"]
    )


@pytest.mark.parametrize("name", ["capacity_rejection", "shutdown_rejection"])
async def test_missing_suppression_receipts_cannot_pass(name, config_path, monkeypatch):
    async def missing(*args, **kwargs):
        return []

    monkeypatch.setattr(SQLiteStorage, "list_receipts_for_event", missing)
    report = await drill.run_drill(name, config_path=config_path)
    assert report["status"] == "failed", report
    assert any("receipt" in reason for reason in report["fail_reasons"])


@pytest.mark.parametrize(
    "name", ["adapter_permanent_failure", "adapter_transient_failure"]
)
async def test_unreadable_native_ref_evidence_cannot_pass(
    name, config_path, monkeypatch
):
    async def fail(*args, **kwargs):
        raise RuntimeError("native evidence unavailable")

    monkeypatch.setattr(SQLiteStorage, "list_native_refs_for_event", fail)
    report = await drill.run_drill(name, config_path=config_path)
    assert report["status"] == "failed", report


async def test_unsuccessful_manual_recovery_cannot_pass(config_path, monkeypatch):
    async def fail(self, result):
        raise AdapterPermanentError("recovery integrity fault")

    monkeypatch.setattr(FakeTransportAdapter, "deliver", fail)
    report = await drill.run_drill("adapter_transient_failure", config_path=config_path)
    assert report["status"] == "failed", report
    assert any(
        "simulate_manual_recovery" in reason for reason in report["fail_reasons"]
    )


async def test_partial_startup_requires_failed_adapter_cleanup(monkeypatch):
    async def unobserved_stop(self, timeout=5.0):
        return None

    monkeypatch.setattr(drill._DrillFailingAdapter, "stop", unobserved_stop)
    report = await drill.run_drill("partial_degraded_startup")
    assert report["status"] == "failed", report
    assert any(
        "verify_failed_adapter_cleanup" in reason for reason in report["fail_reasons"]
    )


@pytest.mark.parametrize("name", _SYNTHETIC)
async def test_synthetic_paths_preserve_environment_and_remove_temp_root(
    name, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", "/operator/state")
    names = (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    )
    before = {name: os.environ.get(name) for name in names}
    roots = []
    original = drill.RuntimeBuilder.build

    def record(builder):
        roots.append(builder._paths.state_dir.parent)
        return original(builder)

    monkeypatch.setattr(drill.RuntimeBuilder, "build", record)
    report = await drill.run_drill(name)
    assert report["status"] == "passed", report
    assert {name: os.environ.get(name) for name in names} == before
    assert roots and all(not root.exists() for root in roots)


@pytest.mark.parametrize("name", ["renderer_failure", "partial_degraded_startup"])
async def test_startup_cancellation_still_closes_runtime(
    name, config_path, monkeypatch
):
    apps = []
    original = MedreApp.start

    async def cancel_after_start(self):
        await original(self)
        apps.append(self)
        raise asyncio.CancelledError

    monkeypatch.setattr(MedreApp, "start", cancel_after_start)
    with pytest.raises(asyncio.CancelledError):
        await drill.run_drill(name, config_path=config_path)
    try:
        assert apps[0].state == RuntimeState.STOPPED
    finally:
        # Keep the regression probe itself leak-free before the fix.
        await apps[0].stop()
