"""Native CI proof gates and documentation preserve their actual claim ceilings."""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str):
    return yaml.load(
        (_ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def test_meshcore_replay_is_in_the_required_local_job():
    workflow = _workflow("test-and-coverage.yml")
    assert "transport-local-integration" in workflow["jobs"]["merge-readiness"]["needs"]
    steps = workflow["jobs"]["transport-local-integration"]["steps"]
    execute = next(
        step
        for step in steps
        if step.get("name") == "Qualify core-authorized MeshCore replay"
    )
    assert "matrix.transport == 'meshcore'" in execute["if"]
    assert "tests/integration/test_meshcore_exact_target_replay.py" in execute["run"]
    assert "local_integration and meshcore_sdk" in execute["run"]
    enforce = next(
        step
        for step in steps
        if step.get("name") == "Enforce MeshCore execution and proof provenance"
    )
    assert "always()" in enforce["if"]
    assert "enforce_test_execution.py" in enforce["run"]
    assert "--expect-tests 12" in enforce["run"]
    assert "native_qualification.py" in enforce["run"]
    assert "--expect-records 2" in enforce["run"]
    assert "--allow-dirty" not in enforce["run"]
    upload = next(
        step for step in steps if step.get("name") == "Upload verified MeshCore proof"
    )
    assert "success()" in upload["if"]
    assert upload["with"]["retention-days"] == "90"
    assert "qualification.json" in upload["with"]["path"]


def test_lxmf_stays_opt_in_but_requires_verified_provenance():
    workflow = _workflow("lxmf-core-native-qualification.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    steps = workflow["jobs"]["core-lxmf-replay"]["steps"]
    enforce = next(
        step
        for step in steps
        if step.get("name")
        == "Enforce pinned scenario execution and independent evidence artifacts"
    )
    assert "always()" in enforce["if"]
    assert "native_qualification.py" in enforce["run"]
    assert "--expect-tests 1" in enforce["run"]
    assert "--expect-records 1" in enforce["run"]
    assert "--allow-dirty" not in enforce["run"]
    upload = next(
        step
        for step in steps
        if step.get("name") == "Upload verified recipient evidence"
    )
    assert upload["if"] == "success()"
    assert upload["with"]["retention-days"] == "90"
    assert "qualification.json" in upload["with"]["path"]


def test_native_specs_reconcile_core_replay_and_provider_only_proof():
    native = (_ROOT / "docs/spec/native-evidence.md").read_text()
    recovery = (_ROOT / "docs/spec/exact-target-recovery.md").read_text()
    campaign = (_ROOT / "docs/dev/native-evidence-campaign.md").read_text()
    register = (_ROOT / "docs/dev/contract-gap-status.md").read_text()
    for test in (
        "test_meshcore_exact_target_replay.py",
        "test_lxmf_core_exact_target_replay.py",
    ):
        assert test in native and test in recovery and test in campaign
    assert "LXMF core-authorized native acceptance remains" not in native
    assert "conformance" in native and "Channel `OK`" in native
    assert "opt-in qualification" in register
    assert "Meshtastic" in register and "physical MeshCore" in register
