"""Process-isolated local integration tests for real LXMF/RNS lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.local_integration, pytest.mark.lxmf_sdk]


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, object]:
    workdir = tmp_path / scenario
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.helpers.lxmf_local_probe",
            scenario,
            str(workdir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    prefix = "MEDRE_LOCAL_INTEGRATION_RESULT="
    result_line = next(
        (
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(prefix)
        ),
        None,
    )
    if result_line is None:
        pytest.fail(
            "LXMF local integration probe did not emit a result line\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )
    return json.loads(result_line[len(prefix) :])


def test_real_router_repeated_lifecycle_persistence_and_failure_cleanup(
    tmp_path: Path,
) -> None:
    result = _run_probe(tmp_path, "suite")
    assert result == {
        "callback_count": 0,
        "cycles": 3,
        "stable_destination": True,
        "startup_failure": "LxmfConnectionError",
    }


@pytest.mark.soak
def test_real_router_local_soak_preserves_identity_across_restarts(
    tmp_path: Path,
) -> None:
    result = _run_probe(tmp_path, "soak")
    assert result == {"cycles": 10, "stable_destination": True}
