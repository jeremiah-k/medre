"""Tests for medre.config.loader: YAML parsing, search order,
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
      homeserver: "https://matrix.test"
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
      homeserver: "https://matrix.example.com"
      user_id: "@bot:example.com"
      access_token: secret1
      encryption_mode: plaintext
    alt:
      enabled: false
      homeserver: "https://matrix.alt.com"
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
  name: "unterminated
"""

MINIMAL_YAML = """\
runtime: {}
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
    """Write SAMPLE_YAML to a temp file and return its path."""
    p = tmp_path / "config.yaml"
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


# ---------------------------------------------------------------------------
# load_config — valid YAML
# ---------------------------------------------------------------------------


class TestLoadValidConfig:
    """load_config parses valid YAML and returns RuntimeConfig."""

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
# load_config — minimal YAML (defaults)
# ---------------------------------------------------------------------------


class TestLoadMinimalConfig:
    """Minimal YAML produces RuntimeConfig with defaults."""

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
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(ConfigFileError, match="not found"):
            load_config(str(missing))

    def test_invalid_yaml_raises_config_file_error(
        self, invalid_config_file: Path
    ) -> None:
        with pytest.raises(ConfigFileError, match="unexpected end of stream"):
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
# Unknown root-level key rejection (TC-010 / audit finding F-012)
# ---------------------------------------------------------------------------


# --- Unknown root-level key rejection (TC-010 / audit finding F-012) ---
# Unknown top-level keys (typos like ``roues:``) are rejected at load.
# Wave 2 added an explicit ``set(data) - _KNOWN_ROOT_KEYS`` check at the
# end of ``_parse_runtime_config`` so a typo at the root surfaces as a
# :class:`ConfigValidationError` instead of silently being dropped (which
# would leave the operator with default values for the section they
# thought they were configuring). Matches ``additionalProperties: false``
# on the JSON schemas.


def test_typo_root_key_rejected(tmp_path: Path) -> None:
    """``roues:`` (typo of ``routes:``) raises ConfigValidationError."""
    p = _write_config(tmp_path, "runtime: {}\nroues: {}\n")
    with pytest.raises(
        ConfigValidationError, match="Unknown root config key"
    ) as exc_info:
        load_config(str(p))
    # section_path must identify the root, not a sub-section.
    assert exc_info.value.section_path == "<root>"
    # The offending key name must appear in the message so operators
    # can find the typo without reading source code.
    assert "roues" in str(exc_info.value)


def test_valid_root_keys_accepted(tmp_path: Path) -> None:
    """Sanity check: a config using only known root keys loads fine."""
    p = _write_config(
        tmp_path,
        "runtime: {}\n"
        "logging:\n  level: INFO\n"
        "storage:\n  backend: sqlite\n"
        "retry:\n  enabled: false\n"
        "adapters: {}\n"
        "routes: {}\n",
    )
    # Must not raise.
    config, _, _ = load_config(str(p))
    assert config.runtime.name == "medre"


# ---------------------------------------------------------------------------
# Migration diagnostics for removed keys (F-018 / Task 4)
# ---------------------------------------------------------------------------
# When operators migrate from an older MEDRE config, they may still use keys
# that were removed or renamed by prior changes (``meshnet_name``,
# ``matrix_relay_prefix``, ...). The unknown-key rejection now appends a
# value-free hint pointing at the replacement field(s). Hints reference only
# key NAMES and replacement field names — never operator-supplied values — so
# they cannot leak secrets (audit F-010..F-013).


def test_removed_root_key_hint_appended(tmp_path: Path) -> None:
    """``meshnet_name`` as a root key surfaces a migration hint.

    The rejection itself is unchanged (still raises ``Unknown root config
    key``); the suggestion is *appended* and points the operator at
    ``origin_label`` / ``source_origin_label`` / ``dest_origin_label``.
    """
    p = _write_config(tmp_path, "runtime: {}\nmeshnet_name: old-style\n")
    with pytest.raises(
        ConfigValidationError, match="Unknown root config key"
    ) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    # The offending key name must still appear.
    assert "meshnet_name" in msg
    # The migration hint block must be present and name the replacement.
    assert "Hints:" in msg
    assert "origin_label" in msg


def test_removed_runtime_key_hint_appended(tmp_path: Path) -> None:
    """``meshnet_name`` inside ``[runtime]`` surfaces the same hint."""
    p = _write_config(
        tmp_path,
        "runtime:\n  name: test\n  meshnet_name: old-style\n",
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "meshnet_name" in msg
    assert "Hints:" in msg
    assert "origin_label" in msg
    assert exc_info.value.section_path == "runtime"


def test_no_hint_for_unknown_key_without_replacement(tmp_path: Path) -> None:
    """An unknown key with no known replacement must not emit a Hints block.

    Guards against the hint machinery spuriously firing on every typo.
    """
    p = _write_config(
        tmp_path,
        "runtime: {}\ntotally_bogus_key: true\n",
    )
    with pytest.raises(
        ConfigValidationError, match="Unknown root config key"
    ) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "totally_bogus_key" in msg
    # No removed-key match → no Hints section at all.
    assert "Hints:" not in msg


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
        other = tmp_path / "other.yaml"
        other.write_text("runtime: {}\n")
        monkeypatch.setenv("MEDRE_CONFIG", str(other))

        path, source = find_config(str(config_file))
        assert source == ConfigSource.EXPLICIT
        assert path == config_file

    def test_medre_config_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "env_config.yaml"
        cfg.write_text("runtime: {}\n")
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_CONFIG
        assert path == cfg

    def test_medre_home_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "medre_home"
        home.mkdir()
        cfg = home / "config.yaml"
        cfg.write_text("runtime: {}\n")
        monkeypatch.setenv("MEDRE_HOME", str(home))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.MEDRE_HOME
        assert path == cfg

    def test_xdg_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        xdg_config = tmp_path / "xdg-config" / "medre"
        xdg_config.mkdir(parents=True)
        cfg = xdg_config / "config.yaml"
        cfg.write_text("runtime: {}\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)

        path, source = find_config(None)
        assert source == ConfigSource.XDG
        assert path == cfg

    def test_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "medre.yaml"
        cfg.write_text("runtime: {}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))

        path, source = find_config(None)
        assert source == ConfigSource.LOCAL
        assert path == cfg

    def test_explicit_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigFileError, match="not found"):
            find_config(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------------------
# Path placeholder expansion in YAML
# ---------------------------------------------------------------------------


class TestPathPlaceholderExpansion:
    """{state}, {data}, etc. in YAML values are expanded."""

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


def _write_config(tmp_path: Path, yaml_content: str) -> Path:
    """Write YAML content to a temp config file and return its path."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml_content)
    return p


