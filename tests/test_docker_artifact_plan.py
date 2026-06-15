"""Tests for medre.runtime.docker_bridge_artifacts — artifact plans and manifest.

Covers TestArtifactPlan, TestGetArtifactPlan, TestCollectArtifactManifest,
and TestCollectArtifactManifestScenarioAware.
"""

from __future__ import annotations

from pathlib import Path

from medre.runtime.docker_bridge_artifacts import (
    ARTIFACT_PLAN,
    SUPPORTED_SCENARIOS,
    _collect_artifact_manifest,
    get_artifact_plan,
)

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
        assert "config.yaml" in required
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
# get_artifact_plan — scenario-aware required lists
# ---------------------------------------------------------------------------


class TestGetArtifactPlan:
    """Validate scenario-aware artifact plans."""

    def test_matrix_to_meshtastic_requires_both_logs(self) -> None:
        plan = get_artifact_plan("matrix_to_meshtastic")
        assert "synapse.log" in plan["required"]
        assert "meshtasticd.log" in plan["required"]

    def test_meshtastic_to_matrix_requires_meshtasticd_log(self) -> None:
        plan = get_artifact_plan("meshtastic_to_matrix")
        assert "meshtasticd.log" in plan["required"]
        assert "synapse.log" not in plan["required"]

    def test_bidirectional_requires_both_logs(self) -> None:
        plan = get_artifact_plan("bidirectional")
        assert "synapse.log" in plan["required"]
        assert "meshtasticd.log" in plan["required"]

    def test_all_scenarios_share_base_required(self) -> None:
        for scenario in SUPPORTED_SCENARIOS:
            plan = get_artifact_plan(scenario)
            for name in ("summary.json", "run-metadata.json", "config.yaml"):
                assert (
                    name in plan["required"]
                ), f"{scenario} missing base required: {name}"

    def test_best_effort_identical_across_scenarios(self) -> None:
        plans = {s: get_artifact_plan(s) for s in SUPPORTED_SCENARIOS}
        best_efforts = [p["best_effort"] for p in plans.values()]
        assert all(b == best_efforts[0] for b in best_efforts)

    def test_best_effort_matches_artifact_plan(self) -> None:
        for scenario in SUPPORTED_SCENARIOS:
            plan = get_artifact_plan(scenario)
            assert plan["best_effort"] == ARTIFACT_PLAN["best_effort"]

    def test_unknown_scenario_returns_both_logs(self) -> None:
        plan = get_artifact_plan("unknown_scenario")
        assert "synapse.log" in plan["required"]
        assert "meshtasticd.log" in plan["required"]

    def test_required_has_no_overlap_with_best_effort(self) -> None:
        for scenario in SUPPORTED_SCENARIOS:
            plan = get_artifact_plan(scenario)
            assert set(plan["required"]).isdisjoint(set(plan["best_effort"]))

    def test_bidirectional_matches_artifact_plan_constant(self) -> None:
        """Bidirectional plan equals the ARTIFACT_PLAN constant."""
        plan = get_artifact_plan("bidirectional")
        assert plan["required"] == ARTIFACT_PLAN["required"]
        assert plan["best_effort"] == ARTIFACT_PLAN["best_effort"]

    def test_matrix_required_count(self) -> None:
        plan = get_artifact_plan("matrix_to_meshtastic")
        assert len(plan["required"]) == 5

    def test_meshtastic_required_count(self) -> None:
        plan = get_artifact_plan("meshtastic_to_matrix")
        assert len(plan["required"]) == 4

    def test_bidirectional_required_count(self) -> None:
        plan = get_artifact_plan("bidirectional")
        assert len(plan["required"]) == 5


# ---------------------------------------------------------------------------
# _collect_artifact_manifest
# ---------------------------------------------------------------------------


class TestCollectArtifactManifest:
    def test_reports_all_missing_in_empty_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty_run"
        run_dir.mkdir()
        manifest = _collect_artifact_manifest(run_dir)
        # summary.json is always assumed present (written by collector).
        expected_missing = [n for n in ARTIFACT_PLAN["required"] if n != "summary.json"]
        assert manifest["missing"]["required"] == expected_missing
        assert set(manifest["missing"]["best_effort"]) == set(
            ARTIFACT_PLAN["best_effort"]
        )

    def test_reports_present_artifacts(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # Create summary.json and config.yaml
        (run_dir / "summary.json").write_text("{}")
        (run_dir / "config.yaml").write_text("key: value")
        (run_dir / "pytest-stdout.log").write_text("stdout")
        (run_dir / "pytest-stderr.log").write_text("stderr")

        manifest = _collect_artifact_manifest(run_dir)
        assert "summary.json" in manifest["artifact_paths"]
        assert "config.yaml" in manifest["artifact_paths"]
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
# _collect_artifact_manifest with scenario
# ---------------------------------------------------------------------------


class TestCollectArtifactManifestScenarioAware:
    """Verify _collect_artifact_manifest respects scenario-specific plans."""

    def test_matrix_scenario_reports_meshtasticd_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "matrix_run"
        run_dir.mkdir()
        # Only synapse.log present (not meshtasticd.log).
        (run_dir / "synapse.log").write_text("synapse log")

        manifest = _collect_artifact_manifest(run_dir, scenario="matrix_to_meshtastic")
        assert "synapse.log" not in manifest["missing"]["required"]
        # meshtasticd.log IS required for matrix scenario (cross-adapter evidence).
        assert "meshtasticd.log" in manifest["missing"]["required"]

    def test_meshtastic_scenario_missing_synapse_not_reported(
        self, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "meshtastic_run"
        run_dir.mkdir()
        # Only meshtasticd.log present (not synapse.log).
        (run_dir / "meshtasticd.log").write_text("meshtasticd log")

        manifest = _collect_artifact_manifest(run_dir, scenario="meshtastic_to_matrix")
        assert "meshtasticd.log" not in manifest["missing"]["required"]
        # synapse.log should NOT be in required at all for meshtastic scenario.
        assert "synapse.log" not in manifest["missing"]["required"]

    def test_bidirectional_reports_both_logs_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "bidirectional_run"
        run_dir.mkdir()

        manifest = _collect_artifact_manifest(run_dir, scenario="bidirectional")
        assert "synapse.log" in manifest["missing"]["required"]
        assert "meshtasticd.log" in manifest["missing"]["required"]

    def test_no_scenario_uses_default_plan(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "default_run"
        run_dir.mkdir()

        manifest = _collect_artifact_manifest(run_dir)
        # Default plan (ARTIFACT_PLAN) requires both logs.
        assert "synapse.log" in manifest["missing"]["required"]
        assert "meshtasticd.log" in manifest["missing"]["required"]

    def test_matrix_scenario_with_all_present_no_missing(self, tmp_path: Path) -> None:
        plan = get_artifact_plan("matrix_to_meshtastic")
        run_dir = tmp_path / "full_matrix"
        run_dir.mkdir()
        for name in plan["required"] + plan["best_effort"]:
            (run_dir / name).write_text("{}")

        manifest = _collect_artifact_manifest(run_dir, scenario="matrix_to_meshtastic")
        assert manifest["missing"]["required"] == []
        assert manifest["missing"]["best_effort"] == []
