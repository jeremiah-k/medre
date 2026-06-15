"""Tests for medre.runtime.docker_bridge_artifacts — metadata, redaction, and paths.

Covers _read_run_metadata, _write_redacted_config (including write-failure
handling), _collect_log_artifacts, structured metadata precedence in
collect_docker_bridge_artifacts, artifact paths in summary, missing-artifact
reporting, config.yaml redaction, _format_yaml_value leaf formatting, and
best-effort error capture / artifact-path aggregation.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.docker_bridge_artifacts import (
    ARTIFACT_PLAN,
    _collect_log_artifacts,
    _format_yaml_value,
    _read_run_metadata,
    _write_redacted_config,
    collect_docker_bridge_artifacts,
    get_artifact_plan,
)
from tests.helpers.docker_artifacts import _FIXED_NOW, _fixed_now


def _make_mock_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake _run_pytest callable returning a fixed result tuple."""

    def _runner(cmd, env, timeout, cwd):
        return returncode, stdout, stderr

    return _runner


def _inject_metadata(base_dir: Path, metadata: dict[str, Any]):
    """Build a now_fn that writes run-metadata.json into the predicted
    run directory on its first invocation."""
    call_count = 0

    def _now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    return _now


# ---------------------------------------------------------------------------
# _read_run_metadata
# ---------------------------------------------------------------------------


def test_returns_none_when_missing(tmp_path: Path) -> None:
    result = _read_run_metadata(tmp_path)
    assert result is None


def test_reads_valid_json(tmp_path: Path) -> None:
    metadata = {"event_id": "$abc", "storage_path": "/tmp/db.sqlite"}
    (tmp_path / "run-metadata.json").write_text(json.dumps(metadata))
    result = _read_run_metadata(tmp_path)
    assert result is not None
    assert result["event_id"] == "$abc"
    assert result["storage_path"] == "/tmp/db.sqlite"


def test_returns_none_on_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "run-metadata.json").write_text("not valid json {{{")
    result = _read_run_metadata(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# _write_redacted_config
# ---------------------------------------------------------------------------


def test_writes_redacted_yaml(tmp_path: Path) -> None:
    config = {
        "synapse_image": "synapse:latest",
        "access_token": "syt_secret123",
        "port": 8008,
    }
    result = _write_redacted_config(tmp_path, config)
    assert result is not None
    assert result.name == "config.yaml"
    content = result.read_text()
    assert "synapse:latest" in content
    assert "access_token" not in content
    assert "8008" in content


def test_handles_nested_values(tmp_path: Path) -> None:
    config = {
        "matrix": {"homeserver": "https://matrix.org", "password": "hunter2"},
        "enabled": True,
    }
    result = _write_redacted_config(tmp_path, config)
    assert result is not None
    content = result.read_text()
    assert "https://matrix.org" in content
    assert "password" not in content
    assert "true" in content.lower()


def test_handles_null_values(tmp_path: Path) -> None:
    config = {"timeout": None, "name": "test"}
    result = _write_redacted_config(tmp_path, config)
    assert result is not None
    content = result.read_text()
    assert "timeout" in content  # present as null
    assert "test" in content


# --- write-failure handling ---


def test_returns_none_when_write_fails(tmp_path: Path) -> None:
    """A failure inside the write try-block is swallowed and None returned."""
    # A non-existent run directory makes write_text raise OSError inside
    # the try block; the writer catches it and returns None.
    missing_dir = tmp_path / "does" / "not" / "exist"
    result = _write_redacted_config(missing_dir, {"k": "v"})
    assert result is None


# ---------------------------------------------------------------------------
# _collect_log_artifacts
# ---------------------------------------------------------------------------


def test_returns_empty_when_no_metadata(tmp_path: Path) -> None:
    result = _collect_log_artifacts(tmp_path, None)
    assert result == {}


def test_copies_referenced_logs(tmp_path: Path) -> None:
    # Create source log files
    synapse_log = tmp_path / "source_synapse.log"
    synapse_log.write_text("synapse log content")
    meshtasticd_log = tmp_path / "source_meshtasticd.log"
    meshtasticd_log.write_text("meshtasticd log content")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metadata = {
        "log_paths": {
            "synapse": str(synapse_log),
            "meshtasticd": str(meshtasticd_log),
        },
    }
    result = _collect_log_artifacts(run_dir, metadata)
    assert "synapse.log" in result
    assert "meshtasticd.log" in result
    assert (run_dir / "synapse.log").read_text() == "synapse log content"


def test_skips_missing_source_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metadata = {
        "log_paths": {
            "synapse": "/nonexistent/synapse.log",
        },
    }
    result = _collect_log_artifacts(run_dir, metadata)
    assert result == {}


# ---------------------------------------------------------------------------
# Structured metadata precedence in collect_docker_bridge_artifacts
#
# Verify structured metadata overrides regex-parsed evidence.
# ---------------------------------------------------------------------------


def test_metadata_overrides_matrix_evidence(tmp_path: Path) -> None:
    """Structured metadata event_id takes precedence over regex."""
    stdout = "ingress_path=sync_loop $regex_event_id 1 passed in 1s\n"
    mock_runner = _make_mock_runner(stdout=stdout)

    # Write run-metadata.json with structured metadata.
    base_dir = tmp_path / "runs"
    # The metadata will be written after run_dir is created by the
    # collector (same timestamp).  Use a post-creation hook via the
    # now_fn to inject metadata.
    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            # First call: create_run_directory
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "event_id": "$metadata_event_id",
                "matrix": {
                    "room": "!metadata_room:localhost",
                    "event_id": "$metadata_event_id",
                    "ingress_path": "sync_loop",
                },
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )

    # Structured metadata event_id should win over regex.
    assert summary["matrix"]["event_id"] == "$metadata_event_id"
    assert summary["matrix"]["room"] == "!metadata_room:localhost"


