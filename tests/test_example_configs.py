"""Validate shipped example configs load, parse, and conform to current schema.

Track 3 requirement: every shipped example must be loadable and either fully
buildable (fake-multi-adapter) or explicitly marked as requiring
credentials / hardware.  No live SDKs are needed to run this suite.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from medre.config.adapters.errors import MatrixConfigError
from medre.config.errors import ConfigFileError
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
    "fake-bridge-smoke.toml",
    "fake-retry-smoke.toml",
    "mixed-matrix-meshtastic.toml",
    "docker-matrix-bridge.toml",
    "docker-meshtastic-bridge.toml",
    "live-matrix-meshtastic.toml",
]

# Configs with placeholder credentials that cannot be fully loaded.
# Validated for TOML structure and route shape only.
PLACEHOLDER_CREDENTIAL_CONFIGS = [
    "docker-bridge-smoke.toml",
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
            "fake_matrix",
            "fake_meshtastic",
            "fake_meshcore",
            "fake_lxmf",
        }
        assert expected_ids == set(adapters.keys())

    def test_build_failures_empty(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert (
            app.build_failures == []
        ), f"Unexpected build failures: {app.build_failures}"

    def test_storage_is_memory(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # Memory-backed SQLite uses ":memory:" path.
        assert app.storage is not None
        assert str(app.storage._db_path) == ":memory:"

    def test_routes_parse_correctly(self) -> None:
        """Routes in fake-multi-adapter config parse into RouteConfigSet."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        routes = config.routes
        assert len(routes.routes) == 2
        route_ids = [r.route_id for r in routes.routes]
        assert "matrix_mesh_bridge" in route_ids
        assert "matrix_fanout" in route_ids

    def test_routes_register_via_builder(self) -> None:
        """Routes from the fake-multi-adapter config register successfully
        after the builder constructs adapters."""
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # All routes should be registered (all adapters built successfully).
        assert len(app._registered_routes) >= 2, (
            f"Expected at least 2 registered routes, got "
            f"{len(app._registered_routes)}: "
            f"{[r.id for r in app._registered_routes]}"
        )
        registered_ids = [r.id for r in app._registered_routes]
        # matrix_mesh_bridge is bidirectional, so it expands to 2 routes
        # (forward + reverse).  matrix_fanout is source_to_dest with 1 source.
        assert "matrix_mesh_bridge" in registered_ids
        assert "matrix_fanout" in registered_ids


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
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

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
            "plaintext",
            "e2ee_required",
            "e2ee_optional",
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
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

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

    def test_routes_section_structure(self) -> None:
        """The mixed bridge config must include a route section referencing
        the correct adapter IDs (main and radio)."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert "routes" in data, "Mixed bridge config must have a [routes] section"
        routes = data["routes"]
        assert "matrix_radio_bridge" in routes
        bridge = routes["matrix_radio_bridge"]
        assert bridge["source_adapters"] == ["main"]
        assert bridge["dest_adapters"] == ["radio"]
        assert bridge["directionality"] == "bidirectional"
        assert bridge["enabled"] is True
        # Policy must only use supported fields.
        if "policy" in bridge:
            policy = bridge["policy"]
            assert "allowed_event_types" in policy
            # Unsupported policy fields must not be present.
            for unsupported in (
                "sender_allowlist",
                "room_allowlist",
                "channel_allowlist",
                "allowed_source_adapters",
                "allowed_dest_adapters",
            ):
                assert unsupported not in policy, (
                    f"Unsupported policy field {unsupported!r} in "
                    f"mixed bridge example config"
                )

    @pytest.mark.skip(reason="Requires Matrix credentials + Meshtastic hardware")
    def test_build_with_credentials_and_hardware(self) -> None:
        """Placeholder: needs both Matrix credentials and Meshtastic hardware."""
        pass


# ===========================================================================
# 7. Fake-bridge-smoke: full load + build (no live SDKs)
# ===========================================================================


class TestFakeBridgeSmoke:
    """The fake-bridge-smoke example loads, validates, and builds without
    any optional SDKs.  All adapters are fake; all routes exercise
    cross-adapter bridge patterns."""

    CONFIG_PATH = CONFIGS_DIR / "fake-bridge-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_loads_via_config_loader(self) -> None:
        config, _source, _paths = load_config(str(self.CONFIG_PATH))
        assert config.runtime.name == "fake-bridge-smoke"
        assert config.storage.backend == "memory"

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
        assert len(app.adapters) == 3, (
            f"Expected 3 built adapters, got {len(app.adapters)}: "
            f"{list(app.adapters.keys())}"
        )

    def test_routes_parse_correctly(self) -> None:
        """Routes in fake-bridge-smoke config parse into RouteConfigSet."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        routes = config.routes
        assert len(routes.routes) == 6
        route_ids = [r.route_id for r in routes.routes]
        assert "mx_to_mesh" in route_ids
        assert "mx_mesh_bidir" in route_ids

    def test_routes_register_via_builder(self) -> None:
        """Routes register successfully after builder constructs adapters."""
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # 6 configured routes; bidirectional expands to forward + reverse.
        assert len(app._registered_routes) >= 6, (
            f"Expected >= 6 registered routes, got "
            f"{len(app._registered_routes)}: "
            f"{[r.id for r in app._registered_routes]}"
        )


