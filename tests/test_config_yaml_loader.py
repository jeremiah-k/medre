"""Tests for medre.config.loader: YAML parsing, config construction,
TOML rejection, and extension validation.

These tests exercise the public ``load_config`` entry point with YAML
config files (``.yaml`` and ``.yml``), verify that ``.toml`` user config
paths produce the dedicated migration error, and confirm that the parsed
YAML data flows correctly into the existing typed config dataclasses.
"""

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
    MeshtasticRuntimeConfig,
    RuntimeConfig,
)
from medre.config.paths import MedrePaths

# ---------------------------------------------------------------------------
# Sample YAML content
# ---------------------------------------------------------------------------

SAMPLE_YAML = """\
runtime:
  name: test
  shutdown_timeout_seconds: 30

logging:
  level: DEBUG

storage:
  backend: sqlite
  path: "{state}/test.db"

adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: "@bot:test"
      access_token: tok
      room_allowlist:
        - "!room:test"
      encryption_mode: plaintext
"""

SAMPLE_MULTI_ADAPTER_YAML = """\
runtime:
  name: multi

logging:
  level: INFO

storage:
  backend: sqlite
  path: "{state}/medre.db"

adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.example.com
      user_id: "@bot:example.com"
      access_token: secret1
      encryption_mode: plaintext
    alt:
      enabled: false
      homeserver: https://matrix.alt.com
      user_id: "@alt:alt.com"
      access_token: secret2
      device_id: ALT_DEVICE
      store_path: "{state}/adapters/alt/matrix/store"
      encryption_mode: e2ee_required
  meshtastic:
    radio:
      enabled: false
      connection_type: serial
      serial_port: /dev/ttyACM0
      origin_label: TestMesh
"""

INVALID_YAML = """\
runtime:
  name: [unclosed
"""

MINIMAL_YAML = """\
runtime: {}
"""

