"""Package licensing metadata is independent of optional transport SDKs."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_license_metadata_matches_distributed_text():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert project["license"] == "MIT"
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Jeremiah K." in license_text
    assert "The above copyright notice and this permission notice" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
