# medre-test-tier: compatibility
"""An installed multi-adapter smoke must test each route source, not just one.

These tests are gated by external entry-point availability so MEDRE's core-only
CI remains independent of the four adapter distributions. The cross-adapter
job installs the pinned providers and exercises the full test below.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from medre.core.contracts.adapter import AdapterPermanentError
from medre.runtime.smoke import run_fake_bridge_smoke
from tests.helpers.drill_configs import write_drill_target_config


@pytest.fixture(autouse=True)
def _isolated_medre_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def cross_adapter_config(tmp_path: Path) -> str:
    providers = {entry.name for entry in entry_points(group="medre.adapters")}
    required = {"matrix", "meshtastic", "meshcore", "lxmf"}
    if missing := required - providers:
        pytest.skip(f"external adapter providers unavailable: {sorted(missing)}")
    return write_drill_target_config(tmp_path)


@pytest.mark.parametrize(
    ("source", "targets"),
    [
        ("matrix", {"meshtastic", "meshcore", "lxmf"}),
        ("meshtastic", {"matrix"}),
        ("meshcore", {"lxmf"}),
        ("lxmf", {"meshcore"}),
    ],
)
async def test_every_route_source_produces_real_delivery_evidence(
    cross_adapter_config: str, source: str, targets: set[str], tmp_path: Path
) -> None:
    report = await run_fake_bridge_smoke(
        cross_adapter_config,
        source_adapter=source,
        storage_path=str(tmp_path / f"smoke-{source}.sqlite"),
    )
    assert report["status"] == "passed", report.get("fail_reasons")
    assert report["source_adapter"] == source
    assert report["coverage_scope"] == "single_source"
    assert set(report["source_candidates"]) == {
        "matrix",
        "meshtastic",
        "meshcore",
        "lxmf",
    }
    assert set(report["target_adapters"]) == targets
    assert len(report["delivery_receipts"]) >= len(targets)


async def test_unrouted_source_is_reported_as_failed(
    cross_adapter_config: str,
) -> None:
    report = await run_fake_bridge_smoke(
        cross_adapter_config, source_adapter="not-configured"
    )
    assert report["status"] == "failed"
    assert "not an enabled route source" in report["fail_reasons"][0]


async def test_partial_fanout_delivery_cannot_be_reported_as_pass(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter

    async def fail_meshcore(self: Any, payload: Any) -> Any:
        raise AdapterPermanentError("injected one-target failure")

    monkeypatch.setattr(FakeMeshCoreAdapter, "deliver", fail_meshcore)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert (
        "One or more route delivery outcomes were not successful"
        in report["fail_reasons"]
    )
    assert "meshtastic" in report["target_adapters"]
    assert "lxmf" in report["target_adapters"]


@pytest.mark.parametrize("failure_stage", ["build", "start"])
async def test_pruned_fanout_target_cannot_disappear_from_smoke_pass(
    cross_adapter_config: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter

    if failure_stage == "build":

        def fail_build(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("target construction failed")

        monkeypatch.setattr(FakeMeshCoreAdapter, "__init__", fail_build)
    else:

        async def fail_start(self: Any, ctx: Any) -> None:
            raise RuntimeError("target startup failed")

        monkeypatch.setattr(FakeMeshCoreAdapter, "start", fail_start)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed", report
    assert any("meshcore" in reason for reason in report["fail_reasons"])
    assert "meshtastic" in report["target_adapters"]
    assert "lxmf" in report["target_adapters"]


async def test_smoke_event_factory_error_stops_runtime(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.adapters.fakes.matrix import FakeMatrixAdapter
    from medre.runtime.app import MedreApp

    stopped = []
    original_stop = MedreApp.stop

    async def stop(app: MedreApp) -> None:
        await original_stop(app)
        stopped.append(app)

    def fail_event(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("event factory failed")

    monkeypatch.setattr(MedreApp, "stop", stop)
    monkeypatch.setattr(FakeMatrixAdapter, "make_event", fail_event)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert "event factory failed" in report["fail_reasons"][0]
    assert len(stopped) == 1


async def test_missing_durable_receipt_for_one_target_fails_smoke(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful adapter outcomes alone cannot stand in for persisted evidence."""
    from medre.core.storage.sqlite.storage import SQLiteStorage

    original = SQLiteStorage.list_receipts_for_event

    async def hide_one_target(self: SQLiteStorage, event_id: str) -> list[Any]:
        receipts = await original(self, event_id)
        return [r for r in receipts if r.target_adapter != "meshcore"]

    monkeypatch.setattr(SQLiteStorage, "list_receipts_for_event", hide_one_target)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert any(
        "missing durable sent receipt" in reason and "meshcore" in reason
        for reason in report["fail_reasons"]
    )
    assert {r["target_adapter"] for r in report["delivery_receipts"]} >= {
        "meshtastic",
        "lxmf",
    }


async def test_shutdown_failure_does_not_produce_passing_smoke(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.runtime.app import MedreApp

    original = MedreApp.stop

    async def stop_then_fail(self: MedreApp) -> None:
        await original(self)
        raise RuntimeError("injected shutdown error")

    monkeypatch.setattr(MedreApp, "stop", stop_then_fail)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert any("Runtime shutdown failed" in r for r in report["fail_reasons"])
    assert report["shutdown_status"] == "stopped"


async def test_snapshot_failure_does_not_produce_passing_smoke(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_snapshot(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected snapshot error")

    monkeypatch.setattr("medre.runtime.smoke.build_runtime_snapshot", fail_snapshot)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert report["shutdown_status"] is None
    assert any("Final runtime snapshot failed" in r for r in report["fail_reasons"])


async def test_partial_delivery_accounting_cannot_pass_multi_target_smoke(
    cross_adapter_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.core.supervision.accounting import RuntimeAccounting

    original = RuntimeAccounting.snapshot

    def undercount(self: RuntimeAccounting) -> dict[str, int]:
        snapshot = original(self)
        return {**snapshot, "outbound_delivered": 1}

    monkeypatch.setattr(RuntimeAccounting, "snapshot", undercount)
    report = await run_fake_bridge_smoke(cross_adapter_config, source_adapter="matrix")
    assert report["status"] == "failed"
    assert any(
        "configured delivery count 3" in reason for reason in report["fail_reasons"]
    )
