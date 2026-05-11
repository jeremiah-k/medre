"""Tests for medre.config.paths: XDG defaults, MEDRE_HOME override,
no directory creation, placeholder expansion, diagnostic output."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.paths import MedrePaths, MedrePathsError, resolve


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Env vars that resolve() reads.  We must clean them all to get a
# deterministic baseline.
_PATH_ENV_VARS: tuple[str, ...] = (
    "MEDRE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no path-related env vars leak between tests."""
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# XDG defaults
# ---------------------------------------------------------------------------


class TestXDGDefaults:
    """When no env vars are set, resolve() follows XDG fallback paths."""

    def test_config_dir_under_xdg_config(self) -> None:
        paths = resolve()
        assert paths.config_dir is not None
        assert paths.config_dir == Path.home() / ".config" / "medre"

    def test_config_file_inside_config_dir(self) -> None:
        paths = resolve()
        assert paths.config_dir is not None
        assert paths.config_file == paths.config_dir / "config.toml"

    def test_state_dir_under_xdg_state(self) -> None:
        paths = resolve()
        assert paths.state_dir == Path.home() / ".local" / "state" / "medre"

    def test_data_dir_under_xdg_data(self) -> None:
        paths = resolve()
        assert paths.data_dir == Path.home() / ".local" / "share" / "medre"

    def test_cache_dir_under_xdg_cache(self) -> None:
        paths = resolve()
        assert paths.cache_dir == Path.home() / ".cache" / "medre"

    def test_log_dir_under_state(self) -> None:
        paths = resolve()
        assert paths.log_dir == paths.state_dir / "logs"

    def test_database_path_under_state(self) -> None:
        paths = resolve()
        assert paths.database_path == paths.state_dir / "medre.sqlite"

    def test_matrix_store_path_under_state(self) -> None:
        paths = resolve()
        assert paths.matrix_store_path == paths.state_dir / "matrix" / "store"


# ---------------------------------------------------------------------------
# XDG env var overrides
# ---------------------------------------------------------------------------


class TestXDGEnvOverrides:
    """XDG_*_HOME env vars override the default fallback directories."""

    def test_config_dir_respects_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
        paths = resolve()
        assert paths.config_dir == Path("/tmp/xdg-config/medre")

    def test_state_dir_respects_xdg_state_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
        paths = resolve()
        assert paths.state_dir == Path("/tmp/xdg-state/medre")

    def test_data_dir_respects_xdg_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
        paths = resolve()
        assert paths.data_dir == Path("/tmp/xdg-data/medre")

    def test_cache_dir_respects_xdg_cache_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
        paths = resolve()
        assert paths.cache_dir == Path("/tmp/xdg-cache/medre")

    def test_log_dir_follows_custom_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
        paths = resolve()
        assert paths.log_dir == Path("/tmp/xdg-state/medre/logs")


# ---------------------------------------------------------------------------
# MEDRE_HOME mode
# ---------------------------------------------------------------------------


class TestMedreHomeMode:
    """When MEDRE_HOME is set, all paths collapse under that root."""

    def test_all_paths_under_medre_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()

        assert paths.config_dir is None
        assert paths.config_file == Path("/opt/medre/config.toml")
        assert paths.state_dir == Path("/opt/medre/state")
        assert paths.data_dir == Path("/opt/medre/data")
        assert paths.cache_dir == Path("/opt/medre/cache")
        assert paths.log_dir == Path("/opt/medre/logs")
        assert paths.database_path == Path("/opt/medre/state/medre.sqlite")
        assert paths.matrix_store_path == Path("/opt/medre/state/matrix/store")

    def test_medre_home_overrides_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEDRE_HOME takes precedence over XDG env vars."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
        paths = resolve()

        # MEDRE_HOME wins
        assert paths.config_dir is None
        assert paths.state_dir == Path("/opt/medre/state")

    def test_medre_home_empty_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_HOME", "")
        paths = resolve()
        # Should fall back to XDG defaults
        assert paths.config_dir is not None

    def test_medre_home_whitespace_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_HOME", "   ")
        paths = resolve()
        assert paths.config_dir is not None


