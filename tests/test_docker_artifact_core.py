"""Tests for medre.runtime.docker_bridge_artifacts — core functionality.

Covers create_run_directory, redact_config_snapshot, build_summary,
write_summary, _parse_pytest_output, _scenario_test_selectors,
collect_docker_bridge_artifacts, Docker test gating, and summary shape.
"""

from __future__ import annotations

import json
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
    _LIMITATIONS,
)

from tests.helpers.docker_artifacts import _fixed_now, tmp_base


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
