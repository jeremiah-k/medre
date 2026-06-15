"""Tests for medre.config.loader.find_config: YAML discovery and
search-order precedence.

Verifies the discovery order:

    explicit > MEDRE_CONFIG > MEDRE_HOME > XDG > local

with ``.yaml`` preferred over ``.yml`` at every step, and ``.toml``
files rejected with the dedicated migration error message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigFileError, ConfigNotFoundError
from medre.config.loader import ConfigSource, find_config

# ---------------------------------------------------------------------------
# Common config text
# ---------------------------------------------------------------------------

_CONFIG_BODY = "runtime:\n  name: discovery\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear config-related env vars for each test."""
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Explicit path
# ---------------------------------------------------------------------------

# --- TestExplicitPathDiscovery: Explicit --config path takes priority over everything. ---


def test_explicit_yaml_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "my.yaml"
    explicit.write_text(_CONFIG_BODY)
    other = tmp_path / "env.yaml"
    other.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_CONFIG", str(other))

    path, source = find_config(str(explicit))
    assert source == ConfigSource.EXPLICIT
    assert path == explicit.resolve()


def test_explicit_yml_accepted(tmp_path: Path) -> None:
    p = tmp_path / "my.yml"
    p.write_text(_CONFIG_BODY)
    path, source = find_config(str(p))
    assert source == ConfigSource.EXPLICIT
    assert path == p.resolve()


def test_explicit_nonexistent_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="not found"):
        find_config(str(tmp_path / "nope.yaml"))


def test_explicit_toml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "old.toml"
    p.write_text("[runtime]\n")
    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(str(p))


# ---------------------------------------------------------------------------
# MEDRE_CONFIG env var
# ---------------------------------------------------------------------------

# --- TestMedreConfigEnvVar: MEDRE_CONFIG environment variable is the second priority. ---


def test_medre_config_env_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "env_config.yaml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_CONFIG
    assert path == cfg.resolve()


def test_medre_config_env_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "env_config.yml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_CONFIG
    assert path == cfg.resolve()


def test_medre_config_env_toml_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "env_config.toml"
    cfg.write_text("[runtime]\n")
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(None)


def test_medre_config_env_nonexistent_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDRE_CONFIG pointing at a missing file does not abort discovery;
    the path is recorded in the checked list and the search continues."""
    monkeypatch.setenv("MEDRE_CONFIG", str(tmp_path / "missing.yaml"))
    # Ensure no other location can supply a config so discovery exhausts.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty_xdg"))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigNotFoundError) as exc_info:
        find_config(None)
    # The checked-list records the attempted MEDRE_CONFIG path.
    assert "MEDRE_CONFIG=" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MEDRE_HOME discovery
# ---------------------------------------------------------------------------

# --- TestMedreHomeDiscovery: $MEDRE_HOME/config.yaml (or .yml) is the third priority. ---


def test_finds_config_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "medre_home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_HOME", str(home))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_HOME
    assert path == cfg


def test_finds_config_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "medre_home"
    home.mkdir()
    cfg = home / "config.yml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_HOME", str(home))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_HOME
    assert path == cfg


def test_prefers_yaml_over_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both .yaml and .yml exist, .yaml wins."""
    home = tmp_path / "medre_home"
    home.mkdir()
    yaml_file = home / "config.yaml"
    yaml_file.write_text("runtime:\n  name: yaml_winner\n")
    yml_file = home / "config.yml"
    yml_file.write_text("runtime:\n  name: yml_loser\n")
    monkeypatch.setenv("MEDRE_HOME", str(home))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_HOME
    assert path == yaml_file