class TestLoggingValidation:
    """Bad logging config types/levels/formats/overs raise ConfigValidationError
    at config load, not AttributeError or setup_logging-time errors."""

    def test_level_int_raises(self, tmp_path: Path) -> None:
        """logging.level = 123 → ConfigValidationError."""
        p = _write_config(tmp_path, "runtime: {}\nlogging:\n  level: 123\n")
        with pytest.raises(ConfigValidationError, match="level must be a string"):
            load_config(str(p))

    def test_level_invalid_string_raises(self, tmp_path: Path) -> None:
        """logging.level = 'NOPE' → ConfigValidationError."""
        p = _write_config(tmp_path, "runtime: {}\nlogging:\n  level: NOPE\n")
        with pytest.raises(ConfigValidationError, match="level must be one of"):
            load_config(str(p))

    def test_format_invalid_raises(self, tmp_path: Path) -> None:
        """logging.format = 'xml' → ConfigValidationError."""
        p = _write_config(tmp_path, "runtime: {}\nlogging:\n  format: xml\n")
        with pytest.raises(ConfigValidationError, match="format must be one of"):
            load_config(str(p))

    def test_format_int_raises(self, tmp_path: Path) -> None:
        """logging.format = 42 → ConfigValidationError."""
        p = _write_config(tmp_path, "runtime: {}\nlogging:\n  format: 42\n")
        with pytest.raises(ConfigValidationError, match="format must be a string"):
            load_config(str(p))

    def test_overrides_array_raises(self, tmp_path: Path) -> None:
        """logging.overrides = [] → ConfigValidationError (not a table)."""
        p = _write_config(tmp_path, "runtime: {}\nlogging:\n  overrides: []\n")
        with pytest.raises(ConfigValidationError, match="overrides must be a table"):
            load_config(str(p))

    def test_overrides_blank_key_raises(self, tmp_path: Path) -> None:
        """overrides[''] = 'DEBUG' → ConfigValidationError (blank logger name)."""
        p = _write_config(
            tmp_path,
            "runtime: {}\n"
            "logging:\n"
            "  level: INFO\n"
            "  overrides:\n"
            '    "": DEBUG\n',
        )
        with pytest.raises(ConfigValidationError, match="invalid logger name"):
            load_config(str(p))

    def test_overrides_value_int_raises(self, tmp_path: Path) -> None:
        """overrides.nio = 123 → ConfigValidationError."""
        p = _write_config(
            tmp_path,
            "runtime: {}\n"
            "logging:\n"
            "  level: INFO\n"
            "  overrides:\n"
            "    nio: 123\n",
        )
        with pytest.raises(ConfigValidationError, match="invalid level"):
            load_config(str(p))

    def test_overrides_value_invalid_string_raises(self, tmp_path: Path) -> None:
        """overrides.nio = 'NOPE' → ConfigValidationError."""
        p = _write_config(
            tmp_path,
            "runtime: {}\n"
            "logging:\n"
            "  level: INFO\n"
            "  overrides:\n"
            "    nio: NOPE\n",
        )
        with pytest.raises(ConfigValidationError, match="invalid level"):
            load_config(str(p))

    def test_valid_quoted_dotted_key_override(self, tmp_path: Path) -> None:
        """'nio.crypto.log' = 'ERROR' loads correctly."""
        p = _write_config(
            tmp_path,
            "runtime: {}\n"
            "logging:\n"
            "  level: INFO\n"
            "  overrides:\n"
            '    "nio.crypto.log": ERROR\n',
        )
        config, _, _ = load_config(str(p))
        assert config.logging.overrides["nio.crypto.log"] == "ERROR"


