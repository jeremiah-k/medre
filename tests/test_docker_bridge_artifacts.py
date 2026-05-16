"""Tests for medre.runtime.docker_bridge_artifacts.

Lightweight tests covering artifact plan/redaction/summary generation without
requiring Docker.  Docker-dependent tests remain gated with the existing
``pytest.mark.docker`` marker and ``MEDRE_SKIP_DOCKER`` convention.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.docker_bridge_artifacts import (
    ARTIFACT_PLAN,
    SUPPORTED_SCENARIOS,
    build_summary,
    collect_docker_bridge_artifacts,
    create_run_directory,
    redact_config_snapshot,
    write_summary,
    _parse_pytest_output,
    _scenario_test_selectors,
    _read_run_metadata,
    _write_redacted_config,
    _collect_log_artifacts,
    _collect_artifact_manifest,
    _build_meshtastic_evidence,
    _LIMITATIONS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    """Provide a temporary base directory for artifact runs."""
    return tmp_path / "bridge-runs"


# ---------------------------------------------------------------------------
# create_run_directory
# ---------------------------------------------------------------------------


class TestCreateRunDirectory:
    def test_creates_timestamped_dir(self, tmp_base: Path) -> None:
        run_dir = create_run_directory(base_dir=tmp_base, now_fn=_fixed_now)
        assert run_dir.is_dir()
        assert "2026-05-16T12-00-00Z" in run_dir.name

    def test_creates_parent_dirs(self, tmp_base: Path) -> None:
        nested = tmp_base / "deep" / "nested"
        run_dir = create_run_directory(base_dir=nested, now_fn=_fixed_now)
        assert run_dir.is_dir()

    def test_deterministic_with_injectable_clock(self, tmp_base: Path) -> None:
        dir1 = create_run_directory(base_dir=tmp_base, now_fn=_fixed_now)
        # Second call with same timestamp should succeed (exist_ok).
        dir2 = create_run_directory(base_dir=tmp_base, now_fn=_fixed_now)
        assert dir1 == dir2


# ---------------------------------------------------------------------------
# redact_config_snapshot
# ---------------------------------------------------------------------------


class TestRedactConfigSnapshot:
    def test_removes_access_token(self) -> None:
        config = {
            "homeserver": "https://matrix.org",
            "access_token": "syt_secret_token_value",
            "user_id": "@bot:matrix.org",
        }
        redacted = redact_config_snapshot(config)
        assert "access_token" not in redacted
        assert redacted["homeserver"] == "https://matrix.org"
        assert redacted["user_id"] == "@bot:matrix.org"

    def test_removes_password(self) -> None:
        config = {"password": "hunter2", "username": "admin"}
        redacted = redact_config_snapshot(config)
        assert "password" not in redacted
        assert redacted["username"] == "admin"

    def test_removes_secret_keys(self) -> None:
        config = {
            "secret_key": "super-secret",
            "private_key": "pk-123",
            "api_key": "ak-456",
            "normal_field": "visible",
        }
        redacted = redact_config_snapshot(config)
        assert "secret_key" not in redacted
        assert "private_key" not in redacted
        assert "api_key" not in redacted
        assert redacted["normal_field"] == "visible"

    def test_preserves_safe_values(self) -> None:
        config = {
            "synapse_image": "matrixdotorg/synapse:v1.149.0",
            "port": 8008,
            "enabled": True,
            "timeout": None,
        }
        redacted = redact_config_snapshot(config)
        assert redacted == config


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_minimal_passed_summary(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            now_fn=_fixed_now,
        )
        assert summary["status"] == "passed"
        assert summary["scenario"] == "matrix_to_meshtastic"
        assert summary["timestamp"] == "2026-05-16T12:00:00+00:00"
        assert summary["matrix"] == {}
        assert summary["meshtastic"] == {}
        assert summary["medre"]["limitations"] == _LIMITATIONS
        assert summary["errors"] == []

    def test_failed_summary_with_errors(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="failed",
            scenario="bidirectional",
            run_directory=tmp_path,
            errors=["Connection refused", "password=admin123 leaked"],
            now_fn=_fixed_now,
        )
        assert summary["status"] == "failed"
        assert summary["scenario"] == "bidirectional"
        # Errors should be sanitized.
        assert len(summary["errors"]) == 2
        # The password error should be sanitized.
        assert "admin123" not in summary["errors"][1]

    def test_partial_status(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="partial",
            scenario="meshtastic_to_matrix",
            run_directory=tmp_path,
            now_fn=_fixed_now,
        )
        assert summary["status"] == "partial"

    def test_matrix_evidence_included(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            matrix={
                "container": "synapse:latest",
                "room": "!test:localhost",
                "event_id": "$event123",
                "ingress_path": "sync_loop",
            },
            now_fn=_fixed_now,
        )
        assert summary["matrix"]["container"] == "synapse:latest"
        assert summary["matrix"]["room"] == "!test:localhost"
        assert summary["matrix"]["event_id"] == "$event123"
        assert summary["matrix"]["ingress_path"] == "sync_loop"

    def test_meshtastic_evidence_included(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="passed",
            scenario="meshtastic_to_matrix",
            run_directory=tmp_path,
            meshtastic={
                "daemon": "meshtasticd:2.7",
                "inbound": {"pubsub_proven": True},
                "outbound": {"packet_ids": ["42", "43"]},
            },
            now_fn=_fixed_now,
        )
        assert summary["meshtastic"]["daemon"] == "meshtasticd:2.7"
        assert summary["meshtastic"]["inbound"]["pubsub_proven"] is True

    def test_medre_evidence_with_limitations(self, tmp_path: Path) -> None:
        custom_limits = ["Custom limitation A", "Custom limitation B"]
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            medre={
                "event_id": "evt-001",
                "limitations": custom_limits,
            },
            now_fn=_fixed_now,
        )
        assert summary["medre"]["limitations"] == custom_limits

    def test_logs_truncated_when_large(self, tmp_path: Path) -> None:
        large_stdout = "x" * (300 * 1024)  # 300 KiB
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            logs={"pytest_stdout": large_stdout, "pytest_stderr": "short"},
            now_fn=_fixed_now,
        )
        assert summary["logs"]["pytest_stdout"] is not None
        assert "[truncated]" in summary["logs"]["pytest_stdout"]
        assert summary["logs"]["pytest_stderr"] == "short"

    def test_config_snapshot_redacted_in_summary(self, tmp_path: Path) -> None:
        config = {
            "synapse_image": "synapse:latest",
            "access_token": "syt_secret123",
        }
        redacted = redact_config_snapshot(config)
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            config_snapshot=redacted,
            now_fn=_fixed_now,
        )
        assert summary["config_snapshot"]["synapse_image"] == "synapse:latest"
        assert "access_token" not in summary["config_snapshot"]

    def test_invalid_scenario_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported scenario"):
            collect_docker_bridge_artifacts(scenario="nonexistent_scenario")

    def test_redacts_token_in_matrix_strings(self, tmp_path: Path) -> None:
        """String values in matrix section containing tokens are sanitized."""
        summary = build_summary(
            status="failed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            matrix={
                "container": "synapse",
                "room": "!room:localhost",
                "event_id": "$evt",
                "ingress_path": "error: token=syt_abc123value happened",
            },
            now_fn=_fixed_now,
        )
        assert "syt_abc123value" not in summary["matrix"]["ingress_path"]


# ---------------------------------------------------------------------------
# write_summary
# ---------------------------------------------------------------------------


class TestWriteSummary:
    def test_writes_json_file(self, tmp_path: Path) -> None:
        summary = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            now_fn=_fixed_now,
        )
        path = write_summary(summary, tmp_path)
        assert path.exists()
        assert path.name == "summary.json"
        loaded = json.loads(path.read_text())
        assert loaded["status"] == "passed"
        assert loaded["scenario"] == "matrix_to_meshtastic"

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir"
        summary = build_summary(
            status="failed",
            scenario="bidirectional",
            run_directory=target,
            errors=["some error"],
            now_fn=_fixed_now,
        )
        path = write_summary(summary, target)
        assert path.exists()

    def test_summary_always_valid_json(self, tmp_path: Path) -> None:
        """Summary is always valid JSON even with unusual data."""
        summary = build_summary(
            status="partial",
            scenario="meshtastic_to_matrix",
            run_directory=tmp_path,
            medre={
                "event_id": None,
                "receipt": None,
                "native_refs": [],
                "runtime": {"passed": 0, "failed": 1, "skipped": 0, "errors": 0},
                "limitations": _LIMITATIONS,
            },
            errors=["Timeout waiting for meshtasticd"],
            now_fn=_fixed_now,
        )
        path = write_summary(summary, tmp_path)
        loaded = json.loads(path.read_text())
        assert loaded["status"] == "partial"
        assert loaded["medre"]["event_id"] is None
        assert len(loaded["errors"]) == 1


# ---------------------------------------------------------------------------
# _parse_pytest_output
# ---------------------------------------------------------------------------


class TestParsePytestOutput:
    def test_extracts_passed_failed_counts(self) -> None:
        stdout = "5 passed, 2 failed, 1 skipped in 10.5s"
        result = _parse_pytest_output(stdout, "")
        assert result["passed_count"] == 5
        assert result["failed_count"] == 2
        assert result["skipped_count"] == 1

    def test_extracts_event_ids(self) -> None:
        stdout = "event $abc123_def sent to room $xyz789_hij"
        result = _parse_pytest_output(stdout, "")
        assert "$abc123_def" in result["event_ids"]
        assert "$xyz789_hij" in result["event_ids"]

    def test_handles_no_results(self) -> None:
        result = _parse_pytest_output("no output", "")
        assert result["passed_count"] == 0
        assert result["failed_count"] == 0
        assert result["event_ids"] == []

    def test_extracts_error_counts(self) -> None:
        stdout = "3 passed, 1 error in 5.0s"
        result = _parse_pytest_output(stdout, "")
        assert result["error_count"] == 1

    def test_deduplicates_event_ids(self) -> None:
        stdout = "$abc $abc $def $abc"
        result = _parse_pytest_output(stdout, "")
        assert len(result["event_ids"]) == 2


# ---------------------------------------------------------------------------
# _scenario_test_selectors
# ---------------------------------------------------------------------------


class TestScenarioTestSelectors:
    def test_matrix_to_meshtastic(self) -> None:
        selectors = _scenario_test_selectors("matrix_to_meshtastic")
        assert any("synapse_bridge_smoke" in s for s in selectors)
        assert any("synapse_connectivity" in s for s in selectors)
        assert any("synapse_run_session" in s for s in selectors)

    def test_meshtastic_to_matrix(self) -> None:
        selectors = _scenario_test_selectors("meshtastic_to_matrix")
        assert any("meshtasticd_connectivity" in s for s in selectors)
        assert any("meshtasticd_sdk_bridge" in s for s in selectors)
        assert not any("synapse" in s for s in selectors)

    def test_bidirectional_includes_both(self) -> None:
        selectors = _scenario_test_selectors("bidirectional")
        assert any("synapse" in s for s in selectors)
        assert any("meshtasticd" in s for s in selectors)

    def test_unknown_returns_empty(self) -> None:
        assert _scenario_test_selectors("unknown") == []


# ---------------------------------------------------------------------------
# collect_docker_bridge_artifacts (mocked pytest)
# ---------------------------------------------------------------------------


class TestCollectDockerBridgeArtifacts:
    """Tests using mocked pytest runner — no Docker required."""

    @staticmethod
    def _make_mock_runner(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ):
        """Create a mock _run_pytest callable."""
        calls: list[dict[str, Any]] = []

        def _runner(cmd, env, timeout, cwd):
            calls.append({"cmd": cmd, "env": env, "timeout": timeout, "cwd": cwd})
            return returncode, stdout, stderr

        return _runner, calls

    def test_passed_run_writes_summary(self, tmp_base: Path) -> None:
        stdout = (
            "test_synapse_connectivity.py::test_connect PASSED\n"
            "1 passed in 10.5s\n"
        )
        mock_runner, _ = self._make_mock_runner(stdout=stdout)

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert summary["status"] == "passed"
        assert summary["scenario"] == "matrix_to_meshtastic"
        assert summary["run_directory"]
        assert summary["errors"] == []

        # Verify summary.json was written.
        run_dir = Path(summary["run_directory"])
        summary_path = run_dir / "summary.json"
        assert summary_path.exists()
        loaded = json.loads(summary_path.read_text())
        assert loaded["status"] == "passed"

    def test_failed_run_still_writes_summary(self, tmp_base: Path) -> None:
        stderr = "ERROR: Docker not available"
        mock_runner, _ = self._make_mock_runner(returncode=1, stderr=stderr)

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert summary["status"] == "failed"
        assert summary["errors"] == []

        # Summary file still exists.
        run_dir = Path(summary["run_directory"])
        assert (run_dir / "summary.json").exists()

    def test_partial_run_has_partial_status(self, tmp_base: Path) -> None:
        stdout = (
            "test_connectivity PASSED\n"
            "test_bridge FAILED\n"
            "1 passed, 1 failed in 20.0s\n"
        )
        mock_runner, _ = self._make_mock_runner(returncode=1, stdout=stdout)

        summary = collect_docker_bridge_artifacts(
            scenario="bidirectional",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert summary["status"] == "partial"

    def test_captures_log_artifacts(self, tmp_base: Path) -> None:
        mock_runner, _ = self._make_mock_runner(
            stdout="stdout content", stderr="stderr content",
        )

        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        run_dir = Path(summary["run_directory"])
        assert (run_dir / "pytest-stdout.log").exists()
        assert (run_dir / "pytest-stderr.log").exists()
        assert (run_dir / "pytest-stdout.log").read_text() == "stdout content"
        assert (run_dir / "pytest-stderr.log").read_text() == "stderr content"

    def test_timeout_produces_failed_summary(self, tmp_base: Path) -> None:
        """When pytest times out, summary should have failed status."""
        def _timeout_runner(cmd, env, timeout, cwd):
            raise TimeoutError("subprocess timed out")

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=_timeout_runner,
        )

        assert summary["status"] == "failed"

    def test_extracts_matrix_evidence_from_output(self, tmp_base: Path) -> None:
        stdout = (
            "ingress_path=sync_loop\n"
            "native_event_id $abc123_synapse\n"
            "!roomid:localhost\n"
            "receipt_status='sent'\n"
            "1 passed in 10.0s\n"
        )
        mock_runner, _ = self._make_mock_runner(stdout=stdout)

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert summary["matrix"]["ingress_path"] == "sync_loop"
        assert summary["matrix"]["event_id"] is not None
        assert summary["matrix"]["room"] is not None

    def test_extracts_fallback_ingress_path(self, tmp_base: Path) -> None:
        stdout = (
            "direct _on_room_message fallback\n"
            "1 passed in 10.0s\n"
        )
        mock_runner, _ = self._make_mock_runner(stdout=stdout)

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert summary["matrix"]["ingress_path"] == "direct_on_room_message_fallback"

    def test_extracts_meshtastic_outbound(self, tmp_base: Path) -> None:
        stdout = (
            "packet_id=42\n"
            "packet_id: 99\n"
            "1 passed in 10.0s\n"
        )
        mock_runner, _ = self._make_mock_runner(stdout=stdout)

        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        meshtastic_out = summary["meshtastic"].get("outbound")
        assert meshtastic_out is not None
        assert "42" in meshtastic_out["packet_ids"]
        assert "99" in meshtastic_out["packet_ids"]

    def test_bidirectional_includes_both_selectors(self, tmp_base: Path) -> None:
        mock_runner, calls = self._make_mock_runner()

        collect_docker_bridge_artifacts(
            scenario="bidirectional",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )

        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        cmd_str = " ".join(cmd)
        assert "synapse" in cmd_str or "meshtastic" in cmd_str

    def test_config_snapshot_included(self, tmp_base: Path) -> None:
        mock_runner, _ = self._make_mock_runner()
        env = {
            "MEDRE_SYNAPSE_IMAGE": "custom/synapse:latest",
            "MEDRE_MESHTASTICD_IMAGE": "custom/meshtasticd:latest",
        }

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            extra_env=env,
            _run_pytest=mock_runner,
        )

        assert summary["config_snapshot"] is not None
        assert summary["config_snapshot"]["synapse_image"] == "custom/synapse:latest"

    def test_all_scenarios_are_valid(self, tmp_base: Path) -> None:
        """Every supported scenario can be run without error."""
        mock_runner, _ = self._make_mock_runner()
        for scenario in SUPPORTED_SCENARIOS:
            summary = collect_docker_bridge_artifacts(
                scenario=scenario,
                base_dir=tmp_base,
                now_fn=_fixed_now,
                _run_pytest=mock_runner,
            )
            assert summary["scenario"] == scenario
            assert summary["status"] in ("passed", "failed", "partial")

    def test_file_not_found_produces_failed(self, tmp_base: Path) -> None:
        """When pytest binary is missing, summary should still be written."""
        def _missing_runner(cmd, env, timeout, cwd):
            raise FileNotFoundError("python not found")

        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_base,
            now_fn=_fixed_now,
            _run_pytest=_missing_runner,
        )

        assert summary["status"] == "failed"
        assert len(summary["errors"]) > 0


# ---------------------------------------------------------------------------
# Docker test gating
# ---------------------------------------------------------------------------


class TestDockerTestGating:
    """Verify Docker-marked tests are properly gated."""

    def test_supported_scenarios_constant(self) -> None:
        assert "matrix_to_meshtastic" in SUPPORTED_SCENARIOS
        assert "meshtastic_to_matrix" in SUPPORTED_SCENARIOS
        assert "bidirectional" in SUPPORTED_SCENARIOS

    def test_limitations_are_honest(self) -> None:
        """Limitations explicitly state no real external service proof."""
        limitations_text = " ".join(_LIMITATIONS).lower()
        assert "real" in limitations_text or "not" in limitations_text
        # Must mention localhost/Docker limitation.
        assert "docker" in limitations_text or "localhost" in limitations_text
        # Must mention no real radio proof.
        assert "radio" in limitations_text or "real" in limitations_text


# ---------------------------------------------------------------------------
# Summary JSON shape validation
# ---------------------------------------------------------------------------


class TestSummaryShape:
    """Validate summary.json has required fields per the specification."""

    def _full_summary(self, tmp_path: Path) -> dict[str, Any]:
        return build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            matrix={
                "container": "synapse:v1",
                "room": "!room:localhost",
                "event_id": "$evt123",
                "ingress_path": "sync_loop",
            },
            meshtastic={
                "daemon": "meshtasticd:2.7",
                "inbound": {"pubsub_proven": True},
                "outbound": {"packet_ids": ["42"]},
            },
            medre={
                "event_id": "medre-evt-001",
                "receipt": {"status": "sent"},
                "native_refs": [{"adapter": "matrix", "native_id": "$evt123"}],
                "runtime": {"passed": 3, "failed": 0, "skipped": 0, "errors": 0},
                "limitations": _LIMITATIONS,
            },
            logs={"pytest_stdout": "output", "pytest_stderr": ""},
            config_snapshot={"synapse_image": "synapse"},
            inspect_artifacts=["/path/to/artifact.log"],
            errors=[],
            now_fn=_fixed_now,
        )

    def test_top_level_fields(self, tmp_path: Path) -> None:
        s = self._full_summary(tmp_path)
        for field in ("status", "scenario", "timestamp", "run_directory"):
            assert field in s, f"Missing top-level field: {field}"

    def test_matrix_fields(self, tmp_path: Path) -> None:
        s = self._full_summary(tmp_path)
        for field in ("container", "room", "event_id", "ingress_path"):
            assert field in s["matrix"], f"Missing matrix field: {field}"

    def test_meshtastic_fields(self, tmp_path: Path) -> None:
        s = self._full_summary(tmp_path)
        for field in ("daemon", "inbound", "outbound"):
            assert field in s["meshtastic"], f"Missing meshtastic field: {field}"

    def test_medre_fields(self, tmp_path: Path) -> None:
        s = self._full_summary(tmp_path)
        for field in ("event_id", "receipt", "native_refs", "runtime", "limitations"):
            assert field in s["medre"], f"Missing medre field: {field}"

    def test_is_json_serializable(self, tmp_path: Path) -> None:
        s = self._full_summary(tmp_path)
        # Should not raise.
        text = json.dumps(s, indent=2, sort_keys=True, default=str)
        assert len(text) > 100  # sanity: non-trivial content

    def test_failed_summary_still_has_required_shape(self, tmp_path: Path) -> None:
        s = build_summary(
            status="failed",
            scenario="bidirectional",
            run_directory=tmp_path,
            errors=["Something went wrong"],
            now_fn=_fixed_now,
        )
        # All top-level sections should exist even on failure.
        assert "matrix" in s
        assert "meshtastic" in s
        assert "medre" in s
        assert "errors" in s
        assert s["status"] == "failed"
        assert s["medre"]["limitations"]  # limitations always present
        # New artifact fields should be present even on failure.
        assert "artifact_plan" in s
        assert "artifact_paths" in s
        assert "missing_artifacts" in s

    def test_artifact_plan_defaults_to_module_constant(self, tmp_path: Path) -> None:
        s = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            now_fn=_fixed_now,
        )
        assert s["artifact_plan"] == ARTIFACT_PLAN
        assert s["artifact_plan"]["required"] == ARTIFACT_PLAN["required"]
        assert s["artifact_plan"]["best_effort"] == ARTIFACT_PLAN["best_effort"]

    def test_artifact_paths_and_missing_default_empty(self, tmp_path: Path) -> None:
        s = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            now_fn=_fixed_now,
        )
        assert s["artifact_paths"] == {}
        assert s["missing_artifacts"] == {}

    def test_artifact_fields_passed_through(self, tmp_path: Path) -> None:
        s = build_summary(
            status="passed",
            scenario="matrix_to_meshtastic",
            run_directory=tmp_path,
            artifact_paths={"summary.json": "/tmp/summary.json"},
            missing_artifacts={"required": ["synapse.log"], "best_effort": []},
            now_fn=_fixed_now,
        )
        assert s["artifact_paths"]["summary.json"] == "/tmp/summary.json"
        assert "synapse.log" in s["missing_artifacts"]["required"]


# ---------------------------------------------------------------------------
# ARTIFACT_PLAN structure
# ---------------------------------------------------------------------------


class TestArtifactPlan:
    """Validate the ARTIFACT_PLAN constant."""

    def test_has_required_key(self) -> None:
        assert "required" in ARTIFACT_PLAN

    def test_has_best_effort_key(self) -> None:
        assert "best_effort" in ARTIFACT_PLAN

    def test_required_contains_expected_files(self) -> None:
        required = ARTIFACT_PLAN["required"]
        assert "summary.json" in required
        assert "run-metadata.json" in required
        assert "config.toml" in required
        assert "synapse.log" in required
        assert "meshtasticd.log" in required

    def test_best_effort_contains_expected_files(self) -> None:
        best = ARTIFACT_PLAN["best_effort"]
        assert "medre.log" in best
        assert "receipts.json" in best
        assert "native-refs.json" in best
        assert "inspect-timeline.json" in best
        assert "evidence.json" in best
        assert "final-snapshot.json" in best

    def test_no_overlap_between_required_and_best_effort(self) -> None:
        required = set(ARTIFACT_PLAN["required"])
        best = set(ARTIFACT_PLAN["best_effort"])
        assert required.isdisjoint(best)


# ---------------------------------------------------------------------------
# _read_run_metadata
# ---------------------------------------------------------------------------


class TestReadRunMetadata:
    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        result = _read_run_metadata(tmp_path)
        assert result is None

    def test_reads_valid_json(self, tmp_path: Path) -> None:
        metadata = {"event_id": "$abc", "storage_path": "/tmp/db.sqlite"}
        (tmp_path / "run-metadata.json").write_text(json.dumps(metadata))
        result = _read_run_metadata(tmp_path)
        assert result is not None
        assert result["event_id"] == "$abc"
        assert result["storage_path"] == "/tmp/db.sqlite"

    def test_returns_none_on_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / "run-metadata.json").write_text("not valid json {{{")
        result = _read_run_metadata(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _write_redacted_config
# ---------------------------------------------------------------------------


class TestWriteRedactedConfig:
    def test_writes_redacted_toml(self, tmp_path: Path) -> None:
        config = {
            "synapse_image": "synapse:latest",
            "access_token": "syt_secret123",
            "port": 8008,
        }
        result = _write_redacted_config(tmp_path, config)
        assert result is not None
        assert result.name == "config.toml"
        content = result.read_text()
        assert "synapse:latest" in content
        assert "access_token" not in content
        assert "8008" in content

    def test_handles_nested_values(self, tmp_path: Path) -> None:
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

    def test_handles_null_values(self, tmp_path: Path) -> None:
        config = {"timeout": None, "name": "test"}
        result = _write_redacted_config(tmp_path, config)
        assert result is not None
        content = result.read_text()
        assert "timeout" in content  # present as comment
        assert "test" in content


# ---------------------------------------------------------------------------
# _collect_log_artifacts
# ---------------------------------------------------------------------------


class TestCollectLogArtifacts:
    def test_returns_empty_when_no_metadata(self, tmp_path: Path) -> None:
        result = _collect_log_artifacts(tmp_path, None)
        assert result == {}

    def test_copies_referenced_logs(self, tmp_path: Path) -> None:
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

    def test_skips_missing_source_files(self, tmp_path: Path) -> None:
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
# _collect_artifact_manifest
# ---------------------------------------------------------------------------


class TestCollectArtifactManifest:
    def test_reports_all_missing_in_empty_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty_run"
        run_dir.mkdir()
        manifest = _collect_artifact_manifest(run_dir)
        # summary.json is always assumed present (written by collector).
        expected_missing = [
            n for n in ARTIFACT_PLAN["required"] if n != "summary.json"
        ]
        assert manifest["missing"]["required"] == expected_missing
        assert set(manifest["missing"]["best_effort"]) == set(
            ARTIFACT_PLAN["best_effort"]
        )

    def test_reports_present_artifacts(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # Create summary.json and config.toml
        (run_dir / "summary.json").write_text("{}")
        (run_dir / "config.toml").write_text("key = 'value'")
        (run_dir / "pytest-stdout.log").write_text("stdout")
        (run_dir / "pytest-stderr.log").write_text("stderr")

        manifest = _collect_artifact_manifest(run_dir)
        assert "summary.json" in manifest["artifact_paths"]
        assert "config.toml" in manifest["artifact_paths"]
        assert "pytest-stdout.log" in manifest["artifact_paths"]
        assert "pytest-stderr.log" in manifest["artifact_paths"]

        # Missing required (not created above)
        assert "run-metadata.json" in manifest["missing"]["required"]
        assert "synapse.log" in manifest["missing"]["required"]
        assert "meshtasticd.log" in manifest["missing"]["required"]

    def test_all_present_no_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "full_run"
        run_dir.mkdir()
        for name in ARTIFACT_PLAN["required"] + ARTIFACT_PLAN["best_effort"]:
            (run_dir / name).write_text("{}")

        manifest = _collect_artifact_manifest(run_dir)
        assert manifest["missing"]["required"] == []
        assert manifest["missing"]["best_effort"] == []


# ---------------------------------------------------------------------------
# Structured metadata precedence in collect_docker_bridge_artifacts
# ---------------------------------------------------------------------------


class TestStructuredMetadataPrecedence:
    """Verify structured metadata overrides regex-parsed evidence."""

    @staticmethod
    def _make_mock_runner(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr
        return _runner

    def test_metadata_overrides_matrix_evidence(self, tmp_path: Path) -> None:
        """Structured metadata event_id takes precedence over regex."""
        stdout = "ingress_path=sync_loop $regex_event_id 1 passed in 1s\n"
        mock_runner = self._make_mock_runner(stdout=stdout)

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

    def test_no_metadata_falls_back_to_regex(self, tmp_path: Path) -> None:
        """Without metadata, regex parsing still works (deprecated)."""
        stdout = "ingress_path=sync_loop $regex_event_id 1 passed in 1s\n"
        mock_runner = self._make_mock_runner(stdout=stdout)

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

    def test_metadata_overrides_medre_evidence(self, tmp_path: Path) -> None:
        """Structured medre metadata overrides regex receipt parsing."""
        stdout = "receipt_status='sent' $evt123 1 passed in 1s\n"
        mock_runner = self._make_mock_runner(stdout=stdout)

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

    def test_metadata_provides_meshtastic_data(self, tmp_path: Path) -> None:
        """Structured meshtastic metadata overrides regex parsing."""
        stdout = "packet_id=42 1 passed in 1s\n"
        mock_runner = self._make_mock_runner(stdout=stdout)

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

    def test_storage_export_fn_called_with_metadata(self, tmp_path: Path) -> None:
        """When metadata has storage_path + event_id, export fn is called."""
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        base_dir = tmp_path / "runs"
        export_calls: list[dict[str, Any]] = []

        def _mock_export(run_dir, storage_path, event_id):
            export_calls.append({
                "run_dir": run_dir,
                "storage_path": storage_path,
                "event_id": event_id,
            })
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
# ---------------------------------------------------------------------------


class TestArtifactPathsInSummary:
    """Verify artifact paths appear in the summary after collection."""

    @staticmethod
    def _make_mock_runner(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr
        return _runner

    def test_summary_has_artifact_plan(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        assert summary["artifact_plan"]["required"] == ARTIFACT_PLAN["required"]
        assert summary["artifact_plan"]["best_effort"] == ARTIFACT_PLAN["best_effort"]

    def test_summary_has_artifact_paths(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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

    def test_summary_reports_missing_required(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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

    def test_config_toml_written_from_env_snapshot(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        # config.toml should be written from env-based config snapshot.
        run_dir = Path(summary["run_directory"])
        assert (run_dir / "config.toml").exists()
        assert "config.toml" in summary["artifact_paths"]

    def test_storage_artifact_paths_included(self, tmp_path: Path) -> None:
        """Paths from storage export are included in artifact_paths."""
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")

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
# ---------------------------------------------------------------------------


class TestMissingArtifactsReported:
    """Verify missing required/best-effort artifacts are reported."""

    @staticmethod
    def _make_mock_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr
        return _runner

    def test_missing_required_artifacts_reported_honestly(self, tmp_path: Path) -> None:
        """Missing required artifacts appear in missing_artifacts, not errors."""
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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
                f"Missing required artifact: {name}" in e
                for e in summary["errors"]
            ), f"Missing required artifact {name} should be in manifest, not errors"

    def test_final_snapshot_limitation_when_missing(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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

    def test_no_false_missing_when_all_present(self, tmp_path: Path) -> None:
        """When all required artifacts exist, no missing-required errors."""
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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
# Redaction in config.toml
# ---------------------------------------------------------------------------


class TestConfigTomlRedaction:
    """Verify config.toml is redacted when written by the collector."""

    @staticmethod
    def _make_mock_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr
        return _runner

    def test_config_toml_redacts_secrets(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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
        config_path = run_dir / "config.toml"
        assert config_path.exists()
        content = config_path.read_text()
        # Images should be present (not secrets).
        assert "synapse:test" in content
        assert "meshtasticd:test" in content

    def test_config_toml_from_metadata_redacts(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
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
        config_path = run_dir / "config.toml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "syt_super_secret_token" not in content
        assert "https://matrix.org" in content


# ---------------------------------------------------------------------------
# _build_meshtastic_evidence honesty
# ---------------------------------------------------------------------------


class TestMeshtasticPubsubHonesty:
    """Verify pubsub_proven is honest — simulate_inbound does NOT prove pubsub."""

    @staticmethod
    def _make_mock_runner(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr
        return _runner

    def test_simulate_inbound_does_not_prove_pubsub_unit(self) -> None:
        """_build_meshtastic_evidence must not set pubsub_proven for simulate_inbound."""
        parsed = {"passed_count": 1, "failed_count": 0, "skipped_count": 0, "error_count": 0, "event_ids": []}
        evidence = _build_meshtastic_evidence(
            parsed=parsed,
            stdout="simulate_inbound completed\n1 passed in 1s\n",
            scenario="meshtastic_to_matrix",
            env={},
        )
        assert evidence["inbound"]["pubsub_proven"] is False
        assert evidence["inbound"].get("simulated_inbound") is True

    def test_real_pubsub_still_proven(self) -> None:
        """Real pubsub callback evidence still sets pubsub_proven True."""
        parsed = {"passed_count": 1, "failed_count": 0, "skipped_count": 0, "error_count": 0, "event_ids": []}
        evidence = _build_meshtastic_evidence(
            parsed=parsed,
            stdout="pubsub callback received packet\n1 passed in 1s\n",
            scenario="meshtastic_to_matrix",
            env={},
        )
        assert evidence["inbound"]["pubsub_proven"] is True
        # simulated_inbound should NOT appear when real pubsub is present.
        assert "simulated_inbound" not in evidence["inbound"]

    def test_neither_signal_means_no_pubsub(self) -> None:
        """No pubsub or simulate_inbound → pubsub_proven False, no simulated key."""
        parsed = {"passed_count": 1, "failed_count": 0, "skipped_count": 0, "error_count": 0, "event_ids": []}
        evidence = _build_meshtastic_evidence(
            parsed=parsed,
            stdout="some other output\n1 passed in 1s\n",
            scenario="meshtastic_to_matrix",
            env={},
        )
        assert evidence["inbound"]["pubsub_proven"] is False
        assert "simulated_inbound" not in evidence["inbound"]

    def test_simulate_inbound_does_not_prove_pubsub_integration(self, tmp_path: Path) -> None:
        """End-to-end: simulate_inbound in stdout does not prove pubsub in summary."""
        mock_runner = self._make_mock_runner(
            stdout="simulate_inbound completed\n1 passed in 10.0s\n",
        )
        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )
        assert summary["meshtastic"]["inbound"]["pubsub_proven"] is False
        assert summary["meshtastic"]["inbound"].get("simulated_inbound") is True

    def test_both_pubsub_and_simulate_shows_pubsub_proven(self, tmp_path: Path) -> None:
        """If both signals present (unlikely but honest), pubsub_proven is True."""
        mock_runner = self._make_mock_runner(
            stdout="pubsub callback received simulate_inbound\n1 passed in 1s\n",
        )
        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
        )
        assert summary["meshtastic"]["inbound"]["pubsub_proven"] is True
        # simulated_inbound only appears when pubsub_proven is False
        assert "simulated_inbound" not in summary["meshtastic"]["inbound"]
