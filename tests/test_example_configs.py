"""Validate shipped example configs load, parse, and conform to current schema.

Track 3 requirement: every shipped example must be loadable and either fully
buildable (fake-multi-adapter) or explicitly marked as requiring
credentials / hardware.  No live SDKs are needed to run this suite.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path

import pytest

from medre.adapters.matrix.errors import MatrixConfigError
from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = _ROOT / "examples"
CONFIGS_DIR = EXAMPLES_DIR / "configs"
ENV_DIR = EXAMPLES_DIR / "env"

REQUIRED_TOML_CONFIGS = [
    "matrix.toml",
    "meshtastic-serial.toml",
    "fake-multi-adapter.toml",
    "mixed-matrix-meshtastic.toml",
]

DOCKER_ENV = ENV_DIR / "docker.env.example"

# Patterns that indicate real secrets — must never appear in examples.
_SECRET_PATTERNS = [
    # Real Matrix access tokens: syt_ followed by 10+ alphanumeric chars.
    # Placeholders like syt_secret_token_here contain underscores and won't match.
    re.compile(r"syt_[a-zA-Z0-9]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
]

# Words suggesting deprecated / legacy language (case-insensitive).
_DEPRECATED_TERMS = [
    "legacy",
    "deprecated",
    "old_config",
    "v1_config",
    "compat_mode",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _has_real_secrets(text: str) -> list[str]:
    """Return list of patterns that matched (empty = clean)."""
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))
    return hits


def _has_deprecated_language(text: str) -> list[str]:
    """Return list of deprecated terms found (lower-cased comparison)."""
    low = text.lower()
    return [t for t in _DEPRECATED_TERMS if t in low]


# ===========================================================================
# 1. File existence
# ===========================================================================


class TestExampleFilesExist:
    """All required example files must ship in the repository."""

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_toml_config_exists(self, name: str) -> None:
        p = CONFIGS_DIR / name
        assert p.is_file(), f"Missing required example config: {p}"

    def test_docker_env_exists(self) -> None:
        assert DOCKER_ENV.is_file(), f"Missing docker env example: {DOCKER_ENV}"


# ===========================================================================
# 2. TOML parseability
# ===========================================================================


class TestTomlParseable:
    """Every shipped TOML example must parse without error."""

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_valid_toml(self, name: str) -> None:
        raw = _read(CONFIGS_DIR / name)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)


# ===========================================================================
# 3. Fake-multi-adapter: full load + build (no live SDKs)
# ===========================================================================


class TestFakeMultiAdapter:
    """The fake-multi-adapter example must load, validate, and build without
    any optional SDKs installed."""

    CONFIG_PATH = CONFIGS_DIR / "fake-multi-adapter.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_loads_via_config_loader(self) -> None:
        config, source, paths = load_config(str(self.CONFIG_PATH))
        assert config.runtime.name == "fake-multi-dev"
        assert config.storage.backend == "memory"
        assert len(config.adapters.all_enabled()) == 4

    def test_all_adapters_are_fake(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        for _transport, _aid, rtc in config.adapters.all_configs():
            assert rtc.adapter_kind == "fake", (
                f"Expected adapter_kind='fake' for {_transport}.{_aid}, "
                f"got {rtc.adapter_kind!r}"
            )

    def test_builds_via_runtime_builder(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        adapters = app.adapters
        assert len(adapters) == 4, (
            f"Expected 4 built adapters, got {len(adapters)}: "
            f"{list(adapters.keys())}"
        )

        expected_ids = {
            "fake_matrix", "fake_meshtastic", "fake_meshcore", "fake_lxmf",
        }
        assert expected_ids == set(adapters.keys())

    def test_build_failures_empty(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert app.build_failures == [], (
            f"Unexpected build failures: {app.build_failures}"
        )

    def test_storage_is_memory(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # Memory-backed SQLite uses ":memory:" path.
        assert app.storage is not None
        assert str(app.storage._db_path) == ":memory:"


# ===========================================================================
# 4. Meshtastic-serial: loads and validates (no build — hardware needed)
# ===========================================================================


class TestMeshtasticSerial:
    """The meshtastic-serial example loads and validates but requires
    real hardware to run (marked as hardware-required)."""

    CONFIG_PATH = CONFIGS_DIR / "meshtastic-serial.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_loads_via_config_loader(self) -> None:
        config, source, paths = load_config(str(self.CONFIG_PATH))
        assert config.runtime.name == "meshtastic-serial"
        assert config.storage.backend == "sqlite"

    def test_meshtastic_adapter_validates(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        meshtastic_adapters = config.adapters.meshtastic
        assert "radio" in meshtastic_adapters
        rtc = meshtastic_adapters["radio"]
        assert rtc.enabled is True
        assert rtc.adapter_kind == "real"
        # connection_type="serial" with serial_port present — validate passes.
        assert rtc.config is not None
        assert rtc.config.serial_port == "/dev/ttyACM0"

    @pytest.mark.skip(reason="Requires real Meshtastic hardware on serial port")
    def test_build_with_hardware(self) -> None:
        """Placeholder: building real Meshtastic adapter needs live SDK + hardware."""
        pass


# ===========================================================================
# 5. Matrix: credential-required (empty access token)
# ===========================================================================


class TestMatrixConfig:
    """The matrix example intentionally has an empty access_token.
    Config loading must raise MatrixConfigError, confirming the example
    is properly structured but credential-incomplete."""

    CONFIG_PATH = CONFIGS_DIR / "matrix.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_load_raises_credential_error(self) -> None:
        """Loading must fail with a credential-related error (empty access_token)."""
        with pytest.raises(MatrixConfigError, match="access_token"):
            load_config(str(self.CONFIG_PATH))

    def test_toml_structure_is_correct(self) -> None:
        """The TOML structure itself is valid — only the credential is missing."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert "adapters" in data
        assert "matrix" in data["adapters"]
        matrix_section = data["adapters"]["matrix"]
        assert "main" in matrix_section
        main = matrix_section["main"]
        assert main.get("homeserver", "").startswith("https://")
        assert main.get("user_id", "").startswith("@")
        assert main.get("encryption_mode") in (
            "plaintext", "e2ee_required", "e2ee_optional",
        )

    @pytest.mark.skip(reason="Requires real Matrix homeserver credentials")
    def test_build_with_credentials(self) -> None:
        """Placeholder: building real Matrix adapter needs live credentials."""
        pass


