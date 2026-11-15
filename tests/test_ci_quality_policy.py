"""CI and local quality use the same committed launcher and Python policy."""

import os
import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def test_ci_uses_the_committed_trunk_toolchain():
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/test-and-coverage.yml").read_text()
    )
    commands = [step.get("run", "") for step in workflow["jobs"]["quality"]["steps"]]
    assert ".trunk/trunk check --all --no-fix --ci" in commands
    assert not any("uvx --from" in command for command in commands)
    # Windows does not expose POSIX executable bits; Linux CI enforces them.
    if os.name == "posix":
        assert (_ROOT / ".trunk/trunk").stat().st_mode & 0o111


def test_trunk_uses_the_authoritative_python_configuration():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    trunk = yaml.safe_load((_ROOT / ".trunk/trunk.yaml").read_text())
    definitions = {item["name"]: item for item in trunk["lint"]["definitions"]}
    for tool in ("black", "ruff", "isort"):
        assert definitions[tool]["direct_configs"] == ["pyproject.toml"]
    assert project["tool"]["black"]["target-version"] == ["py311"]
    assert not (_ROOT / ".trunk/configs/ruff.toml").exists()


def test_frozen_schemas_are_outside_formatter_inputs():
    trunk = yaml.safe_load((_ROOT / ".trunk/trunk.yaml").read_text())
    assert any(
        "ALL" in rule["linters"] and "docs/schemas/**" in rule["paths"]
        for rule in trunk["lint"]["ignore"]
    )
