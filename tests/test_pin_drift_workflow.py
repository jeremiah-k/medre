# medre-test-tier: unit
"""Exercise the canary's public failure summary without GitHub services."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "log, drift",
    [
        (None, False),
        ("network or manifest failure", False),
        (
            "ValueError: adapter compatibility pins differ from local checkouts:\nexample pinned a, local b",
            True,
        ),
    ],
)
def test_failed_pin_verification_summarizes_only_observed_drift(tmp_path, log, drift):
    workflow = yaml.safe_load(
        Path(".github/workflows/adapter-pin-drift.yml").read_text()
    )
    summary = next(
        step["run"]
        for step in workflow["jobs"]["pin-drift"]["steps"]
        if step.get("name") == "Summarize verification failures"
    )
    if log is not None:
        (tmp_path / "pin-check.log").write_text(log)
    # Use a relative path understood by both Git Bash and POSIX Bash.
    output = tmp_path / "summary.md"
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", summary],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_STEP_SUMMARY": "summary.md"},
        check=True,
        timeout=15,
    )
    text = output.read_text()
    assert ("pins lag adapter default branches" in text) is drift
    assert ("could not complete" in text) is not drift
    if log:
        assert log in text
