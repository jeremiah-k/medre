"""Encrypted outbound acceptance must execute and fail, never xfail or skip."""

import ast
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TEST = "tests/integration/test_synapse_e2ee_outbound_acceptance.py"
_EXPECTED_TESTS = 6


def _steps():
    workflow = yaml.load(
        (_ROOT / ".github/workflows/docker-integration.yml").read_text(),
        Loader=yaml.BaseLoader,
    )  # nosec B506 # BaseLoader constructs strings, not arbitrary Python objects.
    return workflow["jobs"]["docker-integration"]["steps"]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step.get("name") == name)


def _collect_tests() -> int:
    module = ast.parse((_ROOT / _TEST).read_text())
    return sum(
        1
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("test_")
    )


def test_outbound_acceptance_is_enforced_in_docker_job():
    """The module is collected explicitly and skipped execution fails the gate."""
    execute = _step("Qualify Matrix encrypted outbound acceptance")
    assert _TEST in execute["run"]
    assert "-m docker" in execute["run"]
    assert "--junitxml=" in execute["run"]
    enforce = _step("Enforce Matrix outbound acceptance execution")
    assert enforce["if"] == "always()"
    assert "enforce_test_execution.py" in enforce["run"]
    assert f"--expect-tests {_EXPECTED_TESTS}" in enforce["run"]
    upload = _step("Upload Matrix outbound acceptance result")
    assert upload["if"] == "success()"
    assert upload["with"]["retention-days"] == "90"
    assert "junit.xml" in upload["with"]["path"]


def test_outbound_acceptance_runs_once_per_job():
    """The directory lane deselects the module the dedicated gate executes."""
    lane = _step("Run Docker integration tests")["run"]
    assert f"--deselect {_TEST}" in lane


def test_enforced_test_count_matches_the_module():
    """The enforced count is the number of tests the module defines."""
    assert _collect_tests() == _EXPECTED_TESTS


def test_outbound_acceptance_never_xfails_or_skips_inside_a_test():
    """A required proof must fail; no in-test escape hatch may exist."""
    module = ast.parse((_ROOT / _TEST).read_text())
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr in {"xfail", "skip", "importorskip"}
    ]
    assert calls == [], [calls[0].lineno]
    forbidden = []
    for node in ast.walk(module):
        target = node.func if isinstance(node, ast.Call) else node
        if not isinstance(target, ast.Attribute) or target.attr not in {
            "xfail",
            "skip",
        }:
            continue
        base = target.value
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "mark"
            and isinstance(base.value, ast.Name)
            and base.value.id == "pytest"
        ):
            forbidden.append(node.lineno)
    assert not forbidden, forbidden


def test_matrix_failure_still_runs_docker_directory_suite() -> None:
    """A Matrix assertion must not suppress unrelated integration qualification."""
    lane = _step("Run Docker integration tests")
    assert "always()" in lane["if"]
    assert "steps.install-integration.outcome == 'success'" in lane["if"]
    assert (
        _step("Qualify Matrix encrypted outbound acceptance").get("continue-on-error")
        is None
    )


def test_failure_artifacts_include_redacted_synapse_diagnostics() -> None:
    """Keep failure evidence without uploading raw Synapse data directories."""
    env = _step("Qualify Matrix encrypted outbound acceptance")["env"]
    assert env["MEDRE_DOCKER_ARTIFACT_RUN_DIR"].endswith(
        "/.ci-artifacts/matrix-outbound-acceptance"
    )
    path = _step("Upload Matrix outbound acceptance failure diagnostics")["with"][
        "path"
    ]
    assert path == ".ci-artifacts/matrix-outbound-acceptance/"
