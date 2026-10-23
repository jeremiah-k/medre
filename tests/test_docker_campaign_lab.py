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


def test_docker_lab_contracts_and_oauth_requirements_are_present():
    dockerfile = (Path(control.__file__).parent / "Dockerfile").read_text()
    assert "bluez dbus openssl git" in dockerfile
    assert "--extra e2e" in dockerfile


def test_lab_stage_timeout_encloses_all_nested_stage_budgets():
    from scripts.adapter_campaign.lab.budgets import NATIVE_STAGES, stage_timeout

    for provider_count in (1, 9, 12):
        contracts = stage_timeout("contracts", provider_count)
        assert contracts > provider_count * 1800
        assert (
            stage_timeout("all", provider_count) > contracts + len(NATIVE_STAGES) * 2400
        )
        assert stage_timeout("native", provider_count) > len(NATIVE_STAGES) * 2400
        assert stage_timeout("all", provider_count) > stage_timeout(
            "native", provider_count
        )


def recovery_logs(root: Path) -> Path:
    from scripts.adapter_campaign.recovery import SCENARIOS, runtime_generations

    (root / "logs").mkdir()
    run = root / "results/resilience-123"
    for scenario in SCENARIOS:
        case = run / scenario
        case.mkdir(parents=True)
        for name in runtime_generations(scenario):
            (case / (name + ".jsonl")).write_text("{}\n")
            (case / (name + ".stdout")).write_text(
                "2026-10-21T16:00:00+0000 [INFO] medre.runtime: started\n"
            )
            (case / (name + ".stderr")).write_text("")
    return run


