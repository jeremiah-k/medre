# medre-test-tier: compatibility
"""Readiness report against the real compatibility snapshot and registry.

Unlike the hermetic composition tests, these scenarios run without stubs:
they exercise the real ``.github/adapter-compatibility.json`` snapshot and
whatever providers this environment actually has installed, so assertions
hold in both an SDK-free environment and a fully provisioned one.
"""

from __future__ import annotations

import json

import pytest

from medre.cli.main import main
from medre.runtime import readiness_report
from medre.runtime.readiness_report import build_readiness_report

_SNAPSHOT_TRANSPORTS = {
    "matrix",
    "meshtastic",
    "meshcore",
    "lxmf",
    "xmpp",
    "chatmail",
    "simplexchat",
}


@pytest.fixture(autouse=True)
def isolated_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "private-state"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)


def test_real_snapshot_lists_every_manifest_provider() -> None:
    """Every snapshot provider is reported, installed or not."""
    report = build_readiness_report(None)
    manifest = report["manifest"]
    assert manifest["status"] == "available"
    transports = {p["transport"] for p in manifest["providers"]}
    assert transports == _SNAPSHOT_TRANSPORTS
    assert manifest["provider_count"] == len(_SNAPSHOT_TRANSPORTS)
    for provider in manifest["providers"]:
        assert provider["ref"]
        assert provider["distribution"]
        if provider["distribution_status"] == "installed":
            assert provider["installed_version"]
        if provider["registration"] == "registered":
            assert provider["native_setup"] in {
                "provider_guided",
                "manual_provisioning",
            }
        if provider["registration"] == "broken":
            assert provider["registration_error_category"]


def test_real_command_reports_the_full_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command exits successfully and includes the whole manifest."""
    main(["readiness", "--json"])
    report = json.loads(capsys.readouterr().out)
    transports = {p["transport"] for p in report["manifest"]["providers"]}
    assert transports == _SNAPSHOT_TRANSPORTS
    assert report["boundary"]["runtime"] == "not_observed"
    assert report["boundary"]["recipients"] == "not_observed"
    assert report["guidance"]


def test_manifest_authority_matches_the_snapshot_loader_rules() -> None:
    """The report's snapshot loader accepts exactly the tracked manifest."""
    status, providers = readiness_report._load_manifest()
    assert status == "available"
    assert {p["transport"] for p in providers} == _SNAPSHOT_TRANSPORTS