class TestLoggingCanonicalisation:
    """Logging level/format/overrides are normalised regardless of YAML casing."""

    def test_lowercase_level_stored_uppercase(self, tmp_path: Path) -> None:
        """logging.level = 'debug' → stored as 'DEBUG'."""
        p = _write_config(
            tmp_path,
            "runtime: {}\nlogging:\n  level: debug\n",
        )
        config, _, _ = load_config(str(p))
        assert config.logging.level == "DEBUG"

    def test_uppercase_format_stored_lowercase(self, tmp_path: Path) -> None:
        """logging.format = 'JSON' → stored as 'json'."""
        p = _write_config(
            tmp_path,
            "runtime: {}\nlogging:\n  level: INFO\n  format: JSON\n",
        )
        config, _, _ = load_config(str(p))
        assert config.logging.format == "json"

    def test_override_lowercase_stored_uppercase(self, tmp_path: Path) -> None:
        """logging.overrides.nio = 'debug' → stored as 'DEBUG'."""
        p = _write_config(
            tmp_path,
            "runtime: {}\n"
            "logging:\n"
            "  level: INFO\n"
            "  overrides:\n"
            "    nio: debug\n",
        )
        config, _, _ = load_config(str(p))
        assert config.logging.overrides["nio"] == "DEBUG"


