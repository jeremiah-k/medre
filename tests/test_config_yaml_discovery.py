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


class TestExplicitPathDiscovery:
    """Explicit --config path takes priority over everything."""

    def test_explicit_yaml_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = tmp_path / "my.yaml"
        explicit.write_text(_CONFIG_BODY)
        other = tmp_path / "env.yaml"
        other.write_text(_CONFIG_BODY)
        monkeypatch.setenv("MEDRE_CONFIG", str(other))

        path, source = find_config(str(explicit))
        assert source == ConfigSource.EXPLICIT
        assert path == explicit.resolve()

    def test_explicit_yml_accepted(self, tmp_path: Path) -> None:
        p = tmp_path / "my.yml"
        p.write_text(_CONFIG_BODY)
        path, source = find_config(str(p))
        assert source == ConfigSource.EXPLICIT
        assert path == p.resolve()

    def test_explicit_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigFileError, match="not found"):
            find_config(str(tmp_path / "nope.yaml"))

    def test_explicit_toml_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "old.toml"
        p.write_text("[runtime]\n")
        with pytest.raises(
            ConfigFileError, match="TOML config files are no longer supported"
        ):
            find_config(str(p))


# ---------------------------------------------------------------------------
# MEDRE_CONFIG env var
# ---------------------------------------------------------------------------


class TestMedreConfigEnvVar:
    """MEDRE_CONFIG environment variable is the second priority."""

    def test_medre_config_env_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "env_config.yaml"
        cfg.write_text(_CONFIG_BODY)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_CONFIG
        assert path == cfg.resolve()

    def test_medre_config_env_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "env_config.yml"
        cfg.write_text(_CONFIG_BODY)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_CONFIG
        assert path == cfg.resolve()

    def test_medre_config_env_toml_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "env_config.toml"
        cfg.write_text("[runtime]\n")
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

        with pytest.raises(
            ConfigFileError, match="TOML config files are no longer supported"
        ):
            find_config(None)


# ---------------------------------------------------------------------------
# MEDRE_HOME discovery
# ---------------------------------------------------------------------------


class TestMedreHomeDiscovery:
    """$MEDRE_HOME/config.yaml (or .yml) is the third priority."""

    def test_finds_config_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "medre_home"
        home.mkdir()
        cfg = home / "config.yaml"
        cfg.write_text(_CONFIG_BODY)
        monkeypatch.setenv("MEDRE_HOME", str(home))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_HOME
        assert path == cfg

    def test_finds_config_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "medre_home"
        home.mkdir()
        cfg = home / "config.yml"
        cfg.write_text(_CONFIG_BODY)
        monkeypatch.setenv("MEDRE_HOME", str(home))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_HOME
        assert path == cfg

    def test_prefers_yaml_over_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_ignores_config_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config.toml in MEDRE_HOME must NOT be discovered."""
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

        with pytest.raises(ConfigNotFoundError):
            find_config(None)


# ---------------------------------------------------------------------------
# XDG discovery
# ---------------------------------------------------------------------------


class TestXDGDiscovery:
    """XDG config path ($XDG_CONFIG_HOME/medre/config.yaml) is the fourth."""

    def test_finds_xdg_config_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_finds_xdg_config_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


class TestLocalDiscovery:
    """./medre.yaml (or .yml) in the current directory is the last priority."""

    def test_finds_local_medre_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def test_finds_local_medre_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


class TestPrecedenceOrdering:
    """Verify the full discovery precedence chain."""

    def test_explicit_beats_medre_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def test_medre_home_beats_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_xdg_beats_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty_xdg"))

        with pytest.raises(ConfigNotFoundError):
            find_config(None)


# ---------------------------------------------------------------------------
# TOML files silently ignored in auto-discovery
# ---------------------------------------------------------------------------


class TestTOMLNotAutoDiscovered:
    """TOML files must not be silently discovered or converted."""

    def test_toml_in_xdg_not_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        xdg = tmp_path / "xdg" / "medre"
        xdg.mkdir(parents=True)
        toml_cfg = xdg / "config.toml"
        toml_cfg.write_text("[runtime]\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)

        with pytest.raises(ConfigNotFoundError):
            find_config(None)

    def test_toml_local_not_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_toml = tmp_path / "medre.toml"
        local_toml.write_text("[runtime]\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty_xdg"))

        with pytest.raises(ConfigNotFoundError):
            find_config(None)
