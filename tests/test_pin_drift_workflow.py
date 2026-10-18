# medre-test-tier: unit
"""Exercise the canary's public failure summary without GitHub services."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _bash() -> str:
    """Resolve a POSIX bash, avoiding the Windows WSL launcher stub."""
    candidate = shutil.which("bash")
    if sys.platform != "win32":
        return candidate or pytest.skip("bash is unavailable")
    # On Windows the first bash on PATH can be the WSL launcher, which
    # fails without an installed distribution; prefer Git's bundled bash.
    git_bash = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Git"
        / "usr"
        / "bin"
        / "bash.exe"
    )
    if git_bash.is_file():
        return str(git_bash)
    if candidate:
        probe = subprocess.run(
            [candidate, "-c", "echo ok"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("no POSIX bash is available on this runner")


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
    completed = subprocess.run(
        [_bash(), "-e", "-o", "pipefail", "-c", summary],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_STEP_SUMMARY": "summary.md"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    text = output.read_text()
    assert ("pins lag adapter default branches" in text) is drift
    assert ("could not complete" in text) is not drift
    if log:
        assert log in text