def test_no_metadata_falls_back_to_regex(tmp_path: Path) -> None:
    """Without metadata, regex parsing still works (deprecated)."""
    stdout = "ingress_path=sync_loop $regex_event_id 1 passed in 1s\n"
    mock_runner = _make_mock_runner(stdout=stdout)

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )

    assert summary["matrix"]["event_id"] == "$regex_event_id"
    # Should have the deprecated-fallback limitation in medre limitations.
    all_limitations = " ".join(summary["medre"]["limitations"]).lower()
    assert "deprecated fallback" in all_limitations


def test_metadata_overrides_medre_evidence(tmp_path: Path) -> None:
    """Structured medre metadata overrides regex receipt parsing."""
    stdout = "receipt_status='sent' $evt123 1 passed in 1s\n"
    mock_runner = _make_mock_runner(stdout=stdout)

    base_dir = tmp_path / "runs"
    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "event_id": "$metadata_evt",
                "medre": {
                    "event_id": "$metadata_evt",
                    "receipt": {"status": "delivered"},
                    "native_refs": [{"adapter": "matrix", "native_id": "$n1"}],
                },
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )

    # Structured medre data should win.
    assert summary["medre"]["event_id"] == "$metadata_evt"
    assert summary["medre"]["receipt"]["status"] == "delivered"


def test_metadata_provides_meshtastic_data(tmp_path: Path) -> None:
    """Structured meshtastic metadata overrides regex parsing."""
    stdout = "packet_id=42 1 passed in 1s\n"
    mock_runner = _make_mock_runner(stdout=stdout)

    base_dir = tmp_path / "runs"
    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "meshtastic": {
                    "packet_ids": ["100", "200"],
                    "pubsub_proven": True,
                },
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="meshtastic_to_matrix",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )

    # Metadata packet_ids should win over regex.
    outbound = summary["meshtastic"].get("outbound", {})
    assert "100" in outbound.get("packet_ids", [])
    assert "200" in outbound.get("packet_ids", [])


