"""Docker-native replay proof must execute, not pass through skipped tests."""

import ast
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TEST = "tests/native_qualification/test_meshtasticd_exact_target_replay.py"


def _steps():
    workflow = yaml.load(
        (_ROOT / ".github/workflows/docker-integration.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    return workflow["jobs"]["docker-integration"]["steps"]


def test_meshtastic_docker_replay_is_enforced_in_required_job():
    """The module is collected explicitly and skipped execution fails the gate."""
    steps = _steps()
    execute = next(
        step
        for step in steps
        if step.get("name") == "Qualify Meshtastic core-authorized replay"
    )
    assert _TEST in execute["run"]
    assert "-m docker" in execute["run"]
    enforce = next(
        step
        for step in steps
        if step.get("name") == "Enforce Meshtastic replay execution"
    )
    assert enforce["if"] == "always()"
    assert "enforce_test_execution.py" in enforce["run"]
    assert "--expect-tests 5" in enforce["run"]
    assert "--allow-dirty" not in enforce["run"]
    upload = next(
        step
        for step in steps
        if step.get("name") == "Upload Meshtastic SDK-boundary replay result"
    )
    assert upload["if"] == "success()"
    assert upload["with"]["retention-days"] == "90"
    assert "junit.xml" in upload["with"]["path"]


def test_meshtastic_replay_suite_declares_sdk_submission_boundary():
    """Pin the suite's stated evidence boundary, not arbitrary future claims."""
    module = ast.parse((_ROOT / _TEST).read_text())
    declaration = ast.get_docstring(module)
    assert declaration is not None
    assert "local submission evidence" in declaration
    assert "NOT an independently observed daemon" in declaration
    assert "RF recipient delivery" in declaration
    assert "authorization to retry an uncertain send" in declaration
