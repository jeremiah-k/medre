"""Tests for medre.runtime.docker_bridge_artifacts — honesty and scenario-aware reporting.

Covers TestScenarioAwareMissingArtifacts and TestMeshtasticPubsubHonesty.
"""

from __future__ import annotations

from pathlib import Path

from medre.runtime.docker_bridge_artifacts import (
    ARTIFACT_PLAN,
    _build_meshtastic_evidence,
    collect_docker_bridge_artifacts,
    get_artifact_plan,
)
from tests.helpers.docker_artifacts import _fixed_now

# ---------------------------------------------------------------------------
# Scenario-aware missing-artifact reporting in collect_docker_bridge_artifacts
# ---------------------------------------------------------------------------


class TestScenarioAwareMissingArtifacts:
    """Verify summary.json missing-artifact reporting is scenario-accurate."""

    @staticmethod
    def _make_mock_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
        def _runner(cmd, env, timeout, cwd):
            return returncode, stdout, stderr

        return _runner

    def test_matrix_scenario_reports_meshtasticd_missing(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        missing_req = summary["missing_artifacts"]["required"]
        assert "meshtasticd.log" in missing_req
        assert "synapse.log" in missing_req

    def test_meshtastic_scenario_does_not_report_synapse_missing(
        self, tmp_path: Path
    ) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        missing_req = summary["missing_artifacts"]["required"]
        assert "synapse.log" not in missing_req
        assert "meshtasticd.log" in missing_req

    def test_bidirectional_reports_both_missing(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="bidirectional",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        missing_req = summary["missing_artifacts"]["required"]
        assert "synapse.log" in missing_req
        assert "meshtasticd.log" in missing_req

    def test_matrix_scenario_artifact_plan_has_correct_required(
        self, tmp_path: Path
    ) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="matrix_to_meshtastic",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        plan = summary["artifact_plan"]
        assert plan["required"] == get_artifact_plan("matrix_to_meshtastic")["required"]
        assert "synapse.log" in plan["required"]
        assert "meshtasticd.log" in plan["required"]

    def test_meshtastic_scenario_artifact_plan_has_correct_required(
        self, tmp_path: Path
    ) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="meshtastic_to_matrix",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        plan = summary["artifact_plan"]
        assert plan["required"] == get_artifact_plan("meshtastic_to_matrix")["required"]
        assert "meshtasticd.log" in plan["required"]
        assert "synapse.log" not in plan["required"]

    def test_bidirectional_artifact_plan_matches_constant(self, tmp_path: Path) -> None:
        mock_runner = self._make_mock_runner(stdout="1 passed in 1s\n")
        summary = collect_docker_bridge_artifacts(
            scenario="bidirectional",
            base_dir=tmp_path / "runs",
            now_fn=_fixed_now,
            _run_pytest=mock_runner,
            _storage_export_fn=lambda rd, sp, eid: {},
        )
        plan = summary["artifact_plan"]
        assert plan["required"] == ARTIFACT_PLAN["required"]
        assert plan["best_effort"] == ARTIFACT_PLAN["best_effort"]


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
        parsed = {
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "event_ids": [],
        }
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
        parsed = {
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "event_ids": [],
        }
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
        parsed = {
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "event_ids": [],
        }
        evidence = _build_meshtastic_evidence(
            parsed=parsed,
            stdout="some other output\n1 passed in 1s\n",
            scenario="meshtastic_to_matrix",
            env={},
        )
        assert evidence["inbound"]["pubsub_proven"] is False
        assert "simulated_inbound" not in evidence["inbound"]

    def test_simulate_inbound_does_not_prove_pubsub_integration(
        self, tmp_path: Path
    ) -> None:
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
