"""Behavioral checks for the Docker runner's selected Python environment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[1] / "scripts/ci/run-docker-integration.sh"


@pytest.fixture
def runner_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    python = tools / "chosen python"
    python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$MEDRE_RUNNER_TEST_LOG"\n'
        'if [ "$1" = "-c" ]; then exit "$MEDRE_RUNNER_TEST_IMPORT_EXIT"; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then exit 0; fi\n'
        "exit 99\n"
    )
    python.chmod(0o755)
    log = tmp_path / "python-arguments"
    env = os.environ.copy()
    env.update(
        PATH=str(tools) + os.pathsep + env["PATH"],
        PYTHON=str(python),
        MEDRE_RUNNER_TEST_LOG=str(log),
        MEDRE_RUNNER_TEST_IMPORT_EXIT="0",
    )
    return env, log


def test_docker_runner_refuses_missing_dependencies_without_installing(
    runner_environment: tuple[dict[str, str], Path],
) -> None:
    env, log = runner_environment
    env["MEDRE_RUNNER_TEST_IMPORT_EXIT"] = "1"
    result = subprocess.run(
        ["bash", str(_RUNNER)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    assert "uv sync --locked --extra dev --extra matrix --extra meshtastic" in result.stderr
    assert "python -m pip install" in result.stderr
    arguments = log.read_text().splitlines()
    assert arguments[0] == "-c"
    assert "pytest" not in arguments[2:]
    assert "pip" not in arguments and "install" not in arguments


def test_docker_runner_uses_the_selected_python_for_prerequisites_and_tests(
    runner_environment: tuple[dict[str, str], Path],
) -> None:
    env, log = runner_environment
    result = subprocess.run(
        ["bash", str(_RUNNER)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = log.read_text().splitlines()
    assert arguments[0] == "-c"
    assert arguments[2:4] == ["-m", "pytest"]
    assert "tests/integration/" in arguments
    assert "pip" not in arguments and "install" not in arguments
