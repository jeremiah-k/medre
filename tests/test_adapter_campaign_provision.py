"""Provisioning retries preserve independent native fixture outcomes."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.adapter_campaign.lab import provision
from scripts.adapter_campaign.lab.sources import write_json


async def test_matrix_provisioning_refuses_a_device_without_its_existing_store(
    tmp_path,
):
    """Do not reuse OAuth credentials while allowing a fresh crypto account."""
    with pytest.raises(ValueError, match="OAuth and its crypto store"):
        await provision.matrix(tmp_path, {})


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


async def test_lxmf_failure_is_recorded_and_does_not_hide_other_fixture_retries(
    tmp_path, monkeypatch, ready_fixtures
):
    fixtures = dict(ready_fixtures)
    fixtures["lxmf"] = {"status": "failed"}
    write_json(tmp_path / "results/provision.json", fixtures)
    lxmf = Mock(side_effect=OSError("device not ready"))
    monkeypatch.setattr(provision, "lxmf", lxmf)
    with pytest.raises(RuntimeError, match="^Native provisioning failed: lxmf$"):
        await provision.provision(tmp_path)
    recorded = json.loads((tmp_path / "results/provision.json").read_text())
    assert recorded["lxmf"] == {"status": "failed", "error_type": "OSError"}
    for name in fixtures.keys() - {"lxmf"}:
        assert recorded[name] == ready_fixtures[name]
    lxmf.assert_called_once_with(tmp_path)


def test_initial_signing_reset_is_consumed_before_signing_attempt(tmp_path):
    from scripts.adapter_campaign.lab.oauth_checks import claim_initial_signing_reset

    profile = tmp_path / "profile"
    profile.mkdir()
    marker = tmp_path / "results/reset-attempted"
    assert claim_initial_signing_reset(profile, marker)
    assert marker.is_file()
    assert not claim_initial_signing_reset(profile, marker)
    marker.unlink()
    (profile / "store.db").write_bytes(b"persisted identity")
    assert not claim_initial_signing_reset(profile, marker)
    assert not marker.exists()
    (profile / "store.db").unlink()
    (profile / "cross-signing.json").write_text("{}")
    assert not claim_initial_signing_reset(profile, marker)