def test_rejects_legacy_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy config.toml in MEDRE_HOME produces a migration error,
    not silent discovery and not a silent 'not found'."""
    home = tmp_path / "medre_home"
    home.mkdir()
    toml_file = home / "config.toml"
    toml_file.write_text("[runtime]\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("MEDRE_HOME", str(home))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no_xdg"))
    monkeypatch.chdir(cwd)

    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(None)


# ---------------------------------------------------------------------------
# XDG discovery
# ---------------------------------------------------------------------------

# --- TestXDGDiscovery: XDG config path ($XDG_CONFIG_HOME/medre/config.yaml) is the fourth. ---


def test_finds_xdg_config_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_config = tmp_path / "xdg" / "medre"
    xdg_config.mkdir(parents=True)
    cfg = xdg_config / "config.yaml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.XDG
    assert path == cfg


def test_finds_xdg_config_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_config = tmp_path / "xdg" / "medre"
    xdg_config.mkdir(parents=True)
    cfg = xdg_config / "config.yml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.XDG
    assert path == cfg


def test_xdg_prefers_yaml_over_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_config = tmp_path / "xdg" / "medre"
    xdg_config.mkdir(parents=True)
    yaml_file = xdg_config / "config.yaml"
    yaml_file.write_text(_CONFIG_BODY)
    yml_file = xdg_config / "config.yml"
    yml_file.write_text(_CONFIG_BODY)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.XDG
    assert path == yaml_file


# ---------------------------------------------------------------------------
# Local ./medre.yaml discovery
# ---------------------------------------------------------------------------

# --- TestLocalDiscovery: ./medre.yaml (or .yml) in the current directory is the last priority. ---


def test_finds_local_medre_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "medre.yaml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no_xdg"))

    path, source = find_config(None)
    assert source == ConfigSource.LOCAL
    assert path == cfg


def test_finds_local_medre_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "medre.yml"
    cfg.write_text(_CONFIG_BODY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no_xdg"))

    path, source = find_config(None)
    assert source == ConfigSource.LOCAL
    assert path == cfg


def test_local_prefers_yaml_over_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_file = tmp_path / "medre.yaml"
    yaml_file.write_text(_CONFIG_BODY)
    yml_file = tmp_path / "medre.yml"
    yml_file.write_text(_CONFIG_BODY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no_xdg"))

    path, source = find_config(None)
    assert source == ConfigSource.LOCAL
    assert path == yaml_file


# ---------------------------------------------------------------------------
# Precedence ordering
# ---------------------------------------------------------------------------

# --- TestPrecedenceOrdering: Verify the full discovery precedence chain. ---


def test_explicit_beats_medre_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_cfg = tmp_path / "env.yaml"
    env_cfg.write_text(_CONFIG_BODY)
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_CONFIG", str(env_cfg))

    path, source = find_config(str(explicit))
    assert source == ConfigSource.EXPLICIT
    assert path == explicit.resolve()


def test_medre_config_beats_medre_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_cfg = tmp_path / "env.yaml"
    env_cfg.write_text(_CONFIG_BODY)
    home = tmp_path / "home"
    home.mkdir()
    home_cfg = home / "config.yaml"
    home_cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_CONFIG", str(env_cfg))
    monkeypatch.setenv("MEDRE_HOME", str(home))

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_CONFIG
    assert path == env_cfg.resolve()


def test_medre_home_beats_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home_cfg = home / "config.yaml"
    home_cfg.write_text(_CONFIG_BODY)
    xdg = tmp_path / "xdg" / "medre"
    xdg.mkdir(parents=True)
    xdg_cfg = xdg / "config.yaml"
    xdg_cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("MEDRE_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.MEDRE_HOME
    assert path == home_cfg


def test_xdg_beats_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg" / "medre"
    xdg.mkdir(parents=True)
    xdg_cfg = xdg / "config.yaml"
    xdg_cfg.write_text(_CONFIG_BODY)
    local_cfg = tmp_path / "medre.yaml"
    local_cfg.write_text(_CONFIG_BODY)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)

    path, source = find_config(None)
    assert source == ConfigSource.XDG
    assert path == xdg_cfg


def test_no_config_anywhere_raises_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty_xdg"))

    with pytest.raises(ConfigNotFoundError):
        find_config(None)


# ---------------------------------------------------------------------------
# Legacy TOML in auto-discovery locations raises a migration error
# ---------------------------------------------------------------------------

# --- TestLegacyTOMLRejected: Legacy TOML files in auto-discovery locations ---
# are rejected with the dedicated migration message, not silently discovered ---
# or silently ignored as "not found".


def test_legacy_toml_in_xdg_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy config.toml in the XDG config dir produces a migration
    error, not silent discovery and not a silent 'not found'."""
    xdg = tmp_path / "xdg" / "medre"
    xdg.mkdir(parents=True)
    toml_cfg = xdg / "config.toml"
    toml_cfg.write_text("[runtime]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)

    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(None)


def test_legacy_local_toml_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy medre.toml in the current working directory produces a
    migration error, not silent discovery and not a silent 'not found'."""
    local_toml = tmp_path / "medre.toml"
    local_toml.write_text("[runtime]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty_xdg"))

    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(None)
