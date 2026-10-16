"""Keep CI and Trunk's sandbox-compatible Python quality policy aligned."""

import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def test_ci_uses_the_trunk_python_quality_versions():
    trunk = yaml.safe_load((_ROOT / ".trunk/trunk.yaml").read_text())
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/test-and-coverage.yml").read_text()
    )
    commands = [step.get("run", "") for step in workflow["jobs"]["quality"]["steps"]]
    versions = dict(
        item.split("@", 1) for item in trunk["lint"]["enabled"] if "@" in item
    )
    for tool in ("ruff", "black"):
        assert any(
            f"--from {tool}=={versions[tool]} " in command for command in commands
        )


def test_trunk_python_policy_mirrors_the_project():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    trunk = tomllib.loads((_ROOT / ".trunk/configs/ruff.toml").read_text())
    assert trunk == project["tool"]["ruff"]