def test_log_review_checks_recovery_generations_instead_of_wrapper_results(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "crash_after_handoff"
    (case / "runtime-0.stdout").write_text(
        "2026-10-21T16:00:00+0000 [INFO] medre.runtime: private-body\n"
    )
    (case / "runtime-1.stdout").write_text(
        "2026-10-21T16:00:01+0000 [INFO] medre.runtime: resumed\nTask was destroyed but it is pending\n"
    )
    (case / "runtime-0.stderr").write_text("")
    (case / "runtime-1.stderr").write_text("")
    (case / "runtime-0.jsonl").write_text("{}\n")
    (case / "runtime-1.jsonl").write_text("{}\n")
    (tmp_path / "results/resilience-999.json").write_text('{"status":"passed"}')
    with pytest.raises(RuntimeError, match="resource warnings"):
        review(tmp_path)
    report = json.loads((tmp_path / "results/log-review.json").read_text())
    assert len(report["logs"]) == 16
    assert "private-body" not in json.dumps(report)


def test_resilience_is_a_selectable_lab_stage(tmp_path, monkeypatch):
    from scripts.adapter_campaign.lab.__main__ import main

    calls = []
    monkeypatch.setattr(control, "operate", calls.append)
    assert main(["test", "--directory", str(tmp_path), "--stage", "resilience"]) == 0
    assert calls[0].stage == "resilience"


def test_log_review_checks_runtime_stderr_without_requiring_log_records(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "crash_before_handoff"
    (case / "runtime-0.stdout").write_text(
        "2026-10-21T16:00:00+0000 [INFO] medre.runtime: private-body\n"
    )
    (case / "runtime-0.stderr").write_text("Unclosed client session\n")
    (case / "runtime-0.jsonl").write_text("{}\n")
    with pytest.raises(RuntimeError, match="resource warnings"):
        review(tmp_path)
    results = json.loads((tmp_path / "results/log-review.json").read_text())
    stderr_key = "results/resilience-123/crash_before_handoff/runtime-0.stderr"
    assert results["logs"][stderr_key]["resource_warning_lines"] == [1]
    assert "private-body" not in json.dumps(results)

    for warning in (
        "RuntimeWarning: coroutine was never awaited",
        "Task exception was never retrieved",
        "Future exception was never retrieved",
        "ResourceWarning: unclosed file",
    ):
        (case / "runtime-0.stderr").write_text(warning + "\n")
        with pytest.raises(RuntimeError, match="resource warnings"):
            review(tmp_path)

    (case / "runtime-0.stderr").write_text("SDK emitted non-log diagnostic\n")
    review(tmp_path)
    results = json.loads((tmp_path / "results/log-review.json").read_text())
    assert results["status"] == "passed"
    assert results["logs"][stderr_key]["levels"] == {}


@pytest.mark.parametrize("missing", ["stdout", "stderr"])
def test_log_review_rejects_incomplete_runtime_stream_evidence(tmp_path, missing):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "crash_before_handoff"
    (case / "runtime-0.jsonl").write_text("{}\n")
    (case / ("runtime-0." + missing)).unlink()
    suffix = "stderr" if missing == "stdout" else "stdout"
    (case / ("runtime-0." + suffix)).write_text(
        "2026-10-21T16:00:00+0000 [INFO] medre.runtime: stopped\n"
    )
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    result = json.loads((tmp_path / "results/log-review.json").read_text())
    assert result["status"] == "failed"
    assert result["missing_streams"] == [
        f"results/resilience-123/crash_before_handoff/runtime-0.{missing}"
    ]


async def test_native_recovery_configures_bounded_reconnection_retries(
    tmp_path, monkeypatch
):
    from scripts.adapter_campaign import recovery_worker

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        "adapters:\n  matrix:\n    source: {}\n  xmpp:\n    target:\n      host: localhost\n      port: 5222\n"
    )
    captured = {}

    class Configured(Exception):
        pass

    def capture(data, paths):
        captured.update(data)
        raise Configured

    monkeypatch.setattr(recovery_worker, "parse_runtime_config", capture)
    with pytest.raises(Configured):
        await recovery_worker.run(
            {
                "source": "source",
                "target": "target",
                "runtime_file": str(runtime),
                "database": str(tmp_path / "medre.sqlite3"),
                "directory": str(tmp_path),
                "source_channel": "!room:example.test",
                "target_channel": "room@rooms.example.test",
            }
        )
    assert captured["routes"]["recovery_route"]["retry"] == {
        "enabled": True,
        "max_attempts": 3,
        "backoff_base": 10,
        "max_delay_seconds": 20,
        "jitter": False,
    }
    assert captured["retry"]["enabled"] is True
    assert captured["retry"]["max_attempts"] == 3


async def test_log_review_reads_stderr_from_actual_owned_runtime(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review
    from scripts.adapter_campaign.process import Peer

    case = recovery_logs(tmp_path) / "crash_before_handoff"
    child = (
        "import json,sys\n"
        "json.loads(sys.stdin.readline())\n"
        "print('2026-10-21T16:00:00+0000 [INFO] medre: started', flush=True)\n"
        "print('MRPEER ' + json.dumps({'kind':'ready','identity':'runtime'}), "
        "flush=True)\n"
        "print('Task exception was never retrieved', file=sys.stderr, flush=True)\n"
        "for line in sys.stdin:\n"
        "    if json.loads(line)['op'] == 'stop': break\n"
    )
    for suffix in ("jsonl", "stdout", "stderr"):
        (case / ("runtime-0." + suffix)).unlink()
    peer = Peer("runtime-0", case, 3)
    try:
        await peer.start({}, argv=[sys.executable, "-c", child])
    finally:
        await peer.stop()
    with pytest.raises(RuntimeError, match="resource warnings"):
        review(tmp_path)
    result = json.loads((tmp_path / "results/log-review.json").read_text())
    stderr_key = "results/resilience-123/crash_before_handoff/runtime-0.stderr"
    assert result["logs"][stderr_key]["resource_warning_lines"] == [1]


def test_log_review_requires_both_streams_for_each_recorded_generation(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "crash_after_handoff"
    for suffix in ("stdout", "stderr"):
        (case / ("runtime-1." + suffix)).unlink()
    (case / "runtime-0.stdout").write_text(
        "2026-10-21T16:00:00+0000 [INFO] medre: startup\n"
    )
    (case / "runtime-0.stderr").write_text("")
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    result = json.loads((tmp_path / "results/log-review.json").read_text())
    assert result["missing_streams"] == [
        "results/resilience-123/crash_after_handoff/runtime-1.stderr",
        "results/resilience-123/crash_after_handoff/runtime-1.stdout",
    ]


@pytest.mark.parametrize("removed", ["generation", "scenario", "control"])
def test_log_review_rejects_missing_generations_even_without_control_files(
    tmp_path, removed
):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "crash_after_handoff"
    names = ("runtime-0", "runtime-1") if removed == "scenario" else ("runtime-1",)
    suffixes = ("jsonl",) if removed == "control" else ("jsonl", "stdout", "stderr")
    for name in names:
        for suffix in suffixes:
            (case / (name + "." + suffix)).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    result = json.loads((tmp_path / "results/log-review.json").read_text())
    assert result["status"] == "failed"
    assert result["expected_generations"] == 8
    assert result["missing_controls"] == [
        "results/resilience-123/crash_after_handoff/" + name + ".jsonl"
        for name in names
    ]


def test_log_review_rejects_unexpected_generation_evidence(tmp_path):
    from scripts.adapter_campaign.lab.log_review import review

    case = recovery_logs(tmp_path) / "transient_outage"
    for suffix in ("jsonl", "stdout", "stderr"):
        (case / ("runtime-99." + suffix)).write_text("")
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    result = json.loads((tmp_path / "results/log-review.json").read_text())
    assert len(result["unexpected_streams"]) == 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX lab directory permissions")
def test_lab_account_password_can_begin_with_an_option_prefix(tmp_path, monkeypatch):
    """Pass generated credentials as values even when they start with a hyphen."""
    project = "medre-lab-0123456789ab"
    tmp_path.chmod(0o700)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/ca.crt").touch()
    (tmp_path / "actions.jsonl").touch()
    password = "-Jfixture-password"
    write_json(tmp_path / "state.json", {"version": 1, "project": project})
    write_json(tmp_path / "compose.json", {"name": project})
    write_json(
        tmp_path / "config/accounts.json", {"users": ["medre"], "password": password}
    )
    commands = {}

    def command(directory, name, argv, **kwargs):
        commands[name] = argv

    monkeypatch.setattr(control, "command", command)
    monkeypatch.setattr(control.subprocess, "check_output", lambda *a, **k: b"local\n")
    control.operate(argparse.Namespace(action="up", directory=tmp_path))
    parser = argparse.ArgumentParser()
    parser.add_argument("--password")
    parsed, _ = parser.parse_known_args(commands["account-medre"])
    assert parsed.password == password
    assert (tmp_path / "config/smp-fingerprint").read_bytes() == b"local\n"


@pytest.mark.skipif(os.name == "nt", reason="Linux BlueZ system bus")
def test_meshtastic_ble_role_maps_only_the_runner_system_bus(tmp_path):
    """An explicitly selected BLE peer gets the same scoped access as MeshCore."""
    (tmp_path / "config").mkdir()
    write_json(
        tmp_path / "hardware.json",
        {
            "meshtastic": {
                "gateway_host": "radio.test",
                "peer_ble_address": "AA:BB:CC:DD:EE:FF",
            }
        },
    )
    images = json.loads((Path(control.__file__).parent / "images.json").read_text())
    configure(tmp_path, images, "medre-lab-fixture", {"http": {}, "database": {}})
    stack = json.loads((tmp_path / "compose.json").read_text())
    runner = stack["services"]["runner"]
    assert "apparmor:unconfined" in runner["security_opt"]
    assert (
        "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro"
        in runner["volumes"]
    )
    assert runner["cap_drop"] == ["ALL"]
    assert not runner.get("privileged")
    assert all(
        "dbus" not in str(service.get("volumes", []))
        for name, service in stack["services"].items()
        if name != "runner"
    )