def test_storage_export_fn_called_with_metadata(tmp_path: Path) -> None:
    """When metadata has storage_path + event_id, export fn is called."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    base_dir = tmp_path / "runs"
    export_calls: list[dict[str, Any]] = []

    def _mock_export(run_dir, storage_path, event_id):
        export_calls.append(
            {
                "run_dir": run_dir,
                "storage_path": storage_path,
                "event_id": event_id,
            }
        )
        return {}

    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "storage_path": "/path/to/medre.db",
                "event_id": "$evt001",
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=_mock_export,
    )

    assert len(export_calls) == 1
    assert export_calls[0]["storage_path"] == "/path/to/medre.db"
    assert export_calls[0]["event_id"] == "$evt001"


# ---------------------------------------------------------------------------
# Artifact paths in summary (integration)
#
# Verify artifact paths appear in the summary after collection.
# ---------------------------------------------------------------------------


def test_summary_has_artifact_plan(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    expected_plan = get_artifact_plan("matrix_to_meshtastic")
    assert summary["artifact_plan"]["required"] == expected_plan["required"]
    assert summary["artifact_plan"]["best_effort"] == expected_plan["best_effort"]


def test_summary_has_artifact_paths(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    # pytest-stdout.log and pytest-stderr.log should always be present.
    assert "pytest-stdout.log" in summary["artifact_paths"]
    assert "pytest-stderr.log" in summary["artifact_paths"]


def test_summary_reports_missing_required(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    # Most required artifacts should be missing (no metadata, no Docker).
    missing_req = summary["missing_artifacts"].get("required", [])
    assert len(missing_req) > 0
    # summary.json should NOT be missing (we write it).
    # But run-metadata.json, synapse.log, meshtasticd.log should be.
    assert "run-metadata.json" in missing_req


def test_config_yaml_written_from_env_snapshot(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    # config.yaml should be written from env-based config snapshot.
    run_dir = Path(summary["run_directory"])
    assert (run_dir / "config.yaml").exists()
    assert "config.yaml" in summary["artifact_paths"]


def test_storage_artifact_paths_included(tmp_path: Path) -> None:
    """Paths from storage export are included in artifact_paths."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")

    def _mock_export(run_dir, storage_path, event_id):
        rpath = run_dir / "receipts.json"
        rpath.write_text("[]")
        return {"receipts.json": rpath}

    base_dir = tmp_path / "runs"
    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "storage_path": "/tmp/test.db",
                "event_id": "$evt1",
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=_mock_export,
    )
    assert "receipts.json" in summary["artifact_paths"]


# ---------------------------------------------------------------------------
# Missing artifacts reported honestly
#
# Verify missing required/best-effort artifacts are reported.
# ---------------------------------------------------------------------------


def test_missing_required_artifacts_reported_honestly(tmp_path: Path) -> None:
    """Missing required artifacts appear in missing_artifacts, not errors."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    # Missing required artifacts should appear in missing_artifacts.required.
    missing_req = summary["missing_artifacts"]["required"]
    assert len(missing_req) > 0
    # These should NOT be in the errors list (they are environmental limits).
    for name in missing_req:
        assert not any(
            f"Missing required artifact: {name}" in e for e in summary["errors"]
        ), f"Missing required artifact {name} should be in manifest, not errors"


def test_final_snapshot_limitation_when_missing(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    # final-snapshot.json should be in missing best-effort.
    assert "final-snapshot.json" in summary["missing_artifacts"]["best_effort"]
    # Should appear in medre limitations.
    all_limitations = " ".join(summary["medre"]["limitations"]).lower()
    assert "final-snapshot" in all_limitations


def test_no_false_missing_when_all_present(tmp_path: Path) -> None:
    """When all required artifacts exist, no missing-required errors."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    base_dir = tmp_path / "runs"
    call_count = 0

    def _inject_all_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            # Create all required artifacts.
            for name in ARTIFACT_PLAN["required"]:
                if name == "summary.json":
                    continue  # written by collector later
                (run_dir / name).write_text("{}")
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_all_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    missing_req = summary["missing_artifacts"]["required"]
    assert len(missing_req) == 0


# ---------------------------------------------------------------------------
# Redaction in config.yaml
#
# Verify config.yaml is redacted when written by the collector.
# ---------------------------------------------------------------------------