# ---------------------------------------------------------------------------
# No directory creation
# ---------------------------------------------------------------------------


class TestNoDirectoryCreation:
    """resolve() performs pure path resolution — no filesystem side effects."""

    def test_nonexistent_paths_not_created(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Point MEDRE_HOME at a non-existent directory; resolve() must not create it."""
        fake_home = tmp_path / "nonexistent_medre_home"
        monkeypatch.setenv("MEDRE_HOME", str(fake_home))
        paths = resolve()

        assert paths.state_dir == fake_home / "state"
        assert not fake_home.exists(), "resolve() must not create directories"

    def test_xdg_nonexistent_not_created(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
        paths = resolve()
        assert not (tmp_path / "nope").exists()


# ---------------------------------------------------------------------------
# Placeholder expansion
# ---------------------------------------------------------------------------


class TestExpandPlaceholder:
    """expand_placeholder resolves {name} tokens to absolute paths."""

    @pytest.fixture()
    def xdg_paths(self) -> MedrePaths:
        return resolve()

    @pytest.fixture()
    def medre_home_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MedrePaths:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        return resolve()

    def test_state_placeholder(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("{state}/foo.db")
        assert result == xdg_paths.state_dir / "foo.db"

    def test_data_placeholder(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("{data}/stuff")
        assert result == xdg_paths.data_dir / "stuff"

    def test_config_placeholder(self, xdg_paths: MedrePaths) -> None:
        assert xdg_paths.config_dir is not None
        result = xdg_paths.expand_placeholder("{config}/extra.toml")
        assert result == xdg_paths.config_dir / "extra.toml"

    def test_cache_placeholder(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("{cache}/tmp")
        assert result == xdg_paths.cache_dir / "tmp"

    def test_logs_placeholder(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("{logs}/app.log")
        assert result == xdg_paths.log_dir / "app.log"

    def test_config_placeholder_in_medre_home_mode(self, medre_home_paths: MedrePaths) -> None:
        """In MEDRE_HOME mode, {config} resolves to config_file.parent."""
        result = medre_home_paths.expand_placeholder("{config}/extra.toml")
        assert result == medre_home_paths.config_file.parent / "extra.toml"

    def test_multiple_placeholders(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("{state}/{data}")
        # Both placeholders are expanded — the result is a Path
        assert str(xdg_paths.state_dir) in str(result)
        assert str(xdg_paths.data_dir) in str(result)

    def test_unknown_placeholder_raises(self, xdg_paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="unknown path placeholder"):
            xdg_paths.expand_placeholder("{unknown}/path")

    def test_no_placeholders_passes_through(self, xdg_paths: MedrePaths) -> None:
        result = xdg_paths.expand_placeholder("/absolute/path/file.db")
        assert result == Path("/absolute/path/file.db")


# ---------------------------------------------------------------------------
# adapter_state_dir
# ---------------------------------------------------------------------------


class TestAdapterStateDir:
    """adapter_state_dir returns state_dir/adapters/{adapter_id}."""

    @pytest.fixture()
    def paths(self) -> MedrePaths:
        return resolve()

    def test_basic_adapter_state_dir(self, paths: MedrePaths) -> None:
        result = paths.adapter_state_dir("matrix_main")
        assert result == paths.state_dir / "adapters" / "matrix_main"

    def test_adapter_state_dir_nested_looks_correct(self, paths: MedrePaths) -> None:
        result = paths.adapter_state_dir("my-adapter")
        assert result == paths.state_dir / "adapters" / "my-adapter"

    def test_empty_adapter_id_raises(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="non-empty"):
            paths.adapter_state_dir("")

    def test_slash_in_adapter_id_raises(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="path separators"):
            paths.adapter_state_dir("bad/adapter")

    @pytest.mark.skipif(os.sep != "\\", reason="Windows-specific separator test")
    def test_backslash_in_adapter_id_raises_windows(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="path separators"):
            paths.adapter_state_dir("bad\\adapter")


# ---------------------------------------------------------------------------
# adapter_transport_state_dir
# ---------------------------------------------------------------------------


class TestAdapterTransportStateDir:
    """adapter_transport_state_dir returns state_dir/adapters/{adapter_id}/{transport}."""

    @pytest.fixture()
    def paths(self) -> MedrePaths:
        return resolve()

    def test_matrix_transport_dir(self, paths: MedrePaths) -> None:
        result = paths.adapter_transport_state_dir("bot1", "matrix")
        assert result == paths.state_dir / "adapters" / "bot1" / "matrix"

    def test_lxmf_transport_dir(self, paths: MedrePaths) -> None:
        result = paths.adapter_transport_state_dir("node1", "lxmf")
        assert result == paths.state_dir / "adapters" / "node1" / "lxmf"

    def test_meshtastic_transport_dir(self, paths: MedrePaths) -> None:
        result = paths.adapter_transport_state_dir("radio1", "meshtastic")
        assert result == paths.state_dir / "adapters" / "radio1" / "meshtastic"

    def test_meshcore_transport_dir(self, paths: MedrePaths) -> None:
        result = paths.adapter_transport_state_dir("core1", "meshcore")
        assert result == paths.state_dir / "adapters" / "core1" / "meshcore"

    def test_derives_from_adapter_state_dir(self, paths: MedrePaths) -> None:
        """adapter_transport_state_dir is adapter_state_dir / transport."""
        base = paths.adapter_state_dir("mybot")
        result = paths.adapter_transport_state_dir("mybot", "matrix")
        assert result == base / "matrix"

    def test_empty_adapter_id_raises(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="non-empty"):
            paths.adapter_transport_state_dir("", "matrix")

    def test_empty_transport_raises(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="non-empty"):
            paths.adapter_transport_state_dir("bot1", "")

    def test_slash_in_transport_raises(self, paths: MedrePaths) -> None:
        with pytest.raises(MedrePathsError, match="path separators"):
            paths.adapter_transport_state_dir("bot1", "mat/rix")

    def test_multiple_adapters_isolated(self, paths: MedrePaths) -> None:
        """Different adapter IDs produce different paths."""
        a = paths.adapter_transport_state_dir("alpha", "matrix")
        b = paths.adapter_transport_state_dir("beta", "matrix")
        assert a != b
        assert "alpha" in str(a)
        assert "beta" in str(b)

    def test_no_tempdir_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolved paths should not use system tempdir."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        result = paths.adapter_transport_state_dir("bot1", "matrix")
        import tempfile
        assert tempfile.gettempdir() not in str(result)


# ---------------------------------------------------------------------------
# to_diagnostics
# ---------------------------------------------------------------------------


class TestToDiagnostics:
    """to_diagnostics returns a string-valued dict for logging."""

    def test_xdg_mode_diagnostics(self) -> None:
        paths = resolve()
        diag = paths.to_diagnostics()

        assert isinstance(diag, dict)
        assert "config_dir" in diag
        assert "config_file" in diag
        assert "state_dir" in diag
        assert "data_dir" in diag
        assert "cache_dir" in diag
        assert "log_dir" in diag
        assert "database_path" in diag
        assert "matrix_store_path" in diag

        # All values are strings
        assert all(isinstance(v, str) for v in diag.values())

    def test_medre_home_mode_diagnostics(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        paths = resolve()
        diag = paths.to_diagnostics()

        # config_dir shows "(none)" in MEDRE_HOME mode
        assert diag["config_dir"] == "(none)"
        assert "config.toml" in diag["config_file"]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """MedrePaths is a frozen dataclass."""

    def test_frozen(self) -> None:
        paths = resolve()
        with pytest.raises(AttributeError):
            paths.state_dir = Path("/hacked")  # type: ignore[misc]
