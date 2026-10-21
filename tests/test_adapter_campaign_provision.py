"""Provisioning retries preserve independent native fixture outcomes."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.adapter_campaign.lab import provision
from scripts.adapter_campaign.lab.sources import write_json


@pytest.fixture
def ready_fixtures(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    write_json(tmp_path / "config/accounts.json", {})
    return {
        name: {
            "status": "ready",
            "runtime": {"profile": name},
            "endpoint": {"channel": name},
        }
        for name in ("lxmf", "matrix", "xmpp", "chatmail", "simplexchat")
    }


@pytest.mark.parametrize("failed_transport", ["matrix", "xmpp", "chatmail"])
async def test_retry_preserves_ready_simplex_when_another_fixture_fails(
    tmp_path, monkeypatch, ready_fixtures, failed_transport
):
    fixtures = dict(ready_fixtures)
    fixtures[failed_transport] = {"status": "failed"}
    write_json(tmp_path / "results/provision.json", fixtures)
    calls = {}
    for name in fixtures:
        factory = Mock if name in {"lxmf", "chatmail"} else AsyncMock
        calls[name] = factory(side_effect=RuntimeError("fixture unavailable"))
        monkeypatch.setattr(
            provision, "simplex" if name == "simplexchat" else name, calls[name]
        )

    with pytest.raises(
        RuntimeError, match=f"^Native provisioning failed: {failed_transport}$"
    ):
        await provision.provision(tmp_path)

    recorded = json.loads((tmp_path / "results/provision.json").read_text())
    assert recorded[failed_transport] == {
        "status": "failed",
        "error_type": "RuntimeError",
    }
    for name in fixtures.keys() - {failed_transport}:
        assert recorded[name] == ready_fixtures[name]
        calls[name].assert_not_called()
    calls[failed_transport].assert_called_once()


async def test_actual_simplex_failure_is_recorded_without_replacing_ready_peers(
    tmp_path, monkeypatch, ready_fixtures
):
    fixtures = dict(ready_fixtures)
    fixtures["simplexchat"] = {"status": "failed"}
    write_json(tmp_path / "results/provision.json", fixtures)
    simplex = AsyncMock(side_effect=TimeoutError("group join timed out"))
    monkeypatch.setattr(provision, "simplex", simplex)

    with pytest.raises(RuntimeError, match="^Native provisioning failed: simplexchat$"):
        await provision.provision(tmp_path)

    recorded = json.loads((tmp_path / "results/provision.json").read_text())
    assert recorded["simplexchat"] == {
        "status": "failed",
        "error_type": "TimeoutError",
    }
    for name in fixtures.keys() - {"simplexchat"}:
        assert recorded[name] == ready_fixtures[name]
    simplex.assert_awaited_once_with(tmp_path)