def test_config_yaml_redacts_secrets(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        extra_env={
            "MEDRE_SYNAPSE_IMAGE": "synapse:test",
            "MEDRE_MESHTASTICD_IMAGE": "meshtasticd:test",
        },
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    run_dir = Path(summary["run_directory"])
    config_path = run_dir / "config.yaml"
    assert config_path.exists()
    content = config_path.read_text()
    # Images should be present (not secrets).
    assert "synapse:test" in content
    assert "meshtasticd:test" in content


def test_config_yaml_from_metadata_redacts(tmp_path: Path) -> None:
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    base_dir = tmp_path / "runs"
    call_count = 0

    def _inject_metadata_now() -> datetime:
        nonlocal call_count
        call_count += 1
        ts = _FIXED_NOW
        if call_count == 1:
            run_dir = base_dir / ts.strftime("%Y-%m-%dT%H-%M-%SZ")
            run_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "config_data": {
                    "homeserver": "https://matrix.org",
                    "access_token": "syt_super_secret_token",
                    "user_id": "@bot:matrix.org",
                },
            }
            (run_dir / "run-metadata.json").write_text(json.dumps(metadata))
        return ts

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    run_dir = Path(summary["run_directory"])
    config_path = run_dir / "config.yaml"
    assert config_path.exists()
    content = config_path.read_text()
    assert "syt_super_secret_token" not in content
    assert "https://matrix.org" in content


# ---------------------------------------------------------------------------
# _format_yaml_value leaf formatting
#
# Cover the string and JSON-fallback branches of _format_yaml_value.
# ---------------------------------------------------------------------------


def test_string_is_double_quoted() -> None:
    assert _format_yaml_value("hello") == '"hello"'


def test_string_escapes_quotes_and_backslashes() -> None:
    # The string scalar escaper doubles backslashes and quotes.
    assert _format_yaml_value('a"b\\c') == '"a\\"b\\\\c"'


def test_list_falls_back_to_json() -> None:
    # Collections that are not nested mappings hit the json.dumps fallback
    # so the file remains valid YAML (YAML is a JSON superset).
    assert _format_yaml_value([1, 2, 3]) == json.dumps([1, 2, 3])


# ---------------------------------------------------------------------------
# Best-effort error capture and artifact-path aggregation
#
# Cover best-effort error capture and artifact-path aggregation branches
# inside collect_docker_bridge_artifacts.
# ---------------------------------------------------------------------------


def test_storage_export_failure_recorded_in_errors(
    tmp_path: Path,
) -> None:
    """A raising _storage_export_fn is caught and reported in errors."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    base_dir = tmp_path / "runs"

    def _failing_export(run_dir, storage_path, event_id):
        raise RuntimeError("export blew up")

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata(base_dir, {"storage_path": "/x.db", "event_id": "$e1"}),
        _run_pytest=mock_runner,
        _storage_export_fn=_failing_export,
    )
    assert any("Storage artifact export failed" in e for e in summary["errors"])
    assert any("export blew up" in e for e in summary["errors"])


def test_config_snapshot_failure_recorded_in_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising _collect_config_snapshot is caught and reported in errors."""
    import medre.runtime.docker_bridge_artifacts as dba

    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")

    def _boom(scenario: str, env: dict[str, str]) -> dict[str, Any]:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(dba, "_collect_config_snapshot", _boom)

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=tmp_path / "runs",
        now_fn=_fixed_now,
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    assert any("Config snapshot collection failed" in e for e in summary["errors"])


def test_log_artifacts_and_config_path_aggregated(
    tmp_path: Path,
) -> None:
    """Log artifacts from metadata and the config.yaml path are both
    aggregated into summary['artifact_paths']."""
    mock_runner = _make_mock_runner(stdout="1 passed in 1s\n")
    base_dir = tmp_path / "runs"
    synapse_log = tmp_path / "src_synapse.log"
    synapse_log.write_text("synapse log lines")

    summary = collect_docker_bridge_artifacts(
        scenario="matrix_to_meshtastic",
        base_dir=base_dir,
        now_fn=_inject_metadata(
            base_dir,
            {
                "log_paths": {"synapse": str(synapse_log)},
                "config_data": {"homeserver": "https://m.org"},
            },
        ),
        _run_pytest=mock_runner,
        _storage_export_fn=lambda rd, sp, eid: {},
    )
    artifact_paths = summary["artifact_paths"]
    # log artifact aggregated into the summary path map
    assert "synapse.log" in artifact_paths
    # config.yaml written from config_data and its path aggregated
    assert "config.yaml" in artifact_paths
    run_dir = Path(summary["run_directory"])
    assert (run_dir / "config.yaml").exists()