# ===========================================================================
# 7b. Docker-bridge-smoke: placeholder credential validation
# ===========================================================================


class TestDockerBridgeSmoke:
    """The docker-bridge-smoke example has placeholder credentials for the
    Matrix adapter.  Full load_config() will fail on the credential —
    validate TOML structure and route shape only."""

    CONFIG_PATH = CONFIGS_DIR / "docker-bridge-smoke.toml"

    def test_toml_parseable(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)

    def test_toml_structure_adapters(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapters = data["adapters"]
        assert "matrix" in adapters
        assert "meshtastic" in adapters
        # Real Matrix adapter
        mx = adapters["matrix"]["docker_matrix"]
        assert mx["adapter_kind"] == "real"
        assert mx["access_token"] == "PLACEHOLDER"
        # Real Meshtastic adapter
        mesh = adapters["meshtastic"]["docker_meshtastic"]
        assert mesh["adapter_kind"] == "real"
        assert mesh["connection_type"] == "tcp"
        # Fake adapters
        fake_mesh = adapters["meshtastic"]["fake_mesh"]
        assert fake_mesh["adapter_kind"] == "fake"
        fake_mx = adapters["matrix"]["fake_mx"]
        assert fake_mx["adapter_kind"] == "fake"

    def test_routes_section_structure(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        routes = data["routes"]
        assert "real_matrix_to_fake_mesh" in routes
        assert "real_mesh_to_fake_mx" in routes
        assert "real_bidir" in routes
        assert "fake_bridge" in routes
        assert "disabled_example" in routes

    def test_route_adapter_refs_valid(self) -> None:
        """All route source/dest references name adapters that exist."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapter_ids = set()
        for _transport, instances in data["adapters"].items():
            for inst_name in instances:
                adapter_ids.add(inst_name)
        for _route_id, route in data["routes"].items():
            for ref in route.get("source_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown source adapter '{ref}'"
            for ref in route.get("dest_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown dest adapter '{ref}'"

    def test_disabled_route_is_disabled(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert data["routes"]["disabled_example"]["enabled"] is False

    def test_bidirectional_route_direction(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert data["routes"]["real_bidir"]["directionality"] == "bidirectional"

    def test_no_real_secrets(self) -> None:
        text = _read(self.CONFIG_PATH)
        hits = _has_real_secrets(text)
        assert hits == [], f"Possible real secrets found: {hits}"

    def test_loads_successfully_with_placeholder_credentials(self) -> None:
        """Config loads because PLACEHOLDER is a non-empty string.
        The placeholder would fail at runtime when connecting to Synapse,
        but config validation only rejects empty access_token."""
        config, _source, _paths = load_config(str(self.CONFIG_PATH))
        assert config.runtime.name == "docker-bridge-smoke"
        # Real Matrix adapter has placeholder credentials — loads but
        # would fail at runtime.  Fake adapters are fully valid.
        assert len(config.adapters.all_enabled()) == 4


# ===========================================================================
# 8. Docker env example: format and content validation
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


ALL_SHIPPED_CONFIGS = REQUIRED_TOML_CONFIGS + PLACEHOLDER_CREDENTIAL_CONFIGS


class TestExampleHygiene:
    """All shipped example files must be free of real secrets and
    deprecated / legacy language."""

    @pytest.mark.parametrize("name", ALL_SHIPPED_CONFIGS)
    def test_no_real_secrets(self, name: str) -> None:
        text = _read(CONFIGS_DIR / name)
        hits = _has_real_secrets(text)
        assert hits == [], f"{name}: possible real secrets: {hits}"

    @pytest.mark.parametrize("name", ALL_SHIPPED_CONFIGS)
    def test_no_deprecated_language(self, name: str) -> None:
        text = _read(CONFIGS_DIR / name)
        found = _has_deprecated_language(text)
        assert found == [], f"{name}: deprecated language: {found}"

    @pytest.mark.parametrize("name", ALL_SHIPPED_CONFIGS)
    def test_uses_supported_storage_backend(self, name: str) -> None:
        """Storage backend must be one supported by RuntimeBuilder."""
        raw = _read(CONFIGS_DIR / name)
        data = tomllib.loads(raw)
        backend = data.get("storage", {}).get("backend", "sqlite")
        assert backend in (
            "sqlite",
            "memory",
        ), f"{name}: unsupported storage backend {backend!r}"

    @pytest.mark.parametrize("name", ALL_SHIPPED_CONFIGS)
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


# ===========================================================================
# 9. Per-config parse + structure + adapter ID assertions
# ===========================================================================


class TestFakeBridgeSmokeDeep:
    """Deep per-field assertions on fake-bridge-smoke.toml beyond the
    existing smoke tests in TestFakeBridgeSmoke."""

    CONFIG_PATH = CONFIGS_DIR / "fake-bridge-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_config_has_expected_top_level_sections(self) -> None:
        """Parsed config exposes runtime, storage, adapters, routes."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert config.runtime is not None
        assert config.storage is not None
        assert config.adapters is not None
        assert config.routes is not None

    def test_config_adapter_ids_match_toml(self) -> None:
        """Adapter IDs are exactly fake_matrix, fake_meshtastic, fake_meshcore."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        all_ids = [aid for _t, aid, _rtc in config.adapters.all_configs()]
        assert "fake_matrix" in all_ids
        assert "fake_meshtastic" in all_ids
        assert "fake_meshcore" in all_ids
        assert len(all_ids) == 3

    def test_matrix_section_has_at_least_one_adapter(self) -> None:
        """The matrix transport section contains at least one adapter."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert len(config.adapters.matrix) >= 1, "Expected at least one matrix adapter"
        assert "fake_matrix" in config.adapters.matrix

    def test_meshtastic_section_has_at_least_one_adapter(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert len(config.adapters.meshtastic) >= 1
        assert "fake_meshtastic" in config.adapters.meshtastic

    def test_meshcore_section_has_at_least_one_adapter(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert len(config.adapters.meshcore) >= 1
        assert "fake_meshcore" in config.adapters.meshcore

    def test_no_lxmf_adapters(self) -> None:
        """fake-bridge-smoke does not include an LXMF adapter."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert len(config.adapters.lxmf) == 0


# ===========================================================================
# 9b. Fake-retry-smoke: full load + build with retry worker enabled
# ===========================================================================


class TestFakeRetrySmoke:
    """The fake-retry-smoke example loads, validates, and builds without
    any optional SDKs.  The retry worker is enabled via the [retry] section."""

    CONFIG_PATH = CONFIGS_DIR / "fake-retry-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_loads_via_config_loader(self) -> None:
        config, _source, _paths = load_config(str(self.CONFIG_PATH))
        assert config.runtime.name == "fake-retry-smoke"
        assert config.storage.backend == "memory"

    def test_retry_section_enabled(self) -> None:
        """The [retry] section parses with enabled=true."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert config.retry.enabled is True
        assert config.retry.max_attempts == 3
        assert config.retry.interval_seconds == 5.0
        assert config.retry.batch_size == 10

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
        assert len(app.adapters) == 2, (
            f"Expected 2 built adapters, got {len(app.adapters)}: "
            f"{list(app.adapters.keys())}"
        )

    def test_no_build_failures(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert (
            app.build_failures == []
        ), f"Unexpected build failures: {app.build_failures}"

    def test_routes_parse_correctly(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        routes = config.routes
        assert len(routes.routes) == 2
        route_ids = [r.route_id for r in routes.routes]
        assert "mx_to_mesh" in route_ids
        assert "mesh_to_mx" in route_ids

    def test_routes_register_via_builder(self) -> None:
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert len(app._registered_routes) >= 2, (
            f"Expected >= 2 registered routes, got "
            f"{len(app._registered_routes)}: "
            f"{[r.id for r in app._registered_routes]}"
        )

    def test_toml_retry_section_structure(self) -> None:
        """Raw TOML [retry] section has expected keys."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert "retry" in data
        retry = data["retry"]
        assert retry["enabled"] is True
        assert retry["interval_seconds"] == 5.0
        assert retry["batch_size"] == 10
        assert retry["max_attempts"] == 3

    def test_route_retry_policies_attached(self) -> None:
        """Per-route retry sections are built and attached to pipeline config."""
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        policies = app.pipeline_runner._config.route_retry_policies
        # Both routes declare [routes.<id>.retry] enabled.
        assert "mx_to_mesh" in policies, (
            f"mx_to_mesh missing from route_retry_policies, "
            f"got {sorted(policies.keys())}"
        )
        assert "mesh_to_mx" in policies, (
            f"mesh_to_mx missing from route_retry_policies, "
            f"got {sorted(policies.keys())}"
        )
        assert policies["mx_to_mesh"].max_attempts == 3
        assert policies["mx_to_mesh"].backoff_base == 2.0
        assert policies["mx_to_mesh"].max_delay_seconds == 60.0
        assert policies["mx_to_mesh"].jitter is False
        assert policies["mesh_to_mx"].max_attempts == 3


class TestFakeBridgeSmokeTwoWayRoutes:
    """Verify bidirectional route expansion in fake-bridge-smoke."""

    CONFIG_PATH = CONFIGS_DIR / "fake-bridge-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_bidirectional_route_exists(self) -> None:
        """mx_mesh_bidir is a declared route."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        route_ids = [r.route_id for r in config.routes.routes]
        assert "mx_mesh_bidir" in route_ids

    def test_bidirectional_route_directionality(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        bidir = next(r for r in config.routes.routes if r.route_id == "mx_mesh_bidir")
        assert bidir.directionality.value == "bidirectional"
        assert bidir.enabled is True

    def test_bidirectional_expands_to_two_registered_routes(self) -> None:
        """Bidirectional route registers forward + reverse in the runtime."""
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        registered_ids = [r.id for r in app._registered_routes]
        # mx_mesh_bidir expands to mx_mesh_bidir + mx_mesh_bidir__reverse
        bidir_variants = [
            rid for rid in registered_ids if rid.startswith("mx_mesh_bidir")
        ]
        assert len(bidir_variants) >= 2, (
            f"Bidirectional route should expand to >=2 registered routes, "
            f"got {bidir_variants}"
        )


class TestDockerMatrixBridgeConfig:
    """docker-matrix-bridge.toml TOML structure validation.

    This config uses ``${ENV_VAR}`` syntax for credentials and room IDs.
    The loader's ``_expand_paths_in_dict`` treats ``{...}`` as path
    placeholders, so ``load_config()`` cannot be used (it would try to
    expand ``${MEDRE_HOMESERVER}`` as a path placeholder).  Tests validate
    TOML structure and route shape only — same pattern as TestDockerBridgeSmoke.
    """

    CONFIG_PATH = CONFIGS_DIR / "docker-matrix-bridge.toml"

    def test_toml_parseable(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)

    def test_toml_structure_adapters(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapters = data["adapters"]
        assert "matrix" in adapters
        assert "meshtastic" in adapters
        # Real Matrix adapter with env var placeholders
        mx = adapters["matrix"]["synapse"]
        assert mx["adapter_kind"] == "real"
        assert mx["access_token"] == "${MEDRE_ACCESS_TOKEN}"
        assert mx["homeserver"] == "${MEDRE_HOMESERVER}"
        # Fake Meshtastic adapter
        fake_out = adapters["meshtastic"]["fake_out"]
        assert fake_out["adapter_kind"] == "fake"
        assert fake_out["connection_type"] == "fake"

    def test_expected_adapter_ids(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapter_ids: set[str] = set()
        for _transport, instances in data["adapters"].items():
            for inst_name in instances:
                adapter_ids.add(inst_name)
        assert "synapse" in adapter_ids
        assert "fake_out" in adapter_ids
        assert len(adapter_ids) == 2

    def test_routes_section_structure(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        routes = data["routes"]
        assert "synapse_to_fake" in routes
        route = routes["synapse_to_fake"]
        assert route["source_adapters"] == ["synapse"]
        assert route["dest_adapters"] == ["fake_out"]
        assert route["directionality"] == "source_to_dest"
        assert route["enabled"] is True

    def test_route_adapter_refs_valid(self) -> None:
        """All route source/dest references name adapters that exist."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapter_ids: set[str] = set()
        for _transport, instances in data["adapters"].items():
            for inst_name in instances:
                adapter_ids.add(inst_name)
        for _route_id, route in data["routes"].items():
            for ref in route.get("source_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown source adapter '{ref}'"
            for ref in route.get("dest_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown dest adapter '{ref}'"

    def test_no_real_secrets(self) -> None:
        text = _read(self.CONFIG_PATH)
        hits = _has_real_secrets(text)
        assert hits == [], f"Possible real secrets found: {hits}"


class TestDockerMeshtasticBridgeConfig:
    """docker-meshtastic-bridge.toml TOML structure validation.

    This config uses ``${MESHTASTIC_HOST}`` which the loader treats as a
    path placeholder.  Tests validate TOML structure and route shape only.
    """

    CONFIG_PATH = CONFIGS_DIR / "docker-meshtastic-bridge.toml"

    def test_toml_parseable(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)

    def test_toml_structure_adapters(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapters = data["adapters"]
        assert "meshtastic" in adapters
        assert "matrix" in adapters
        # Real Meshtastic adapter with TCP connection
        daemon = adapters["meshtastic"]["daemon"]
        assert daemon["adapter_kind"] == "real"
        assert daemon["connection_type"] == "tcp"
        assert daemon["host"] == "${MESHTASTIC_HOST}"
        assert daemon["port"] == 4403
        # Fake Matrix adapter
        fake_out = adapters["matrix"]["fake_out"]
        assert fake_out["adapter_kind"] == "fake"

    def test_expected_adapter_ids(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapter_ids: set[str] = set()
        for _transport, instances in data["adapters"].items():
            for inst_name in instances:
                adapter_ids.add(inst_name)
        assert "daemon" in adapter_ids
        assert "fake_out" in adapter_ids
        assert len(adapter_ids) == 2

    def test_routes_section_structure(self) -> None:
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        routes = data["routes"]
        assert "daemon_to_matrix" in routes
        route = routes["daemon_to_matrix"]
        assert route["source_adapters"] == ["daemon"]
        assert route["dest_adapters"] == ["fake_out"]
        assert route["directionality"] == "source_to_dest"
        assert route["enabled"] is True

    def test_route_adapter_refs_valid(self) -> None:
        """All route source/dest references name adapters that exist."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        adapter_ids: set[str] = set()
        for _transport, instances in data["adapters"].items():
            for inst_name in instances:
                adapter_ids.add(inst_name)
        for _route_id, route in data["routes"].items():
            for ref in route.get("source_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown source adapter '{ref}'"
            for ref in route.get("dest_adapters", []):
                assert (
                    ref in adapter_ids
                ), f"Route '{_route_id}' references unknown dest adapter '{ref}'"

    def test_no_real_secrets(self) -> None:
        text = _read(self.CONFIG_PATH)
        hits = _has_real_secrets(text)
        assert hits == [], f"Possible real secrets found: {hits}"


# ===========================================================================
# 10. Env var documentation cross-check
# ===========================================================================


# All configs in the examples/configs directory.
_ALL_CONFIG_FILES = sorted(CONFIGS_DIR.glob("*.toml"))

# Regex for ${ENV_VAR} references in TOML values.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TestEnvVarDocumentation:
    """Extract all ${ENV_VAR} references from example configs and cross-check
    against docker.env.example to ensure documented env vars are complete."""

    def _extract_env_vars(self, path: Path) -> set[str]:
        """Extract all ${VAR} references from a config file."""
        text = _read(path)
        return set(_ENV_VAR_RE.findall(text))

    def test_all_env_vars_listed_in_docker_env(self) -> None:
        """Every ${VAR} in docker-bridge-smoke.toml must appear in
        docker.env.example.  Other configs (docker-matrix-bridge,
        docker-meshtastic-bridge) are Docker integration test configs
        whose env vars are set programmatically by conftest.py fixtures,
        not by operators via docker.env.example."""
        env_text = _read(DOCKER_ENV)
        documented = set(_ENV_VAR_RE.findall(env_text))
        # Also collect bare VAR= lines from the env file.
        for line in env_text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                var_name = stripped.split("=", 1)[0].strip()
                documented.add(var_name)

        # Only check configs that operators are expected to use with
        # docker.env.example.  docker-matrix-bridge and docker-meshtastic-bridge
        # have their own env vars managed by Docker integration test fixtures.
        operator_configs = [
            CONFIGS_DIR / "docker-bridge-smoke.toml",
        ]

        undocumented: dict[str, list[str]] = {}
        for config_path in operator_configs:
            used = self._extract_env_vars(config_path)
            missing = used - documented
            for m in missing:
                undocumented.setdefault(m, []).append(config_path.name)

        if undocumented:
            lines = [
                f"  {var}: used in {', '.join(files)}"
                for var, files in sorted(undocumented.items())
            ]
            pytest.fail(
                "Undocumented env vars (not in docker.env.example):\n"
                + "\n".join(lines)
            )

    def test_no_placeholder_secrets_in_env_refs(self) -> None:
        """${VAR} values must not contain obvious placeholder patterns
        like 'PLACEHOLDER' or 'CHANGEME' as the env var name."""
        for config_path in _ALL_CONFIG_FILES:
            text = _read(config_path)
            for match in _ENV_VAR_RE.finditer(text):
                var_name = match.group(1)
                assert "PLACEHOLDER" not in var_name.upper(), (
                    f"{config_path.name}: env var {var_name!r} contains "
                    f"'PLACEHOLDER' — use a descriptive name instead"
                )
                assert "CHANGEME" not in var_name.upper(), (
                    f"{config_path.name}: env var {var_name!r} contains "
                    f"'CHANGEME' — use a descriptive name instead"
                )

    def test_env_var_summary(self) -> None:
        """Print a summary of all env vars used across configs (informational)."""
        all_vars: dict[str, list[str]] = {}
        for config_path in _ALL_CONFIG_FILES:
            for var in sorted(self._extract_env_vars(config_path)):
                all_vars.setdefault(var, []).append(config_path.name)

        # This test always passes — it's a documentation cross-check.
        # The assertion just ensures the mapping is built correctly.
        assert isinstance(all_vars, dict)
        for var in all_vars:
            assert len(all_vars[var]) >= 1


# ===========================================================================
# 11. Runtime build + route validation (deep)
# ===========================================================================


class TestFakeConfigBuildsRuntime:
    """Build a full runtime from fake-bridge-smoke.toml and assert structure."""

    CONFIG_PATH = CONFIGS_DIR / "fake-bridge-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def _build_app(self):
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        return builder.build()

    def test_build_succeeds_no_exception(self) -> None:
        app = self._build_app()
        assert app is not None

    def test_adapter_count_matches_config(self) -> None:
        app = self._build_app()
        config, _, _ = load_config(str(self.CONFIG_PATH))
        enabled_count = len(
            [1 for _t, _a, rtc in config.adapters.all_configs() if rtc.enabled]
        )
        assert len(app.adapters) == enabled_count, (
            f"Expected {enabled_count} adapters, got {len(app.adapters)}: "
            f"{list(app.adapters.keys())}"
        )

    def test_no_build_failures(self) -> None:
        app = self._build_app()
        assert (
            app.build_failures == []
        ), f"Unexpected build failures: {app.build_failures}"

    def test_storage_is_memory(self) -> None:
        app = self._build_app()
        assert app.storage is not None
        assert str(app.storage._db_path) == ":memory:"

    def test_adapter_ids_are_deterministic(self) -> None:
        """Building twice produces the same adapter IDs in the same order."""
        app1 = self._build_app()
        app2 = self._build_app()
        assert list(app1.adapters.keys()) == list(app2.adapters.keys())


class TestFakeConfigRouteValidate:
    """Route validation on fake-bridge-smoke: deterministic output, correct counts."""

    CONFIG_PATH = CONFIGS_DIR / "fake-bridge-smoke.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def _build_app(self):
        config, _, paths = load_config(str(self.CONFIG_PATH))
        builder = RuntimeBuilder(config, paths)
        return builder.build()

    def test_expected_route_count(self) -> None:
        """6 configured routes (including 1 disabled)."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        assert len(config.routes.routes) == 6

    def test_enabled_route_count(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        enabled = [r for r in config.routes.routes if r.enabled]
        assert len(enabled) == 5

    def test_disabled_route_count(self) -> None:
        config, _, _ = load_config(str(self.CONFIG_PATH))
        disabled = [r for r in config.routes.routes if not r.enabled]
        assert len(disabled) == 1
        assert disabled[0].route_id == "disabled_example"

    def test_registered_routes_deterministic(self) -> None:
        """Building twice produces identical registered route IDs."""
        app1 = self._build_app()
        app2 = self._build_app()
        ids1 = [r.id for r in app1._registered_routes]
        ids2 = [r.id for r in app2._registered_routes]
        assert (
            ids1 == ids2
        ), f"Route registration is not deterministic: {ids1} != {ids2}"

    def test_bidirectional_expands_to_two(self) -> None:
        """mx_mesh_bidir (bidirectional) registers forward + reverse."""
        app = self._build_app()
        registered_ids = [r.id for r in app._registered_routes]
        assert "mx_mesh_bidir" in registered_ids
        # Reverse variant uses __rev_N naming convention.
        bidir_variants = [
            rid for rid in registered_ids if rid.startswith("mx_mesh_bidir__rev")
        ]
        assert len(bidir_variants) >= 1, (
            f"Bidirectional route should produce reverse variant, "
            f"got {sorted(registered_ids)}"
        )

    def test_unidirectional_routes_have_no_reverse(self) -> None:
        """Unidirectional routes do not produce __reverse variants."""
        app = self._build_app()
        registered_ids = [r.id for r in app._registered_routes]
        for rid in registered_ids:
            if rid == "mx_mesh_bidir" or rid == "mx_mesh_bidir__reverse":
                continue
            assert not rid.endswith(
                "__reverse"
            ), f"Unidirectional route should not have reverse: {rid}"

    def test_all_enabled_route_ids_present(self) -> None:
        """Every enabled route ID appears in registered routes."""
        config, _, _ = load_config(str(self.CONFIG_PATH))
        app = self._build_app()
        registered_ids = {r.id for r in app._registered_routes}
        for route in config.routes.routes:
            if route.enabled:
                # For bidirectional, the forward route has the original ID.
                assert route.route_id in registered_ids, (
                    f"Enabled route {route.route_id} not in registered: "
                    f"{sorted(registered_ids)}"
                )


# ===========================================================================
# 12. Docker configs: clean errors for unresolved env var placeholders
# ===========================================================================


class TestDockerConfigsEnvVarValidation:
    """Verify Docker configs with unresolved ``${ENV_VAR}`` placeholders
    produce clean, operator-actionable errors — not raw tracebacks.

    The loader's ``_expand_paths_in_dict`` treats ``{...}`` as path
    placeholders.  When a config contains ``${MEDRE_HOMESERVER}``, the
    ``{MEDRE_HOMESERVER}`` substring triggers path expansion.  Since
    ``MEDRE_HOMESERVER`` is not a recognised path placeholder
    (only ``{config}``, ``{state}``, etc. are valid), the loader raises
    ``ConfigFileError`` wrapping ``MedrePathsError``.  This test verifies
    the error message is clean and mentions the problematic field.
    """

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_docker_matrix_bridge_fails_cleanly_without_env_vars(self) -> None:
        """Loading docker-matrix-bridge.toml without env vars produces a
        ConfigFileError about the path placeholder, not a raw traceback."""
        config_path = CONFIGS_DIR / "docker-matrix-bridge.toml"

        # Step 1: TOML is valid (raw parse succeeds)
        raw = _read(config_path)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)

        # Step 2: load_config fails with ConfigFileError
        with pytest.raises(ConfigFileError) as exc_info:
            load_config(str(config_path))

        error_msg = str(exc_info.value)

        # Step 3: Error mentions the placeholder problem
        assert (
            "placeholder" in error_msg.lower()
        ), f"Expected error to mention 'placeholder', got: {error_msg}"

        # Step 4: Error mentions the specific config field that caused it
        assert any(
            field in error_msg for field in ("homeserver", "user_id", "access_token")
        ), (
            f"Expected error to mention a config field (homeserver/user_id/access_token), "
            f"got: {error_msg}"
        )

        # Step 5: Error mentions the env var name so operator knows what to set
        assert any(
            var in error_msg
            for var in ("MEDRE_HOMESERVER", "MEDRE_USER_ID", "MEDRE_ACCESS_TOKEN")
        ), f"Expected error to mention an env var name, got: {error_msg}"

        # Step 6: No raw traceback — error is clean
        assert "Traceback" not in error_msg
        assert "File " not in error_msg

    def test_docker_meshtastic_bridge_fails_cleanly_without_env_vars(self) -> None:
        """Loading docker-meshtastic-bridge.toml without env vars produces
        a ConfigFileError about the host placeholder."""
        config_path = CONFIGS_DIR / "docker-meshtastic-bridge.toml"

        # TOML is valid
        raw = _read(config_path)
        data = tomllib.loads(raw)
        assert isinstance(data, dict)

        # load_config fails with ConfigFileError
        with pytest.raises(ConfigFileError) as exc_info:
            load_config(str(config_path))

        error_msg = str(exc_info.value)

        # Error is clean and actionable
        assert (
            "placeholder" in error_msg.lower()
        ), f"Expected error to mention 'placeholder', got: {error_msg}"
        assert any(
            field in error_msg for field in ("host", "MESHTASTIC_HOST")
        ), f"Expected error to mention 'host' or 'MESHTASTIC_HOST', got: {error_msg}"
        assert "Traceback" not in error_msg


# ===========================================================================
# 13. Live Matrix ↔ Meshtastic: explicit route targeting fields
# ===========================================================================


class TestLiveMatrixMeshtasticTargeting:
    """The live-matrix-meshtastic config must carry explicit route targeting
    fields (source_room, dest_channel) on its bidirectional route so that
    both expansion legs are fully specified without relying on implicit defaults."""

    CONFIG_PATH = CONFIGS_DIR / "live-matrix-meshtastic.toml"

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        # Isolate from operator's Matrix sidecar credentials so the test
        # is deterministic regardless of local ~/.config/medre/credentials/.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    def test_load_raises_credential_error(self) -> None:
        """Loading with empty credentials raises MatrixConfigError,
        confirming the config is properly structured but credential-incomplete."""
        with pytest.raises(MatrixConfigError):
            load_config(str(self.CONFIG_PATH))

    def test_canonical_config_contains_explicit_targeting(self) -> None:
        """The bidirectional route has source_room + dest_channel targeting fields."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        routes = data["routes"]

        bridge = routes["matrix_radio_bridge"]
        assert bridge["directionality"] == "bidirectional"
        assert "source_room" in bridge, "matrix_radio_bridge missing source_room"
        assert "dest_channel" in bridge, "matrix_radio_bridge missing dest_channel"

    def test_bidirectional_route_target_values_are_nonempty(self) -> None:
        """Bidirectional route targeting fields must be non-empty strings."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        bridge = data["routes"]["matrix_radio_bridge"]

        assert (
            isinstance(bridge["source_room"], str) and bridge["source_room"]
        ), "matrix_radio_bridge source_room must be a non-empty string"
        assert (
            isinstance(bridge["dest_channel"], str) and bridge["dest_channel"]
        ), "matrix_radio_bridge dest_channel must be a non-empty string"

    def test_live_config_does_not_rely_on_implicit_target(self) -> None:
        """The bidirectional route has explicit targeting fields — no implicit
        channel defaults used."""
        raw = _read(self.CONFIG_PATH)
        data = tomllib.loads(raw)
        bridge = data["routes"]["matrix_radio_bridge"]

        targeting_fields = {
            "source_room": bridge["source_room"],
            "dest_channel": bridge["dest_channel"],
        }
        for field, value in targeting_fields.items():
            assert isinstance(value, str) and value, (
                f"Targeting field {field!r} must be a non-empty string, "
                f"got {value!r}"
            )

    def test_targeting_comments_present_in_toml(self) -> None:
        """The TOML file includes comments explaining the targeting fields."""
        raw = _read(self.CONFIG_PATH)
        # Check for comment explaining source_room/dest_room format
        assert (
            "!opaque:server" in raw or "Matrix room IDs" in raw
        ), "Missing comment explaining Matrix room ID format"
        # Check for comment explaining channel index format
        assert (
            "channel index" in raw.lower() or "Meshtastic channel" in raw
        ), "Missing comment explaining Meshtastic channel index format"
