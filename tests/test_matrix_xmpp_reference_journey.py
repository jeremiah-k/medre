"""Fail-closed qualification guards for the opt-in Matrix/XMPP reference lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.adapter_campaign.fidelity import CASES
from scripts.adapter_campaign.lab import __main__ as lab_cli
from scripts.adapter_campaign.lab import fidelity, reference, scenarios


def _write_sources(root: Path) -> None:
    sources = {
        name: {
            "checkout": f"/private/{name}/secret-path",
            "commit": "a" * 40,
            "snapshot_sha256": "b" * 64,
            "dirty": False,
        }
        for name in ("medre", "matrix", "xmpp")
    }
    (root / "sources.json").write_text(json.dumps(sources))


def _passing_analysis() -> dict:
    return {
        "version": 1,
        "status": "passed",
        "cases": [{"name": name, "status": "passed"} for name, _, _ in CASES],
    }


def test_reference_verdict_requires_both_directions_and_all_cases():
    report = reference.summarize(_passing_analysis())
    assert report["status"] == "passed"
    assert report["case_count"] == len(CASES)
    assert set(report["source_case_counts"]) == {"matrix", "xmpp"}
    assert all(report["source_case_counts"].values())
    assert "Plaintext Matrix room" in report["limits"][1]
    assert "message" not in str(report)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc.update(status="running"),
        lambda doc: doc.update(version=2),
        lambda doc: doc["cases"].pop(),
        lambda doc: doc["cases"][0].update(status="failed"),
        lambda doc: doc["cases"][0].update(name="wrong"),
        lambda doc: doc["cases"].append({"name": "extra", "status": "passed"}),
    ],
)
def test_reference_verdict_refuses_incomplete_or_unrelated_evidence(mutation):
    source = _passing_analysis()
    mutation(source)
    with pytest.raises(ValueError, match="did not establish"):
        reference.summarize(source)


def test_reference_cli_accepts_only_explicit_reference_stage(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(
        "scripts.adapter_campaign.lab.control.operate",
        lambda namespace: captured.append(namespace),
    )
    assert (
        lab_cli.main(["test", "--directory", str(tmp_path), "--stage", "reference"])
        == 0
    )
    assert (
        lab_cli.main(
            ["test", "--directory", str(tmp_path), "--stage", "reference-oauth"]
        )
        == 0
    )
    assert (
        lab_cli.main(
            ["test", "--directory", str(tmp_path), "--stage", "reference-provision"]
        )
        == 0
    )
    assert [row.stage for row in captured] == [
        "reference",
        "reference-oauth",
        "reference-provision",
    ]


@pytest.mark.asyncio
async def test_reference_dispatch_uses_fidelity_and_requires_verdict(
    tmp_path, monkeypatch
):
    async def fake_campaign(root, *, presentation=False, reference=False):
        assert root == tmp_path and reference and not presentation
        folder = root / "results/reference-nonce"
        folder.mkdir(parents=True)
        (folder / "analysis.json").write_text(json.dumps(_passing_analysis()))
        return folder

    _write_sources(tmp_path)
    monkeypatch.setattr(scenarios, "ROOT", tmp_path)
    monkeypatch.setattr(fidelity, "run", fake_campaign)
    await scenarios.run("reference")
    receipt = json.loads(
        (tmp_path / "results/reference-nonce/reference-verdict.json").read_text()
    )
    assert receipt["evidence_level"] == "docker_native_independent_peers_and_sqlite"
    assert receipt["case_count"] == len(CASES)
    assert set(receipt["source_revisions"]) == {"medre", "matrix", "xmpp"}
    assert "/private/" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_reference_does_not_write_verdict_for_failed_analysis(
    tmp_path, monkeypatch
):
    async def invalid_campaign(root, *, reference=False):
        folder = root / "results/reference-invalid"
        folder.mkdir(parents=True)
        (folder / "analysis.json").write_text(json.dumps({"status": "failed"}))
        return folder

    _write_sources(tmp_path)
    monkeypatch.setattr(fidelity, "run", invalid_campaign)
    with pytest.raises(ValueError, match="did not establish"):
        await reference.run(tmp_path)
    assert not (tmp_path / "results/reference-invalid/reference-verdict.json").exists()


@pytest.mark.asyncio
async def test_reference_provision_only_uses_matrix_and_xmpp(tmp_path, monkeypatch):
    from scripts.adapter_campaign.lab import provision as provision_module

    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "config/accounts.json").write_text(
        json.dumps({"password": "DO-NOT-EMIT"})
    )
    called = []

    async def stub(name, root, accounts):
        assert root == tmp_path and accounts["password"] == "DO-NOT-EMIT"
        called.append(name)
        return {"configured": name}, {"native_peer": name}

    monkeypatch.setattr(provision_module, "matrix", lambda r, a: stub("matrix", r, a))
    monkeypatch.setattr(provision_module, "xmpp", lambda r, a: stub("xmpp", r, a))
    await reference.provision(tmp_path)
    assert called == ["matrix", "xmpp"]
    document = json.loads((tmp_path / "results/provision.json").read_text())
    assert set(document) == {"matrix", "xmpp"}
    await reference.provision(tmp_path)
    assert called == ["matrix", "xmpp"]
    assert "DO-NOT-EMIT" not in json.dumps(document)


@pytest.mark.asyncio
async def test_reference_provision_fails_closed_and_retains_independent_outcomes(
    tmp_path, monkeypatch
):
    from scripts.adapter_campaign.lab import provision as provision_module

    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "config/accounts.json").write_text(json.dumps({"password": "secret"}))

    async def fail(*args):
        raise RuntimeError("SECRET DIAGNOSTIC")

    async def pass_xmpp(*args):
        return {"name": "xmpp"}, {"native_peer": "xmpp"}

    monkeypatch.setattr(provision_module, "matrix", fail)
    monkeypatch.setattr(provision_module, "xmpp", pass_xmpp)
    with pytest.raises(
        RuntimeError, match="Reference provisioning failed: matrix"
    ) as err:
        await reference.provision(tmp_path)
    assert "SECRET DIAGNOSTIC" not in str(err.value)
    document = json.loads((tmp_path / "results/provision.json").read_text())
    assert document["matrix"]["status"] == "failed"
    assert document["xmpp"]["status"] == "ready"


def test_reference_does_not_enlarge_all_stage_collection():
    from scripts.adapter_campaign.lab.budgets import NATIVE_STAGES

    assert "reference" not in NATIVE_STAGES
    assert "reference-provision" not in NATIVE_STAGES
    assert "fidelity" in NATIVE_STAGES


@pytest.mark.parametrize(
    "tamper",
    [
        lambda document: document.pop("xmpp"),
        lambda document: document["medre"].update(commit="not-a-sha"),
        lambda document: document["matrix"].update(snapshot_sha256="bad"),
        lambda document: document["xmpp"].update(dirty="false"),
    ],
)
def test_reference_verdict_refuses_unbound_or_malformed_source_snapshot(
    tmp_path, tamper
):
    _write_sources(tmp_path)
    source = json.loads((tmp_path / "sources.json").read_text())
    tamper(source)
    (tmp_path / "sources.json").write_text(json.dumps(source))
    with pytest.raises(ValueError, match="source snapshot"):
        reference.source_revisions(tmp_path)


@pytest.mark.asyncio
async def test_reference_rejects_source_snapshot_change_during_campaign(
    tmp_path, monkeypatch
):
    _write_sources(tmp_path)

    async def tamper_source(root, *, reference=False):
        folder = root / "results/reference-tampered"
        folder.mkdir(parents=True)
        (folder / "analysis.json").write_text(json.dumps(_passing_analysis()))
        source = json.loads((root / "sources.json").read_text())
        source["matrix"]["snapshot_sha256"] = "c" * 64
        (root / "sources.json").write_text(json.dumps(source))
        return folder

    monkeypatch.setattr(fidelity, "run", tamper_source)
    with pytest.raises(ValueError, match="changed during campaign"):
        await reference.run(tmp_path)
    assert not (tmp_path / "results/reference-tampered/reference-verdict.json").exists()
