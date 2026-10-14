"""Behavioral checks for the Docker runner's selected Python environment."""

from __future__ import annotations

import os
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[1] / "scripts/ci/run-docker-integration.sh"


# This harness exercises a POSIX Bash entry point with executable shell fakes.
# Windows core/runtime coverage runs separately without relying on WSL Bash.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX Docker runner harness (not a Windows entry point)"
)


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
    assert "uv sync --locked --extra dev" in result.stderr
    assert (
        "uv pip install medre-adapter-matrix medre-adapter-meshtastic" in result.stderr
    )
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
    assert "" not in arguments  # An empty pytest path collects the entire checkout.
    assert "pip" not in arguments and "install" not in arguments


def test_docker_runner_forwards_pytest_arguments_without_shell_expansion(
    runner_environment: tuple[dict[str, str], Path],
) -> None:
    env, log = runner_environment
    result = subprocess.run(
        ["bash", str(_RUNNER), "-k", "bridge and not attachment"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().splitlines()[-2:] == ["-k", "bridge and not attachment"]


def test_docker_runner_without_gnu_timeout_uses_pytest_timeout(
    runner_environment: tuple[dict[str, str], Path],
) -> None:
    """macOS has no GNU timeout by default; retain bounded per-test timeouts."""
    env, log = runner_environment
    env["PATH"] = str(Path(env["PYTHON"]).parent)
    shell = shutil.which("bash")
    assert shell is not None
    result = subprocess.run(
        [shell, str(_RUNNER)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--timeout=300" in log.read_text()
    assert "global timeout utility not found" in result.stderr


@pytest.mark.parametrize("scenario", [
    "matrix_to_meshtastic", "meshtastic_to_matrix", "bidirectional",
])
def test_docker_artifact_runner_without_gnu_timeout_uses_collector_deadline(
    runner_environment: tuple[dict[str, str], Path], scenario: str,
) -> None:
    """All scenarios work without coreutils and never reach a catch-all branch."""
    env, log = runner_environment
    env["PATH"] = str(Path(env["PYTHON"]).parent)
    shell = shutil.which("bash")
    assert shell is not None
    artifact_runner = _RUNNER.with_name("run-docker-bridge-artifacts.sh")
    result = subprocess.run(
        [shell, str(artifact_runner), scenario],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "global timeout utility not found" in result.stderr
    assert "collect_docker_bridge_artifacts" in log.read_text()


def test_docker_artifact_timeout_is_validated_before_embedding_in_python(
    runner_environment: tuple[dict[str, str], Path],
) -> None:
    env, log = runner_environment
    env["TIMEOUT_MINUTES"] = "1;__import__('os').system('echo injected')"
    shell = shutil.which("bash")
    assert shell is not None
    artifact_runner = _RUNNER.with_name("run-docker-bridge-artifacts.sh")
    result = subprocess.run(
        [shell, str(artifact_runner)], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "TIMEOUT_MINUTES must be a positive integer" in result.stderr
    assert not log.exists()
