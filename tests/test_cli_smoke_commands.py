"""Tests for 'medre version' and 'medre paths' commands."""

from __future__ import annotations

import pytest

from tests.helpers.cli import _run_cli


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


class TestVersion:
    """Tests for 'medre version' command."""

    def test_version_output_contains_medre(self) -> None:
        output = _run_cli("version")
        assert "medre" in output

    def test_version_shows_python(self) -> None:
        output = _run_cli("version")
        assert "Python" in output

    def test_version_shows_platform(self) -> None:
        output = _run_cli("version")
        assert "Platform" in output

    def test_version_format(self) -> None:
        """Version output has expected format: medre X.Y.Z"""
        output = _run_cli("version")
        lines = output.strip().splitlines()
        assert lines[0].startswith("medre ")


class TestPaths:
    """Tests for 'medre paths' command."""

    def test_paths_shows_config_file(self) -> None:
        output = _run_cli("paths")
        assert "Config file:" in output

    def test_paths_shows_state_dir(self) -> None:
        output = _run_cli("paths")
        assert "State dir:" in output

    def test_paths_shows_data_dir(self) -> None:
        output = _run_cli("paths")
        assert "Data dir:" in output

    def test_paths_shows_log_dir(self) -> None:
        output = _run_cli("paths")
        assert "Log dir:" in output

    def test_paths_shows_global_db(self) -> None:
        output = _run_cli("paths")
        assert "Global DB:" in output
