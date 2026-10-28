"""SDK-free help navigation and explicit bridge-default selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from medre.config.errors import ConfigFileError
from medre.config.loader import ConfigSource, find_config, selected_config_path
from tests.helpers.cli import _run_cli_raw

pytestmark = pytest.mark.usefixtures("isolated_config_env")


def test_selected_config_is_explicit_and_can_be_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    yaml = tmp_path / "other" / "private.yaml"
    yaml.parent.mkdir()
    yaml.write_text("runtime:\n  name: selected\n")
    out, err, code = _run_cli_raw("config", "use", str(yaml))
    assert code == 0, err
    assert "selected" in out
    if sys.platform != "win32":
        assert selected_config_path().stat().st_mode & 0o777 == 0o600
    chosen, source = find_config()
    assert chosen == yaml and source == ConfigSource.SELECTED
    out, err, code = _run_cli_raw("routes", "diagram")
    assert code == 0, err
    assert "No enabled route" in out
    out, _, code = _run_cli_raw("config", "current")
    assert code == 0 and "selected" in out
    # Explicit CLI and environment overrides are never silently shadowed.
    alternate = tmp_path / "override.yaml"
    alternate.write_text("runtime: {}\n")
    assert find_config(alternate)[0] == alternate
    monkeypatch.setenv("MEDRE_CONFIG", str(alternate))
    assert find_config()[0] == alternate
    monkeypatch.delenv("MEDRE_CONFIG")
    out, _, code = _run_cli_raw("config", "unset")
    assert code == 0 and "not deleted" in out
    assert yaml.exists() and not selected_config_path().exists()


def test_stale_or_symlink_default_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    pointer = selected_config_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(tmp_path / "missing.yaml") + "\n")
    with pytest.raises(ConfigFileError, match="Selected config is missing"):
        find_config()
    pointer.unlink()
    if sys.platform != "win32":
        private = tmp_path / "external.txt"
        private.write_text("/tmp/real.yaml\n")
        pointer.symlink_to(private)
        with pytest.raises(ConfigFileError, match="Invalid selected-config pointer"):
            find_config()


def test_explicit_missing_environment_selection_never_runs_another_bridge(
    tmp_path, monkeypatch
):
    from medre.config.loader import remember_config

    saved = tmp_path / "saved.yaml"
    saved.write_text("runtime: {}\n")
    remember_config(saved)
    monkeypatch.setenv("MEDRE_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigFileError, match="MEDRE_CONFIG"):
        find_config()


def test_failed_selection_keeps_the_previous_default(tmp_path):
    from medre.config.loader import remember_config

    saved = tmp_path / "saved.yaml"
    saved.write_text("runtime: {}\n")
    remember_config(saved)
    before = selected_config_path().read_bytes()
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("runtime: [not, a, mapping]\n")
    _, _, code = _run_cli_raw("config", "use", str(invalid))
    assert code != 0
    assert selected_config_path().read_bytes() == before


def test_config_use_explains_when_environment_has_precedence(tmp_path, monkeypatch):
    selected = tmp_path / "selected.yaml"
    selected.write_text("runtime: {}\n")
    override = tmp_path / "environment.yaml"
    override.write_text("runtime: {}\n")
    monkeypatch.setenv("MEDRE_CONFIG", str(override))
    output, error, code = _run_cli_raw("config", "use", str(selected))
    assert code == 0, error
    assert "overrides saved selection" in output and "MEDRE_CONFIG" in output
    assert find_config()[0] == override


def test_config_use_reports_a_saved_selection_even_when_environment_is_broken(
    tmp_path, monkeypatch
):
    selected = tmp_path / "selected.yaml"
    selected.write_text("runtime: {}\n")
    monkeypatch.setenv("MEDRE_CONFIG", str(tmp_path / "missing.yaml"))
    output, error, code = _run_cli_raw("config", "use", str(selected))
    assert code == 0
    assert "selected" in output and "discovery is blocked" in error
    assert selected_config_path().read_text().strip() == str(selected)
    with pytest.raises(ConfigFileError, match="MEDRE_CONFIG"):
        find_config()


@pytest.mark.parametrize(
    "failure", [PermissionError("private"), NotADirectoryError("private")]
)
def test_unreadable_selection_metadata_preserves_the_config_error_contract(
    tmp_path, monkeypatch, failure
):
    from medre.config import loader

    pointer = tmp_path / ".selected-config"
    monkeypatch.setattr(loader, "selected_config_path", lambda: pointer)
    original = Path.lstat

    def lstat(path):
        if path == pointer:
            raise failure
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(
        ConfigFileError, match="Cannot read selected-config pointer"
    ) as caught:
        loader._read_selected_config()
    assert caught.value.__cause__ is failure
