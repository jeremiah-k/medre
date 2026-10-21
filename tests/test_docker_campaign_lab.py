"""Isolation and evidence guards for the opt-in ecosystem lab."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.adapter_campaign.lab import control
from scripts.adapter_campaign.lab.sources import snapshot, write_json
from scripts.adapter_campaign.lab.stack import configure


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root


def test_snapshot_copies_candidate_bytes_and_excludes_private_state(tmp_path):
    root = repository(tmp_path)
    (root / "credentials.json").write_text("private")
    evidence = snapshot(root, tmp_path / "copy")
    assert not evidence["dirty"]
    assert evidence["files"] == 1
    assert not (tmp_path / "copy/credentials.json").exists()
    (root / "source.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="Commit source"):
        snapshot(root, tmp_path / "rejected")
    candidate = snapshot(root, tmp_path / "candidate", allow_dirty=True)
    assert candidate["dirty"]
    assert candidate["snapshot_sha256"] != evidence["snapshot_sha256"]
    assert (tmp_path / "candidate/source.py").read_text() == "value = 2\n"


def test_snapshot_rejects_tracked_symlinks(tmp_path):
    root = repository(tmp_path)
    try:
        (root / "escape").symlink_to("/etc/passwd")
    except OSError:
        pytest.skip("symlink creation unavailable")
    subprocess.run(["git", "-C", str(root), "add", "escape"], check=True)
    with pytest.raises(ValueError, match="symlinks"):
        snapshot(root, tmp_path / "copy", allow_dirty=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX lab UID/GID ownership")
def test_stack_does_not_publish_services_or_grant_host_privilege(tmp_path):
    (tmp_path / "config").mkdir()
    write_json(tmp_path / "hardware.json", {})
    images = json.loads((Path(control.__file__).parent / "images.json").read_text())
    configure(tmp_path, images, "medre-lab-fixture", {"http": {}, "database": {}})
    stack = json.loads((tmp_path / "compose.json").read_text())
    assert stack["name"] == "medre-lab-fixture"
    for service in stack["services"].values():
        assert not service.get("ports")
        assert service.get("network_mode") != "host"
        assert not service.get("privileged")
        assert not service.get("sysctls")
        assert not service.get("devices")
        assert all("docker.sock" not in value for value in service.get("volumes", []))
    assert "mmrelay" not in stack["services"]  # Runs sequentially in its own venv.
    assert stack["services"]["runner"]["cap_drop"] == ["ALL"]
    assert all("@sha256:" in value for value in images.values())


@pytest.mark.skipif(os.name == "nt", reason="POSIX lab directory permissions")
def test_cleanup_runs_even_when_service_log_collection_fails(tmp_path, monkeypatch):
    project = "medre-lab-0123456789ab"
    tmp_path.chmod(0o700)
    write_json(tmp_path / "state.json", {"version": 1, "project": project})
    write_json(tmp_path / "compose.json", {"name": project})
    actions = []

    def command(directory, name, argv, **kwargs):
        actions.append((name, argv))
        if name == "service-logs":
            raise RuntimeError("log collection failed")

    monkeypatch.setattr(control, "command", command)
    with pytest.raises(RuntimeError, match="log collection"):
        control.operate(argparse.Namespace(action="down", directory=tmp_path))
    assert [name for name, _ in actions] == ["service-logs", "cleanup"]
    assert "--volumes" in actions[-1][1]


def test_cleanup_rejects_a_project_identity_mismatch(tmp_path, monkeypatch):
    write_json(
        tmp_path / "state.json", {"version": 1, "project": "medre-lab-0123456789ab"}
    )
    write_json(tmp_path / "compose.json", {"name": "unrelated-deployment"})
    actions = []
    monkeypatch.setattr(control, "command", lambda *args: actions.append(args))
    with pytest.raises(ValueError, match="matching disposable lab"):
        control.operate(argparse.Namespace(action="down", directory=tmp_path))
    assert not actions


def test_failed_command_retains_evidence_without_logging_arguments(tmp_path):
    (tmp_path / "logs").mkdir()
    with pytest.raises(RuntimeError, match="private log"):
        control.command(
            tmp_path,
            "failure",
            [sys.executable, "-c", "raise SystemExit(1)", "private-password"],
        )
    evidence = (tmp_path / "actions.jsonl").read_text()
    assert json.loads(evidence)["status"] == "failed"
    assert json.loads(evidence)["log"].startswith("logs/")
    assert "private-password" not in evidence
    assert (tmp_path / json.loads(evidence)["log"]).exists()


def test_log_review_counts_records_without_exporting_message_content():
    from scripts.adapter_campaign.lab.log_review import analyze

    report = analyze(
        "2026-10-21T16:00:00+0000 [INFO] medre.runtime: private-body\n2026-10-21 16:00:01 +0000 WARNING:Matrix:private-body\nWARNING:medre.runtime:no timestamp\nUnclosed client session\n"
    )
    assert report["levels"] == {"INFO": 1, "WARNING": 2}
    assert report["records_without_timestamp"] == [3]
    assert report["resource_warning_lines"] == [4]
    assert "private-body" not in json.dumps(report)


def test_log_review_cannot_treat_wrapper_output_as_runtime_evidence(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review

    (tmp_path / "logs").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "logs/native-campaign-1234567891234567.log").write_text(
        "2026-10-21T16:00:00+0000 [INFO] medre.runtime: stopped\nUnclosed client session\n"
    )
    (tmp_path / "logs/native-campaign-01.log").write_text("Docker runner created\n")
    with pytest.raises(RuntimeError, match="resource warnings"):
        review(tmp_path)
    report = json.loads((tmp_path / "results/log-review.json").read_text())
    assert list(report["logs"]) == ["logs/native-campaign-1234567891234567.log"]
