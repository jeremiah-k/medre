"""Tests for medre.config.loader: TOML parsing, search order,
config construction, error handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import (
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.loader import ConfigSource, find_config, load_config
from medre.config.model import (
    MatrixRuntimeConfig,
    RuntimeConfig,
)
from medre.config.paths import MedrePaths

# ---------------------------------------------------------------------------
# Sample TOML content
# ---------------------------------------------------------------------------

SAMPLE_TOML = """\
[runtime]
name = "test"
shutdown_timeout_seconds = 30

[logging]
level = "DEBUG"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"
"""

SAMPLE_MULTI_ADAPTER_TOML = """\
[runtime]
name = "multi"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/medre.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "secret1"
encryption_mode = "plaintext"

[adapters.matrix.alt]
enabled = false
homeserver = "https://matrix.alt.com"
user_id = "@alt:alt.com"
access_token = "secret2"
device_id = "ALT_DEVICE"
store_path = "{state}/adapters/alt/matrix/store"
encryption_mode = "e2ee_required"

[adapters.meshtastic.radio]
enabled = false
connection_type = "serial"
serial_port = "/dev/ttyACM0"
origin_label = "TestMesh"
"""

INVALID_TOML = """\
[runtime
name = "bad"
"""

MINIMAL_TOML = """\
[runtime]
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear config-related env vars for each test."""
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """Write SAMPLE_TOML to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(SAMPLE_TOML)
    return p


@pytest.fixture()
def multi_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(SAMPLE_MULTI_ADAPTER_TOML)
    return p


@pytest.fixture()
def invalid_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(INVALID_TOML)
    return p


@pytest.fixture()
def minimal_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(MINIMAL_TOML)
    return p


# ---------------------------------------------------------------------------
# load_config — valid TOML
# ---------------------------------------------------------------------------


class TestLoadValidConfig:
    """load_config parses valid TOML and returns RuntimeConfig."""

    def test_returns_runtime_config(self, config_file: Path) -> None:
        config, source, paths = load_config(str(config_file))
        assert isinstance(config, RuntimeConfig)

    def test_source_is_explicit(self, config_file: Path) -> None:
        _config, source, _paths = load_config(str(config_file))
        assert source == ConfigSource.EXPLICIT

    def test_paths_returned(self, config_file: Path) -> None:
        _config, _source, paths = load_config(str(config_file))
        assert isinstance(paths, MedrePaths)

    def test_runtime_options_parsed(self, config_file: Path) -> None:
        config, _, _ = load_config(str(config_file))
        assert config.runtime.name == "test"
        assert config.runtime.shutdown_timeout_seconds == 30

    def test_logging_config_parsed(self, config_file: Path) -> None:
        config, _, _ = load_config(str(config_file))
        assert config.logging.level == "DEBUG"

    def test_storage_config_parsed(self, config_file: Path) -> None:
        config, _, _ = load_config(str(config_file))
        assert config.storage.backend == "sqlite"
        # Path placeholder should be expanded
        assert config.storage.path is not None
        assert "test.db" in config.storage.path
        # Should be an absolute path (placeholder expanded)
        assert Path(config.storage.path).is_absolute()

    def test_adapter_parsed(self, config_file: Path) -> None:
        config, _, _ = load_config(str(config_file))
        assert "main" in config.adapters.matrix
        matrix = config.adapters.matrix["main"]
        assert isinstance(matrix, MatrixRuntimeConfig)
        assert matrix.enabled is True
        assert matrix.adapter_id == "main"
        assert matrix.config is not None
        assert matrix.config.homeserver == "https://matrix.test"
        assert matrix.config.user_id == "@bot:test"
        assert matrix.config.access_token == "tok"

    def test_room_allowlist_is_set(self, config_file: Path) -> None:
        config, _, _ = load_config(str(config_file))
        matrix = config.adapters.matrix["main"]
        assert matrix.config is not None
        assert isinstance(matrix.config.room_allowlist, set)
        assert "!room:test" in matrix.config.room_allowlist


# ---------------------------------------------------------------------------
# load_config — minimal TOML (defaults)
# ---------------------------------------------------------------------------


class TestLoadMinimalConfig:
    """Minimal TOML produces RuntimeConfig with defaults."""

    def test_defaults(self, minimal_config_file: Path) -> None:
        config, _, _ = load_config(str(minimal_config_file))
        assert config.runtime.name == "medre"
        assert config.runtime.shutdown_timeout_seconds == 10
        assert config.logging.level == "INFO"
        assert config.logging.format == "text"
        assert config.storage.backend == "sqlite"
        assert config.storage.path is None
        assert config.adapters.matrix == {}


# ---------------------------------------------------------------------------
# load_config — multiple adapter instances
# ---------------------------------------------------------------------------


class TestLoadMultiAdapter:
    """Multiple named adapter instances under the same transport type."""

    def test_two_matrix_instances(self, multi_config_file: Path) -> None:
        config, _, _ = load_config(str(multi_config_file))
        assert "main" in config.adapters.matrix
        assert "alt" in config.adapters.matrix

    def test_main_enabled_alt_disabled(self, multi_config_file: Path) -> None:
        config, _, _ = load_config(str(multi_config_file))
        assert config.adapters.matrix["main"].enabled is True
        assert config.adapters.matrix["alt"].enabled is False

    def test_mixed_transport_types(self, multi_config_file: Path) -> None:
        config, _, _ = load_config(str(multi_config_file))
        assert "radio" in config.adapters.meshtastic
        radio = config.adapters.meshtastic["radio"]
        assert radio.enabled is False
        assert radio.config is not None
        assert radio.config.origin_label == "TestMesh"

    def test_disabled_adapter_still_parsed(self, multi_config_file: Path) -> None:
        config, _, _ = load_config(str(multi_config_file))
        alt = config.adapters.matrix["alt"]
        assert alt.config is not None
        assert alt.config.homeserver == "https://matrix.alt.com"
        assert alt.config.encryption_mode == "e2ee_required"


# ---------------------------------------------------------------------------
# load_config — error cases
# ---------------------------------------------------------------------------


class TestLoadErrors:
    """Error handling for missing or invalid config files."""

    def test_missing_explicit_path_raises_config_file_error(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nonexistent.toml"
        with pytest.raises(ConfigFileError, match="not found"):
            load_config(str(missing))

    def test_invalid_toml_raises_config_file_error(
        self, invalid_config_file: Path
    ) -> None:
        with pytest.raises(ConfigFileError, match="Invalid TOML"):
            load_config(str(invalid_config_file))

    def test_missing_config_raises_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No config file anywhere → ConfigNotFoundError."""
        monkeypatch.chdir(tmp_path)
        # Ensure no XDG config or MEDRE paths resolve to existing files
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        with pytest.raises(ConfigNotFoundError):
            load_config(None)


# ---------------------------------------------------------------------------
# find_config — search order
# ---------------------------------------------------------------------------


class TestFindConfig:
    """find_config respects the documented search order."""

    def test_explicit_path_wins(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit path takes priority over all other sources."""
        # Set MEDRE_CONFIG to a different file
        other = tmp_path / "other.toml"
        other.write_text("[runtime]\n")
        monkeypatch.setenv("MEDRE_CONFIG", str(other))

        path, source = find_config(str(config_file))
        assert source == ConfigSource.EXPLICIT
        assert path == config_file

    def test_medre_config_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "env_config.toml"
        cfg.write_text("[runtime]\n")
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_CONFIG
        assert path == cfg

    def test_medre_home_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "medre_home"
        home.mkdir()
        cfg = home / "config.toml"
        cfg.write_text("[runtime]\n")
        monkeypatch.setenv("MEDRE_HOME", str(home))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_HOME
        assert path == cfg

    def test_xdg_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        xdg_config = tmp_path / "xdg-config" / "medre"
        xdg_config.mkdir(parents=True)
        cfg = xdg_config / "config.toml"
        cfg.write_text("[runtime]\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.XDG
        assert path == cfg

    def test_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "medre.toml"
        cfg.write_text("[runtime]\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))

        path, source = find_config(None)
        assert source == ConfigSource.LOCAL
        assert path == cfg

    def test_explicit_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigFileError, match="not found"):
            find_config(str(tmp_path / "nope.toml"))


# ---------------------------------------------------------------------------
# Path placeholder expansion in TOML
# ---------------------------------------------------------------------------


class TestPathPlaceholderExpansion:
    """{state}, {data}, etc. in TOML values are expanded."""

    def test_storage_path_expanded(self, config_file: Path) -> None:
        config, _, paths = load_config(str(config_file))
        assert config.storage.path is not None
        # The expanded path should start with the state_dir
        assert config.storage.path.startswith(str(paths.state_dir))

    def test_medre_home_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Placeholders resolve correctly in MEDRE_HOME mode."""
        home = tmp_path / "mh"
        home.mkdir()
        cfg = home / "config.toml"
        cfg.write_text("""\
[runtime]
[storage]
backend = "sqlite"
path = "{state}/mydb.sqlite"
""")
        monkeypatch.setenv("MEDRE_HOME", str(home))

        config, _, paths = load_config(str(cfg))
        assert config.storage.path is not None
        assert str(home / "state" / "mydb.sqlite") in config.storage.path


# ---------------------------------------------------------------------------
# ConfigSource enum
# ---------------------------------------------------------------------------


class TestConfigSourceEnum:
    """ConfigSource enum has expected values."""

    def test_values(self) -> None:
        assert ConfigSource.EXPLICIT.value == "explicit"
        assert ConfigSource.MEDRE_CONFIG.value == "MEDRE_CONFIG"
        assert ConfigSource.MEDRE_HOME.value == "MEDRE_HOME"
        assert ConfigSource.XDG.value == "xdg"
        assert ConfigSource.LOCAL.value == "local"


# ---------------------------------------------------------------------------
# Sample config — generate_sample_config() structure
# ---------------------------------------------------------------------------


class TestSampleConfig:
    """The sample config generated by generate_sample_config() must be
    well-formed and include all required sections."""

    def test_sample_config_is_valid_yaml(self) -> None:
        from medre.config._yaml import parse_yaml_config
        from medre.config.sample import generate_sample_config

        text = generate_sample_config()
        data = parse_yaml_config(text)
        assert isinstance(data, dict)

    def test_sample_config_includes_routes_section(self) -> None:
        """The sample config must demonstrate route configuration."""
        from medre.config._yaml import parse_yaml_config
        from medre.config.sample import generate_sample_config

        text = generate_sample_config()
        data = parse_yaml_config(text)
        assert "routes" in data, "Sample config must include routes section"

    def test_sample_config_route_refs_valid_adapter_ids(self) -> None:
        """Route source/dest adapters must reference adapter IDs declared
        in the sample config."""
        from medre.config._yaml import parse_yaml_config
        from medre.config.sample import generate_sample_config

        text = generate_sample_config()
        data = parse_yaml_config(text)

        # Collect all adapter IDs from the config.
        adapter_ids: set[str] = set()
        adapters = data.get("adapters", {})
        for _transport, instances in adapters.items():
            if not isinstance(instances, dict):
                continue
            for inst_name, inst_conf in instances.items():
                if not isinstance(inst_conf, dict):
                    continue
                aid = inst_conf.get("adapter_id", inst_name)
                adapter_ids.add(aid)

        # Check route references.
        routes = data.get("routes", {})
        for route_id, route_table in routes.items():
            if not isinstance(route_table, dict):
                continue
            for field in ("source_adapters", "dest_adapters"):
                refs = route_table.get(field, [])
                for ref in refs:
                    assert ref in adapter_ids, (
                        f"Sample config route {route_id!r} references "
                        f"{field}={ref!r} which is not a declared adapter ID"
                    )


# ---------------------------------------------------------------------------
# Logging validation — bad types / levels / formats / overrides
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, toml_content: str) -> Path:
    """Write TOML content to a temp config file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(toml_content)
    return p


class TestLoggingValidation:
    """Bad logging config types/levels/formats/overs raise ConfigValidationError
    at config load, not AttributeError or setup_logging-time errors."""

    def test_level_int_raises(self, tmp_path: Path) -> None:
        """logging.level = 123 → ConfigValidationError."""
        p = _write_config(tmp_path, "[runtime]\n[logging]\nlevel = 123\n")
        with pytest.raises(ConfigValidationError, match="level must be a string"):
            load_config(str(p))

    def test_level_invalid_string_raises(self, tmp_path: Path) -> None:
        """logging.level = 'NOPE' → ConfigValidationError."""
        p = _write_config(tmp_path, '[runtime]\n[logging]\nlevel = "NOPE"\n')
        with pytest.raises(ConfigValidationError, match="level must be one of"):
            load_config(str(p))

    def test_format_invalid_raises(self, tmp_path: Path) -> None:
        """logging.format = 'xml' → ConfigValidationError."""
        p = _write_config(tmp_path, '[runtime]\n[logging]\nformat = "xml"\n')
        with pytest.raises(ConfigValidationError, match="format must be one of"):
            load_config(str(p))

    def test_format_int_raises(self, tmp_path: Path) -> None:
        """logging.format = 42 → ConfigValidationError."""
        p = _write_config(tmp_path, "[runtime]\n[logging]\nformat = 42\n")
        with pytest.raises(ConfigValidationError, match="format must be a string"):
            load_config(str(p))

    def test_overrides_array_raises(self, tmp_path: Path) -> None:
        """logging.overrides = [] → ConfigValidationError (not a table)."""
        p = _write_config(tmp_path, "[runtime]\n[logging]\noverrides = []\n")
        with pytest.raises(ConfigValidationError, match="overrides must be a table"):
            load_config(str(p))

    def test_overrides_blank_key_raises(self, tmp_path: Path) -> None:
        """overrides[''] = 'DEBUG' → ConfigValidationError (blank logger name)."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\n[logging.overrides]\n"" = "DEBUG"\n',
        )
        with pytest.raises(ConfigValidationError, match="invalid logger name"):
            load_config(str(p))

    def test_overrides_value_int_raises(self, tmp_path: Path) -> None:
        """overrides.nio = 123 → ConfigValidationError."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\n[logging.overrides]\nnio = 123\n',
        )
        with pytest.raises(ConfigValidationError, match="invalid level"):
            load_config(str(p))

    def test_overrides_value_invalid_string_raises(self, tmp_path: Path) -> None:
        """overrides.nio = 'NOPE' → ConfigValidationError."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\n[logging.overrides]\nnio = "NOPE"\n',
        )
        with pytest.raises(ConfigValidationError, match="invalid level"):
            load_config(str(p))

    def test_valid_quoted_dotted_key_override(self, tmp_path: Path) -> None:
        """'nio.crypto.log' = 'ERROR' loads correctly."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\n'
            '[logging.overrides]\n"nio.crypto.log" = "ERROR"\n',
        )
        config, _, _ = load_config(str(p))
        assert config.logging.overrides["nio.crypto.log"] == "ERROR"


class TestLoggingCanonicalisation:
    """Logging level/format/overrides are normalised regardless of TOML casing."""

    def test_lowercase_level_stored_uppercase(self, tmp_path: Path) -> None:
        """logging.level = 'debug' → stored as 'DEBUG'."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "debug"\n',
        )
        config, _, _ = load_config(str(p))
        assert config.logging.level == "DEBUG"

    def test_uppercase_format_stored_lowercase(self, tmp_path: Path) -> None:
        """logging.format = 'JSON' → stored as 'json'."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\nformat = "JSON"\n',
        )
        config, _, _ = load_config(str(p))
        assert config.logging.format == "json"

    def test_override_lowercase_stored_uppercase(self, tmp_path: Path) -> None:
        """logging.overrides.nio = 'debug' → stored as 'DEBUG'."""
        p = _write_config(
            tmp_path,
            '[runtime]\n[logging]\nlevel = "INFO"\n'
            '[logging.overrides]\nnio = "debug"\n',
        )
        config, _, _ = load_config(str(p))
        assert config.logging.overrides["nio"] == "DEBUG"
