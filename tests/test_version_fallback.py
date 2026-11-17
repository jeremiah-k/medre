"""Installed-version fallbacks report the packaged release version."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from medre.cli.main import _get_version as cli_fallback
from medre.runtime.evidence._helpers import _get_version as evidence_fallback
from medre.runtime.installation_summary import _core_version as startup_fallback


def _packaged_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


@pytest.mark.parametrize(
    "fallback",
    [cli_fallback, evidence_fallback, startup_fallback],
    ids=["cli", "evidence", "startup"],
)
def test_version_fallback_matches_the_packaged_version(fallback, monkeypatch):
    def missing(name: str):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    assert fallback() == _packaged_version()