SAMPLE_WITH_ROUTES_YAML = """\
runtime:
  name: routed

logging:
  level: INFO

storage:
  backend: sqlite

adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: "@bot:test"
      access_token: tok
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      connection_type: serial
      serial_port: /dev/ttyACM0

routes:
  bridge1:
    source_adapters:
      - main
    dest_adapters:
      - radio
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
    """Write SAMPLE_YAML to a temp .yaml file and return its path."""
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_YAML)
    return p


@pytest.fixture()
def config_file_yml(tmp_path: Path) -> Path:
    """Write SAMPLE_YAML to a temp .yml file and return its path."""
    p = tmp_path / "config.yml"
    p.write_text(SAMPLE_YAML)
    return p


@pytest.fixture()
def multi_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_MULTI_ADAPTER_YAML)
    return p


@pytest.fixture()
def invalid_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(INVALID_YAML)
    return p


@pytest.fixture()
def minimal_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_YAML)
    return p


@pytest.fixture()
def routes_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_WITH_ROUTES_YAML)
    return p


# ---------------------------------------------------------------------------
# load_config — valid YAML
# --- TestLoadValidYAML: load_config parses valid YAML and returns RuntimeConfig. ---
# ---------------------------------------------------------------------------


def test_returns_runtime_config(config_file: Path) -> None:
    config, _source, _paths = load_config(str(config_file))
    assert isinstance(config, RuntimeConfig)


def test_source_is_explicit(config_file: Path) -> None:
    _config, source, _paths = load_config(str(config_file))
    assert source == ConfigSource.EXPLICIT


def test_paths_returned(config_file: Path) -> None:
    _config, _source, paths = load_config(str(config_file))
    assert isinstance(paths, MedrePaths)


def test_runtime_options_parsed(config_file: Path) -> None:
    config, _, _ = load_config(str(config_file))
    assert config.runtime.name == "test"
    assert config.runtime.shutdown_timeout_seconds == 30


def test_logging_config_parsed(config_file: Path) -> None:
    config, _, _ = load_config(str(config_file))
    assert config.logging.level == "DEBUG"


def test_storage_config_parsed(config_file: Path) -> None:
    config, _, _ = load_config(str(config_file))
    assert config.storage.backend == "sqlite"
    assert config.storage.path is not None
    assert "test.db" in config.storage.path
    assert Path(config.storage.path).is_absolute()


def test_adapter_parsed(config_file: Path) -> None:
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


def test_room_allowlist_is_set(config_file: Path) -> None:
    config, _, _ = load_config(str(config_file))
    matrix = config.adapters.matrix["main"]
    assert matrix.config is not None
    assert isinstance(matrix.config.room_allowlist, set)
    assert "!room:test" in matrix.config.room_allowlist


def test_loads_yml_extension(config_file_yml: Path) -> None:
    """The .yml extension is also accepted."""
    config, source, _ = load_config(str(config_file_yml))
    assert isinstance(config, RuntimeConfig)
    assert source == ConfigSource.EXPLICIT
    assert config.runtime.name == "test"


# ---------------------------------------------------------------------------
# load_config — minimal YAML (defaults)
# --- TestLoadMinimalYAML: Minimal YAML produces RuntimeConfig with defaults. ---
# ---------------------------------------------------------------------------


def test_defaults(minimal_config_file: Path) -> None:
    config, _, _ = load_config(str(minimal_config_file))
    assert config.runtime.name == "medre"
    assert config.runtime.shutdown_timeout_seconds == 10
    assert config.logging.level == "INFO"
    assert config.logging.format == "text"
    assert config.storage.backend == "sqlite"
    assert config.storage.path is None
    assert config.adapters.matrix == {}


def test_empty_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("")
    with pytest.raises(ConfigFileError, match="empty"):
        load_config(str(p))


# ---------------------------------------------------------------------------
# load_config — multiple adapter instances
# --- TestLoadMultiAdapter: Multiple named adapter instances under the same transport type. ---
# ---------------------------------------------------------------------------


def test_two_matrix_instances(multi_config_file: Path) -> None:
    config, _, _ = load_config(str(multi_config_file))
    assert "main" in config.adapters.matrix
    assert "alt" in config.adapters.matrix


def test_main_enabled_alt_disabled(multi_config_file: Path) -> None:
    config, _, _ = load_config(str(multi_config_file))
    assert config.adapters.matrix["main"].enabled is True
    assert config.adapters.matrix["alt"].enabled is False


def test_mixed_transport_types(multi_config_file: Path) -> None:
    config, _, _ = load_config(str(multi_config_file))
    assert "radio" in config.adapters.meshtastic
    radio = config.adapters.meshtastic["radio"]
    assert isinstance(radio, MeshtasticRuntimeConfig)
    assert radio.enabled is False
    assert radio.config is not None
    assert radio.config.origin_label == "TestMesh"


def test_disabled_adapter_still_parsed(multi_config_file: Path) -> None:
    config, _, _ = load_config(str(multi_config_file))
    alt = config.adapters.matrix["alt"]
    assert alt.config is not None
    assert alt.config.homeserver == "https://matrix.alt.com"
    assert alt.config.encryption_mode == "e2ee_required"


# ---------------------------------------------------------------------------
# load_config — routes
# --- TestLoadRoutes: Route sections parse correctly from YAML. ---
# ---------------------------------------------------------------------------


def test_route_parsed(routes_config_file: Path) -> None:
    config, _, _ = load_config(str(routes_config_file))
    routes = config.routes.routes
    assert len(routes) == 1
    route = routes[0]
    assert route.route_id == "bridge1"
    assert "main" in route.source_adapters
    assert "radio" in route.dest_adapters


# ---------------------------------------------------------------------------
# load_config — error cases
# --- TestLoadErrors: Error handling for missing or invalid config files. ---
# ---------------------------------------------------------------------------


def test_missing_explicit_path_raises_config_file_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nonexistent.yaml"
    with pytest.raises(ConfigFileError, match="not found"):
        load_config(str(missing))


def test_invalid_yaml_raises_config_file_error(
    invalid_config_file: Path,
) -> None:
    with pytest.raises(ConfigFileError):
        load_config(str(invalid_config_file))


def test_missing_config_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    with pytest.raises(ConfigNotFoundError):
        load_config(None)


# ---------------------------------------------------------------------------
# TOML rejection
# --- TestTOMLRejection: TOML config files must produce a clear, dedicated error. ---
# ---------------------------------------------------------------------------


def test_explicit_toml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[runtime]\nname = 'test'\n")
    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        load_config(str(p))


def test_toml_rejection_exact_message(tmp_path: Path) -> None:
    p = tmp_path / "medre.toml"
    p.write_text("[runtime]\n")
    with pytest.raises(ConfigFileError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "TOML config files are no longer supported; use YAML" in msg
    assert ".yaml" in msg
    assert ".yml" in msg


def test_find_config_rejects_explicit_toml(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[runtime]\n")
    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(str(p))


def test_find_config_rejects_medre_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[runtime]\n")
    monkeypatch.setenv("MEDRE_CONFIG", str(p))
    with pytest.raises(
        ConfigFileError, match="TOML config files are no longer supported"
    ):
        find_config(None)


def test_toml_not_silently_parsed(tmp_path: Path) -> None:
    """A TOML file must not be parsed as YAML; it must be rejected."""
    p = tmp_path / "config.toml"
    p.write_text('[runtime]\nname = "test"\n[adapters.matrix]\n')
    with pytest.raises(ConfigFileError) as exc_info:
        load_config(str(p))
    # The error must mention TOML rejection, not a YAML parse error
    msg = str(exc_info.value)
    assert "no longer supported" in msg


# ---------------------------------------------------------------------------
# Unsupported extensions
# --- TestUnsupportedExtensions: Files with unsupported extensions are rejected at discovery. ---
# ---------------------------------------------------------------------------


def test_txt_extension_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.txt"
    p.write_text("runtime: {}")
    with pytest.raises(ConfigFileError, match="unsupported config file extension"):
        load_config(str(p))


def test_json_extension_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text('{"runtime": {}}')
    with pytest.raises(ConfigFileError, match="unsupported config file extension"):
        load_config(str(p))


def test_no_extension_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config"
    p.write_text("runtime: {}")
    with pytest.raises(ConfigFileError, match="unsupported config file extension"):
        load_config(str(p))


# ---------------------------------------------------------------------------
# Path placeholder expansion
# --- TestPathPlaceholderExpansion: {state}, {data}, etc. in YAML values are expanded. ---
# ---------------------------------------------------------------------------


def test_storage_path_expanded(config_file: Path) -> None:
    config, _, paths = load_config(str(config_file))
    assert config.storage.path is not None
    assert config.storage.path.startswith(str(paths.state_dir))


def test_medre_home_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "mh"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text(
        "runtime: {}\n"
        "storage:\n"
        "  backend: sqlite\n"
        '  path: "{state}/mydb.sqlite"\n'
    )
    monkeypatch.setenv("MEDRE_HOME", str(home))

    config, _, paths = load_config(str(cfg))
    assert config.storage.path is not None
    assert str(home / "state" / "mydb.sqlite") in config.storage.path


def test_adapter_store_path_expanded(multi_config_file: Path) -> None:
    config, _, _ = load_config(str(multi_config_file))
    alt = config.adapters.matrix["alt"]
    assert alt.config is not None
    assert alt.config.store_path is not None
    assert "adapters/alt/matrix/store" in str(alt.config.store_path)


# ---------------------------------------------------------------------------
# ConfigSource enum
# --- TestConfigSourceEnum: ConfigSource enum has expected values. ---
# ---------------------------------------------------------------------------


def test_values() -> None:
    assert ConfigSource.EXPLICIT.value == "explicit"
    assert ConfigSource.MEDRE_CONFIG.value == "MEDRE_CONFIG"
    assert ConfigSource.MEDRE_HOME.value == "MEDRE_HOME"
    assert ConfigSource.XDG.value == "xdg"
    assert ConfigSource.LOCAL.value == "local"


# ---------------------------------------------------------------------------
# Logging validation (confirm YAML flows into existing validation)
# --- TestLoggingValidationFromYAML: Bad logging config types/levels/formats raise ConfigValidationError. ---
# ---------------------------------------------------------------------------


def test_level_int_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("runtime: {}\nlogging:\n  level: 123\n")
    with pytest.raises(ConfigValidationError, match="level must be a string"):
        load_config(str(p))


def test_level_invalid_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("runtime: {}\nlogging:\n  level: NOPE\n")
    with pytest.raises(ConfigValidationError, match="level must be one of"):
        load_config(str(p))


def test_format_invalid_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("runtime: {}\nlogging:\n  format: xml\n")
    with pytest.raises(ConfigValidationError, match="format must be one of"):
        load_config(str(p))


def test_lowercase_level_canonicalised(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("runtime: {}\nlogging:\n  level: debug\n")
    config, _, _ = load_config(str(p))
    assert config.logging.level == "DEBUG"
