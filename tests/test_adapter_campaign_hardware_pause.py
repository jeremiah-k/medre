"""MeshCore qualification pauses after one failure until operator repair is acknowledged."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.adapter_campaign.lab import control, hardware, scenarios


async def test_failed_meshcore_probe_stops_pair_and_requires_explicit_repair(
    tmp_path, monkeypatch
):
    """Do not touch the second board or reopen either board during a paused run."""
    (tmp_path / "results").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "hardware.json").write_text(
        json.dumps({"meshcore": {"gateway_ble_address": "A", "peer_ble_address": "B"}})
    )
    calls = []
    failing = True

    @asynccontextmanager
    async def open_device(address):
        """Model one failed link, followed by successful operator re-pairing."""
        calls.append(address)
        if failing:
            raise ConnectionError("pairing required")
        yield SimpleNamespace(
            self_info={"name": address},
            commands=SimpleNamespace(
                get_time=AsyncMock(return_value=SimpleNamespace(payload={})),
            ),
        )

    monkeypatch.setattr(
        "scripts.adapter_campaign.meshcore_ble.open_meshcore_ble", open_device
    )
    result = await hardware.inspect_hardware(tmp_path)
    assert calls == ["A"]
    assert result["meshcore"]["gateway"]["status"] == "unavailable"
    assert result["meshcore"]["peer"]["status"] == "not_attempted"
    failing = False
    await hardware.inspect_hardware(tmp_path)
    assert calls == ["A"]
    result = await hardware.inspect_hardware(tmp_path, meshcore_repaired=True)
    assert calls == ["A", "A", "B"]
    assert all(value["status"] == "available" for value in result["meshcore"].values())
    assert not (tmp_path / "results/meshcore-paused.json").exists()


async def test_docker_provisioning_never_opens_radio_hardware(tmp_path, monkeypatch):
    """Services can be provisioned independently of pairing or serial readiness."""
    from scripts.adapter_campaign.lab import provision

    create = AsyncMock()
    inspect = AsyncMock(side_effect=AssertionError("unexpected hardware access"))
    monkeypatch.setattr(scenarios, "ROOT", tmp_path)
    monkeypatch.setattr(scenarios, "inspect_hardware", inspect)
    monkeypatch.setattr(provision, "provision", create)
    await scenarios.run("provision")
    create.assert_awaited_once_with(tmp_path)
    inspect.assert_not_called()


def test_meshcore_repair_flag_is_explicit_and_hardware_only(tmp_path, monkeypatch):
    """Other lab stages cannot accidentally acknowledge a pairing repair."""
    from scripts.adapter_campaign.lab.__main__ import main

    called = []
    monkeypatch.setattr(control, "operate", called.append)
    assert (
        main(
            [
                "test",
                "--directory",
                str(tmp_path),
                "--stage",
                "hardware",
                "--meshcore-repaired",
            ]
        )
        == 0
    )
    assert called[0].meshcore_repaired is True
    with pytest.raises(SystemExit):
        main(
            [
                "test",
                "--directory",
                str(tmp_path),
                "--stage",
                "provision",
                "--meshcore-repaired",
            ]
        )