class TestLoadConfigFileReadErrors:
    """load_config wraps file-read failures as ConfigFileError."""

    def test_load_config_wraps_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError reading the resolved config file is wrapped as
        ConfigFileError, not leaked raw."""
        cfg = tmp_path / "unreadable.yaml"
        cfg.write_text("runtime:\n  name: x\n")

        def _boom(self: Path, *args: object, **kwargs: object) -> str:
            raise OSError("permission denied (simulated)")

        # find_config only checks is_file(); the first (and only) read_text
        # call is the one in load_config we want to fail.
        monkeypatch.setattr("pathlib.Path.read_text", _boom)
        with pytest.raises(ConfigFileError, match="Cannot read config file"):
            load_config(str(cfg))

    def test_load_config_wraps_unicode_decode_error(self, tmp_path: Path) -> None:
        """A config file that is not valid UTF-8 is wrapped as ConfigFileError."""
        cfg = tmp_path / "bad-utf8.yaml"
        # \xff / \xfe are invalid as a UTF-8 start sequence.
        cfg.write_bytes(b"\xff\xfe\xfd\xfc not valid utf8")
        with pytest.raises(ConfigFileError, match="not valid UTF-8"):
            load_config(str(cfg))


# ---------------------------------------------------------------------------
# Section type validation — non-mapping section values are rejected
# (Task 2: runtime/storage/retry/adapters/routes [] or "bad" → ConfigValidationError)
# ---------------------------------------------------------------------------


class TestSectionTypeValidation:
    """A section that should be a mapping but is a list/scalar is rejected
    with a clear ConfigValidationError instead of crashing with a raw
    AttributeError when downstream code calls ``.get()`` / ``.items()``.
    """

    def test_runtime_list_rejected(self, tmp_path: Path) -> None:
        """runtime: [] → ConfigValidationError, section_path='runtime'."""
        p = _write_config(tmp_path, "runtime: []\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "runtime"
        assert "runtime" in str(exc_info.value)
        assert "mapping" in str(exc_info.value)

    def test_storage_list_rejected(self, tmp_path: Path) -> None:
        """storage: [] → ConfigValidationError, section_path='storage'."""
        p = _write_config(tmp_path, "storage: []\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "storage"

    def test_retry_list_rejected(self, tmp_path: Path) -> None:
        """retry: [] → ConfigValidationError, section_path='retry'."""
        p = _write_config(tmp_path, "retry: []\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "retry"

    def test_adapters_list_rejected(self, tmp_path: Path) -> None:
        """adapters: [] → ConfigValidationError, section_path='adapters'."""
        p = _write_config(tmp_path, "adapters: []\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters"

    def test_routes_list_rejected(self, tmp_path: Path) -> None:
        """routes: [] → ConfigValidationError, section_path='routes'.

        Without the type check, RouteConfigSet.from_dict would call
        ``.items()`` on a list and raise a raw AttributeError.
        """
        p = _write_config(tmp_path, "routes: []\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "routes"

    def test_runtime_null_accepted(self, tmp_path: Path) -> None:
        """runtime: null loads with defaults (treated as unset/empty)."""
        p = _write_config(tmp_path, "runtime: null\n")
        config, _, _ = load_config(str(p))
        assert config.runtime.name == "medre"

    def test_logging_null_accepted(self, tmp_path: Path) -> None:
        """logging: null loads with defaults (treated as unset/empty)."""
        p = _write_config(tmp_path, "logging: null\n")
        config, _, _ = load_config(str(p))
        assert config.logging.level == "INFO"

    def test_existing_valid_configs_still_load(self, config_file: Path) -> None:
        """Sanity check: a fully valid config still loads without error."""
        config, _, _ = load_config(str(config_file))
        assert config.runtime.name == "test"


# ---------------------------------------------------------------------------
# Unknown adapter transport group rejection (Task 3)
# ---------------------------------------------------------------------------


class TestUnknownTransportRejection:
    """A typo'd transport group (e.g. ``adapters.matrixx``) is rejected
    instead of silently loading with no adapters configured."""

    def test_unknown_transport_matrixx_rejected(self, tmp_path: Path) -> None:
        """adapters.matrixx → ConfigValidationError mentioning the typo
        and listing valid transports."""
        p = _write_config(
            tmp_path,
            "adapters:\n  matrixx:\n    main:\n      enabled: true\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters"
        msg = str(exc_info.value)
        assert "matrixx" in msg
        # At least one valid transport name appears in the message.
        assert "matrix" in msg

    def test_unknown_transport_rejected_with_error_detail(self, tmp_path: Path) -> None:
        """section_path is 'adapters' and the accepted transport list
        appears verbatim in the message."""
        p = _write_config(
            tmp_path,
            "adapters:\n  bogustype:\n    foo:\n      enabled: true\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters"
        msg = str(exc_info.value)
        assert "bogustype" in msg
        # All four canonical transports appear in the accepted list.
        for t in ("matrix", "meshtastic", "meshcore", "lxmf"):
            assert t in msg

    def test_known_transports_load(self, multi_config_file: Path) -> None:
        """Sanity check: known transport groups still load normally."""
        config, _, _ = load_config(str(multi_config_file))
        assert "main" in config.adapters.matrix
        assert "radio" in config.adapters.meshtastic


# ---------------------------------------------------------------------------
# Malformed adapter group / instance shape rejection (Task 4)
# ---------------------------------------------------------------------------


class TestAdapterShapeValidation:
    """Non-mapping adapter group values and non-mapping instance values
    are rejected with a clear ConfigValidationError instead of crashing
    with AttributeError (group) or silently skipping (instance)."""

    def test_non_mapping_transport_group_rejected(self, tmp_path: Path) -> None:
        """adapters.matrix: 'bad' → ConfigValidationError at
        section_path='adapters.matrix'."""
        p = _write_config(tmp_path, "adapters:\n  matrix: bad\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters.matrix"
        msg = str(exc_info.value)
        assert "matrix" in msg
        assert "mapping" in msg

    def test_non_mapping_transport_group_int_rejected(self, tmp_path: Path) -> None:
        """adapters.matrix: 123 → ConfigValidationError."""
        p = _write_config(tmp_path, "adapters:\n  matrix: 123\n")
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters.matrix"

    def test_non_mapping_instance_rejected(self, tmp_path: Path) -> None:
        """adapters.matrix.main: 'bad' → ConfigValidationError at
        section_path='adapters.matrix.main'.

        Previously this was silently skipped via ``continue``; the typo
        never surfaced.
        """
        p = _write_config(
            tmp_path,
            "adapters:\n  matrix:\n    main: bad\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters.matrix.main"
        msg = str(exc_info.value)
        assert "main" in msg
        assert "mapping" in msg

    def test_non_mapping_instance_meshtastic_rejected(self, tmp_path: Path) -> None:
        """adapters.meshtastic.radio: 42 → ConfigValidationError.

        Confirms the rejection applies to every transport, not just matrix.
        """
        p = _write_config(
            tmp_path,
            "adapters:\n  meshtastic:\n    radio: 42\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "adapters.meshtastic.radio"

    def test_valid_adapter_configs_still_load(self, multi_config_file: Path) -> None:
        """Sanity check: valid adapter tables still load."""
        config, _, _ = load_config(str(multi_config_file))
        # Two matrix instances + one meshtastic instance parsed.
        assert {"main", "alt"} == set(config.adapters.matrix)
        assert {"radio"} == set(config.adapters.meshtastic)


# ---------------------------------------------------------------------------
# Unknown global [retry] key rejection (Task 5)
# ---------------------------------------------------------------------------


class TestGlobalRetryUnknownKeys:
    """The top-level ``[retry]`` section rejects unknown keys, mirroring
    the per-route ``[routes.<id>.retry]`` unknown-key rejection."""

    def test_unknown_global_retry_key_rejected(self, tmp_path: Path) -> None:
        """retry: {bogus: 123} → ConfigValidationError at
        section_path='retry' mentioning 'bogus' and accepted keys."""
        p = _write_config(
            tmp_path,
            "retry:\n  enabled: true\n  bogus: 123\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "retry"
        msg = str(exc_info.value)
        assert "bogus" in msg
        assert "Accepted keys" in msg
        # All accepted keys appear in the message.
        for k in ("enabled", "interval_seconds", "batch_size", "max_attempts"):
            assert k in msg

    def test_unknown_global_retry_key_before_value_validation(
        self, tmp_path: Path
    ) -> None:
        """The unknown-key check fires before any value-type validation,
        so an operator with both a typo AND a bad value sees the typo."""
        p = _write_config(
            tmp_path,
            "retry:\n  typo_key: 1\n  batch_size: -5\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        # Must be the unknown-key error, not the range check on batch_size.
        assert "typo_key" in str(exc_info.value)
        assert "unknown key" in str(exc_info.value)

    def test_known_global_retry_keys_accepted(self, tmp_path: Path) -> None:
        """All accepted retry keys load without an 'unknown key' error."""
        p = _write_config(
            tmp_path,
            "retry:\n"
            "  enabled: true\n"
            "  interval_seconds: 5.0\n"
            "  batch_size: 10\n"
            "  max_attempts: 3\n",
        )
        config, _, _ = load_config(str(p))
        assert config.retry.enabled is True
        assert config.retry.batch_size == 10


# ---------------------------------------------------------------------------
# Unknown keys in runtime / runtime.limits / logging / storage (Task 6)
# ---------------------------------------------------------------------------


class TestSectionUnknownKeys:
    """Unknown keys in the runtime, runtime.limits, logging, and storage
    sections are rejected so typos surface at load time."""

    def test_unknown_runtime_key_rejected(self, tmp_path: Path) -> None:
        """runtime: {bogus: 1} → ConfigValidationError at 'runtime'."""
        p = _write_config(
            tmp_path,
            "runtime:\n  name: test\n  bogus: 1\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "runtime"
        msg = str(exc_info.value)
        assert "bogus" in msg
        assert "Accepted keys" in msg

    def test_unknown_runtime_limits_key_rejected(self, tmp_path: Path) -> None:
        """runtime: {limits: {bogus: 1}} → ConfigValidationError at
        'runtime.limits'."""
        p = _write_config(
            tmp_path,
            "runtime:\n  limits:\n    bogus: 1\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "runtime.limits"
        msg = str(exc_info.value)
        assert "bogus" in msg

    def test_unknown_logging_key_rejected(self, tmp_path: Path) -> None:
        """logging: {bogus: 1} → ConfigValidationError at 'logging'."""
        p = _write_config(
            tmp_path,
            "logging:\n  level: INFO\n  bogus: 1\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "logging"
        assert "bogus" in str(exc_info.value)

    def test_unknown_storage_key_rejected(self, tmp_path: Path) -> None:
        """storage: {bogus: 1} → ConfigValidationError at 'storage'."""
        p = _write_config(
            tmp_path,
            "storage:\n  backend: memory\n  bogus: 1\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        assert exc_info.value.section_path == "storage"
        assert "bogus" in str(exc_info.value)

    def test_unknown_runtime_key_before_value_validation(self, tmp_path: Path) -> None:
        """Unknown-key fires before any other validation, matching
        RouteConfig.from_dict / BridgePolicy.from_dict ordering."""
        p = _write_config(
            tmp_path,
            "runtime:\n  typo_field: 1\n  shutdown_timeout_seconds: -1\n",
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(p))
        # Must be the unknown-key error, not anything about the timeout.
        assert "typo_field" in str(exc_info.value)
        assert "unknown key" in str(exc_info.value)

    def test_valid_sections_still_load(self, config_file: Path) -> None:
        """Sanity check: a fully valid config still loads with all
        sections populated."""
        config, _, _ = load_config(str(config_file))
        assert config.runtime.name == "test"
        assert config.runtime.shutdown_timeout_seconds == 30
        assert config.storage.backend == "sqlite"
