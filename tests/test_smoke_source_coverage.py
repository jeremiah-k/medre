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