# ===========================================================================
# 6. Mixed Matrix + Meshtastic: credential-required for Matrix component
# ===========================================================================


class TestMixedMatrixMeshtastic:
    """The mixed bridge example has a Matrix adapter with empty access_token.
    Loading must fail on the Matrix credential."""

    CONFIG_PATH = CONFIGS_DIR / "mixed-matrix-meshtastic.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_load_raises_credential_error(self) -> None:
        with pytest.raises(MatrixConfigError, match="access_token"):
            load_config(str(self.CONFIG_PATH))

    def test_toml_structure_is_correct(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert "matrix" in data["adapters"]
        assert "meshtastic" in data["adapters"]
        meshtastic = data["adapters"]["meshtastic"]["radio"]
        assert meshtastic["connection_type"] == "serial"
        assert "serial_port" in meshtastic

    @pytest.mark.skip(reason="Requires Matrix credentials + Meshtastic hardware")
    def test_build_with_credentials_and_hardware(self) -> None:
        """Placeholder: needs both Matrix credentials and Meshtastic hardware."""
        pass


# ===========================================================================
# 7. Docker env example: format and content validation
# ===========================================================================


class TestDockerEnvExample:
    """Validate docker.env.example has proper format and no real secrets."""

    def test_file_is_commented_template(self) -> None:
        lines = _read(DOCKER_ENV).splitlines()
        non_empty = [l for l in lines if l.strip() and not l.strip().startswith("#")]
        assert len(non_empty) > 0, "env file should have at least one variable"

    def test_matrix_vars_present(self) -> None:
        text = _read(DOCKER_ENV)
        assert "MEDRE_MATRIX_HOMESERVER" in text
        assert "MEDRE_MATRIX_USER_ID" in text
        assert "MEDRE_MATRIX_ACCESS_TOKEN" in text

    def test_meshtastic_vars_present(self) -> None:
        text = _read(DOCKER_ENV)
        assert "MEDRE_MESHTASTIC_ENABLED" in text
        assert "MEDRE_MESHTASTIC_CONNECTION_TYPE" in text
        assert "MEDRE_MESHTASTIC_SERIAL_PORT" in text

    def test_medre_home_var_present(self) -> None:
        text = _read(DOCKER_ENV)
        assert "MEDRE_HOME" in text

    def test_log_level_var_present(self) -> None:
        text = _read(DOCKER_ENV)
        assert "MEDRE_LOG_LEVEL" in text

    def test_no_real_secrets(self) -> None:
        text = _read(DOCKER_ENV)
        hits = _has_real_secrets(text)
        assert hits == [], f"Possible real secrets found: {hits}"

    def test_no_deprecated_language(self) -> None:
        text = _read(DOCKER_ENV)
        found = _has_deprecated_language(text)
        assert found == [], f"Deprecated language found: {found}"


# ===========================================================================
# 8. Cross-cutting: no real secrets, no deprecated language
# ===========================================================================


class TestExampleHygiene:
    """All shipped example files must be free of real secrets and
    deprecated / legacy language."""

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_no_real_secrets(self, name: str) -> None:
        text = _read(CONFIGS_DIR / name)
        hits = _has_real_secrets(text)
        assert hits == [], f"{name}: possible real secrets: {hits}"

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_no_deprecated_language(self, name: str) -> None:
        text = _read(CONFIGS_DIR / name)
        found = _has_deprecated_language(text)
        assert found == [], f"{name}: deprecated language: {found}"

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_uses_supported_storage_backend(self, name: str) -> None:
        """Storage backend must be one supported by RuntimeBuilder."""
        raw = _read(CONFIGS_DIR / name)
        data = tomllib.loads(raw)
        backend = data.get("storage", {}).get("backend", "sqlite")
        assert backend in ("sqlite", "memory"), (
            f"{name}: unsupported storage backend {backend!r}"
        )

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_path_placeholders_are_valid(self, name: str) -> None:
        """Path placeholders in storage.path must use known tokens."""
        raw = _read(CONFIGS_DIR / name)
        data = tomllib.loads(raw)
        storage_path = data.get("storage", {}).get("path")
        if storage_path is None:
            return  # memory backend or default path — OK
        # Extract all {placeholder} tokens.
        tokens = set(re.findall(r"\{(\w+)\}", storage_path))
        valid = {"config", "state", "data", "cache", "logs"}
        unknown = tokens - valid
        assert unknown == set(), (
            f"{name}: unknown path placeholders in storage.path: {unknown}"
        )

    @pytest.mark.parametrize("name", REQUIRED_TOML_CONFIGS)
    def test_adapter_kinds_valid(self, name: str) -> None:
        """adapter_kind values must be 'real' or 'fake'."""
        raw = _read(CONFIGS_DIR / name)
        data = tomllib.loads(raw)
        adapters = data.get("adapters", {})
        for transport, instances in adapters.items():
            if not isinstance(instances, dict):
                continue
            for inst_name, inst_conf in instances.items():
                if not isinstance(inst_conf, dict):
                    continue
                kind = inst_conf.get("adapter_kind", "real")
                assert kind in ("real", "fake"), (
                    f"{name}: adapters.{transport}.{inst_name} has "
                    f"invalid adapter_kind={kind!r}"
                )
