# medre-test-tier: compatibility
"""A session proves every configured delivery for every injected event."""

from pathlib import Path

import pytest
import yaml

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.core.contracts.adapter import AdapterPermanentError
from medre.runtime.run_session.orchestration import run_bridge_session
from medre.runtime.smoke import _default_smoke_config_path


@pytest.fixture
def fanout_config(tmp_path: Path) -> str:
    config = yaml.safe_load(Path(_default_smoke_config_path()).read_text())
    config["routes"] = {
        "fanout": {
            "source_adapters": ["fake_matrix"],
            "dest_adapters": ["fake_meshtastic", "fake_meshcore"],
            "enabled": True,
        }
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


@pytest.mark.parametrize("mode", ["direct_pipeline", "adapter_callback"])
@pytest.mark.parametrize("stage", ["build", "start", "deliver"])
async def test_partial_fanout_never_passes(
    fanout_config, tmp_path, monkeypatch, mode, stage
):
    if stage == "build":

        def fail(self, *args, **kwargs):
            raise RuntimeError("injected build failure")

        monkeypatch.setattr(FakeMeshCoreAdapter, "__init__", fail)
    else:

        async def fail(self, *args, **kwargs):
            raise AdapterPermanentError("injected target failure")

        monkeypatch.setattr(FakeMeshCoreAdapter, stage, fail)
    report = await run_bridge_session(
        config_path=fanout_config,
        storage_path=str(tmp_path / "session.sqlite"),
        ingress_mode=mode,
    )
    assert report["status"] == "failed", report
    assert any("fake_meshcore" in reason for reason in report["fail_reasons"])
    assert "fake_meshtastic" in report["target_adapters"]
    assert report["shutdown_status"] == "stopped"


@pytest.mark.parametrize("mode", ["direct_pipeline", "adapter_callback"])
async def test_complete_fanout_proves_each_event(fanout_config, tmp_path, mode):
    report = await run_bridge_session(
        config_path=fanout_config,
        storage_path=str(tmp_path / "session.sqlite"),
        ingress_mode=mode,
        message_count=2,
    )
    assert report["status"] == "passed", report.get("fail_reasons")
    for event_id in report["event_ids"]:
        assert {
            (receipt["route_id"], receipt["target_adapter"])
            for receipt in report["delivery_receipts"]
            if receipt["event_id"] == event_id and receipt["status"] == "sent"
        } == {("fanout", "fake_meshtastic"), ("fanout", "fake_meshcore")}
    assert report["shutdown_status"] == "stopped"
    assert (
        "No live delivery acknowledgement or recipient receipt proven"
        in report["limitations"]
    )


@pytest.mark.parametrize("mode", ["direct_pipeline", "adapter_callback"])
async def test_later_success_cannot_hide_an_earlier_event_failure(
    fanout_config, tmp_path, monkeypatch, mode
):
    original = FakeMeshCoreAdapter.deliver
    attempts = 0

    async def fail_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AdapterPermanentError("first event failed")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(FakeMeshCoreAdapter, "deliver", fail_once)
    report = await run_bridge_session(
        config_path=fanout_config,
        storage_path=str(tmp_path / "session.sqlite"),
        ingress_mode=mode,
        message_count=2,
    )
    assert report["accounting"]["outbound_delivered"] >= 2
    assert report["status"] == "failed", report
    assert report["event_ids"][0] in " ".join(report["fail_reasons"])
    assert report["shutdown_status"] == "stopped"
