"""Config-to-runtime parity tests.

Proves that fake, Docker, and minimal configs all traverse the same
``load_config`` → ``RuntimeBuilder.build()`` path and behave predictably
when optional dependencies or environment variables are missing.

Every test follows the same contract:
    1. Load config via ``medre.config.loader.load_config``.
    2. Build runtime via ``medre.runtime.builder.RuntimeBuilder.build()``.
    3. Assert structural invariants on the resulting ``MedreApp``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from medre.config.adapters.errors import (
    LxmfConfigError,
    MatrixConfigError,
    MeshCoreConfigError,
)
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import RuntimeConfig
from medre.config.paths import MedrePaths
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError

# ---------------------------------------------------------------------------
# Paths & helpers
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = _ROOT / "examples" / "configs"


def _write_yaml(tmp_path: Path, content: str, name: str = "test.yaml") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _resolve_docker_placeholders(raw: str) -> str:
    """Replace ``${ENV_VAR}`` placeholders with safe defaults.

    The loader treats ``{name}`` as path placeholders, so Docker configs
    that use ``${MEDRE_HOMESERVER}`` must be pre-resolved.
    """
    return (
        raw.replace("${MEDRE_HOMESERVER}", "https://synapse.test")
        .replace("${MEDRE_USER_ID}", "@bot:test")
        .replace("${MEDRE_ACCESS_TOKEN}", "PLACEHOLDER")
        .replace("${MEDRE_ROOM_ID}", "!room:test")
        .replace("${MESHTASTIC_HOST}", "localhost")
    )


def _load_docker_config(
    config_name: str,
    tmp_path: Path,
) -> tuple[RuntimeConfig, object, MedrePaths]:
    """Read a Docker example config, resolve placeholders, write to tmp, load."""
    raw = (CONFIGS_DIR / config_name).read_text(encoding="utf-8")
    resolved = _resolve_docker_placeholders(raw)
    config_path = _write_yaml(tmp_path, resolved, config_name)
    return load_config(str(config_path))


def _load_any(config_name: str, tmp_path: Path):
    """Load a config, resolving Docker placeholders if needed."""
    raw = (CONFIGS_DIR / config_name).read_text(encoding="utf-8")
    if "${" in raw:
        config_path = _write_yaml(
            tmp_path,
            _resolve_docker_placeholders(raw),
            config_name,
        )
    else:
        config_path = CONFIGS_DIR / config_name
    return load_config(str(config_path))


# ===========================================================================
# Test 1: fake-bridge-smoke goes through the full path
# ===========================================================================


class TestFakeConfigRuntimePath:
    """``fake-bridge-smoke.yaml`` loads and builds end-to-end."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_load_and_build(self) -> None:
        config, _source, paths = load_config(
            str(CONFIGS_DIR / "fake-bridge-smoke.yaml")
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        # app is not None and is the correct type
        assert isinstance(app, MedreApp)

        # At least one adapter built
        assert (
            len(app.adapters) >= 1
        ), f"Expected >= 1 adapter, got {list(app.adapters.keys())}"

        # Build failures should be empty for all-fake config
        assert (
            app.build_failures == []
        ), f"Unexpected build failures: {app.build_failures}"

        # started_adapter_ids is empty pre-start, but adapters dict should
        # contain exactly the enabled adapters from config
        enabled_ids = {aid for aid, _ in config.adapters.all_enabled()}
        assert set(app.adapters.keys()) == enabled_ids, (
            f"Built adapters {set(app.adapters.keys())} != "
            f"enabled adapters {enabled_ids}"
        )


# ===========================================================================
# Test 2: docker-matrix-bridge degrades predictably
# ===========================================================================


class TestDockerMatrixConfigDegradesPredictably:
    """Docker Matrix bridge config parses and degrades when the real
    Matrix SDK is unavailable."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_parse_succeeds(self, tmp_path: Path) -> None:
        config, _source, _paths = _load_docker_config(
            "docker-matrix-bridge.yaml", tmp_path
        )
        assert config.runtime.name == "docker-matrix-bridge"

    def test_build_degrades_cleanly(self, tmp_path: Path) -> None:
        config, _source, paths = _load_docker_config(
            "docker-matrix-bridge.yaml", tmp_path
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert isinstance(app, MedreApp)
        # fake_out (fake Meshtastic) must always build
        assert "fake_out" in app.adapters

        for failure in app.build_failures:
            assert isinstance(failure.error, RuntimeConfigError), (
                f"Expected RuntimeConfigError, got "
                f"{type(failure.error).__name__}: {failure.error}"
            )


# ===========================================================================
# Test 3: docker-meshtastic-bridge degrades predictably
# ===========================================================================


class TestDockerMeshtasticConfigDegradesPredictably:
    """Docker Meshtastic bridge config parses and degrades when
    the Meshtastic SDK (mtjk) is not available."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_parse_succeeds(self, tmp_path: Path) -> None:
        config, _source, _paths = _load_docker_config(
            "docker-meshtastic-bridge.yaml", tmp_path
        )
        assert config.runtime.name == "docker-meshtastic-bridge"

    def test_build_degrades_cleanly(self, tmp_path: Path) -> None:
        config, _source, paths = _load_docker_config(
            "docker-meshtastic-bridge.yaml", tmp_path
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert isinstance(app, MedreApp)
        # fake_out (fake Matrix) must always build
        assert "fake_out" in app.adapters

        for failure in app.build_failures:
            assert isinstance(failure.error, RuntimeConfigError)
            assert failure.adapter_id


# ===========================================================================
# Test 4: disabled adapters produce no validation errors
# ===========================================================================


class TestDisabledAdaptersNoValidationErrors:
    """A disabled adapter referencing a non-existent feature must be silently
    skipped — it must not produce validation errors or affect the build."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_disabled_adapter_silently_skipped(self, tmp_path: Path) -> None:
        yaml = textwrap.dedent("""\
        runtime:
          name: disabled-test
        storage:
          backend: memory
        logging:
          level: INFO
        adapters:
          matrix:
            enabled_one:
              enabled: true
              adapter_kind: fake
              homeserver: https://fake.local
              user_id: "@bot:fake.local"
              access_token: tok
              encryption_mode: plaintext
          meshtastic:
            ghost:
              enabled: false
              adapter_kind: fake
              connection_type: fake
        """)
        config_path = _write_yaml(tmp_path, yaml)
        config, _source, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        # Only the enabled adapter should be in app.adapters
        assert "enabled_one" in app.adapters
        assert "ghost" not in app.adapters

        # No build failures from the disabled adapter
        assert app.build_failures == [], (
            f"Disabled adapter should not cause build failures: "
            f"{app.build_failures}"
        )

        # Verify adapter count matches enabled count
        enabled_count = len([aid for aid, _ in config.adapters.all_enabled()])
        assert len(app.adapters) == enabled_count


# ===========================================================================
# Test 5: unknown adapter is a hard error
# ===========================================================================


class TestUnknownAdapterIsHardError:
    """Referencing a transport type that does not exist must raise
    ``RuntimeConfigError`` — not a raw traceback crash."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_unknown_transport_raises_clean_error(self, tmp_path: Path) -> None:
        yaml = textwrap.dedent("""\
        runtime:
          name: bad-transport-test
        storage:
          backend: memory
        adapters:
          matrix:
            ok:
              enabled: true
              adapter_kind: fake
              homeserver: https://fake.local
              user_id: "@bot:fake.local"
              access_token: tok
              encryption_mode: plaintext
        """)
        config_path = _write_yaml(tmp_path, yaml)
        config, _source, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)

        from unittest.mock import MagicMock

        mock_rtc = MagicMock(adapter_kind="fake")

        with pytest.raises(
            RuntimeConfigError, match="Unknown transport type"
        ) as exc_info:
            builder._build_single_adapter("nonexistent_transport", "bad", mock_rtc)
        assert "nonexistent_transport" in str(exc_info.value)

    def test_unknown_transport_error_is_not_traceback(self, tmp_path: Path) -> None:
        yaml = textwrap.dedent("""\
        runtime:
          name: error-cleanliness-test
        storage:
          backend: memory
        adapters:
          matrix:
            ok:
              enabled: true
              adapter_kind: fake
              homeserver: https://fake.local
              user_id: "@bot:fake.local"
              access_token: tok
              encryption_mode: plaintext
        """)
        config_path = _write_yaml(tmp_path, yaml)
        config, _source, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)

        from unittest.mock import MagicMock

        try:
            builder._build_single_adapter("bogus", "x", MagicMock(adapter_kind="fake"))
        except RuntimeConfigError:
            pass
        except Exception as exc:
            pytest.fail(f"Expected RuntimeConfigError, got {type(exc).__name__}: {exc}")


# ===========================================================================
# Test 6: minimal config builds with empty adapters
# ===========================================================================


class TestMinimalConfigBuilds:
    """A config with only runtime/storage/logging sections and no adapters
    must build successfully, yielding an empty adapter dict."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_minimal_config_no_adapters_no_routes(self, tmp_path: Path) -> None:
        yaml = textwrap.dedent("""\
        runtime:
          name: minimal-test
          shutdown_timeout_seconds: 5
        storage:
          backend: memory
        logging:
          level: INFO
        """)
        config_path = _write_yaml(tmp_path, yaml)
        config, _source, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert isinstance(app, MedreApp)
        assert app.adapters == {}
        assert app.build_failures == []
        assert app.event_bus is not None
        assert app.router is not None
        assert app.storage is not None
        assert app.pipeline_runner is not None
        assert app.shutdown_event is not None

    def test_minimal_config_no_routes_registered(self, tmp_path: Path) -> None:
        yaml = textwrap.dedent("""\
        runtime:
          name: no-routes-test
        storage:
          backend: memory
        logging:
          level: INFO
        """)
        config_path = _write_yaml(tmp_path, yaml)
        config, _source, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert len(app._registered_routes) == 0


# ===========================================================================
# Test 7: all example configs use the same loader path
# ===========================================================================


class TestExampleConfigsUseSameLoader:
    """Every shipped example config is loaded through the same
    ``load_config`` → ``RuntimeBuilder.build()`` path."""

    DIRECT_CONFIGS = (
        "fake-bridge-smoke.yaml",
        "fake-multi-adapter.yaml",
    )
    RESOLVED_CONFIGS = (
        "docker-matrix-bridge.yaml",
        "docker-meshtastic-bridge.yaml",
    )
    ALL_CONFIGS = DIRECT_CONFIGS + RESOLVED_CONFIGS

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def _load_any(self, config_name: str, tmp_path: Path):
        """Load a config, resolving Docker placeholders if needed."""
        raw = (CONFIGS_DIR / config_name).read_text(encoding="utf-8")
        if "${" in raw:
            config_path = _write_yaml(
                tmp_path,
                _resolve_docker_placeholders(raw),
                config_name,
            )
        else:
            config_path = CONFIGS_DIR / config_name
        return load_config(str(config_path))

    @pytest.mark.parametrize("config_name", ALL_CONFIGS)
    def test_loads_via_load_config(self, config_name: str, tmp_path: Path) -> None:
        config, _source, _paths = self._load_any(config_name, tmp_path)
        assert config is not None
        assert hasattr(config, "runtime")
        assert hasattr(config, "adapters")

    @pytest.mark.parametrize("config_name", ALL_CONFIGS)
    def test_builds_via_runtime_builder(self, config_name: str, tmp_path: Path) -> None:
        config, _source, paths = self._load_any(config_name, tmp_path)
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert isinstance(app, MedreApp)
        assert app.event_bus is not None
        assert app.router is not None
        assert app.storage is not None

    @pytest.mark.parametrize("config_name", ALL_CONFIGS)
    def test_build_failures_are_clean(self, config_name: str, tmp_path: Path) -> None:
        """Any build failures must be RuntimeConfigError — not raw tracebacks."""
        config, _source, paths = self._load_any(config_name, tmp_path)
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        for failure in app.build_failures:
            assert isinstance(failure.error, RuntimeConfigError), (
                f"{config_name}: build failure for {failure.adapter_id!r} "
                f"must be RuntimeConfigError, got "
                f"{type(failure.error).__name__}: {failure.error}"
            )


# ===========================================================================
# TC-003..TC-005: minimal and credential-required configs
# ===========================================================================

# Minimal single-adapter configs with adapter_kind: real.  These fail at
# load_config on missing hardware fields (host, ble_address, storage_path),
# NOT on ConfigValidationError for adapter_kind — proving the F-002 fix.
MINIMAL_CONFIGS = (
    "lxmf-receiver.yaml",
    "lxmf-sender.yaml",
    "meshcore-lab.yaml",
    "meshcore-tbeam.yaml",
)

# Configs that require real credentials or hardware to fully load/build.
# (config_name, expected_error_or_None) — None means load_config succeeds
# (hardware check happens only at build time).
CREDENTIAL_REQUIRED_CONFIGS: tuple[tuple[str, type[Exception] | None], ...] = (
    ("matrix.yaml", MatrixConfigError),
    ("meshtastic-serial.yaml", None),
    ("mixed-matrix-meshtastic.yaml", MatrixConfigError),
    ("live-matrix-meshtastic.yaml", MatrixConfigError),
    ("live-matrix-meshtastic-channel-map.yaml", MatrixConfigError),
)


@pytest.fixture()
def _medre_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))


# -- TC-003/TC-004: minimal configs fail on hardware, not adapter_kind ----


@pytest.mark.parametrize("config_name", MINIMAL_CONFIGS)
def test_minimal_config_fails_on_hardware_not_adapter_kind(
    config_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 4 minimal configs use adapter_kind: real but are missing
    required hardware fields (host, ble_address, storage_path).

    They must fail with the transport-specific config error — NOT with
    ConfigValidationError for an invalid adapter_kind.  This verifies
    the F-002 fix: adapter_kind is now 'real' (valid) and the configs
    progress past the wrapper into adapter-specific validation.
    """
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    with pytest.raises((LxmfConfigError, MeshCoreConfigError)) as exc_info:
        _load_any(config_name, tmp_path)
    assert not isinstance(exc_info.value, ConfigValidationError)


# -- TC-005: credential-required configs fail on credentials, not structure -


@pytest.mark.parametrize(
    ("config_name", "expected_error"),
    CREDENTIAL_REQUIRED_CONFIGS,
    ids=[c[0] for c in CREDENTIAL_REQUIRED_CONFIGS],
)
def test_credential_required_config_fails_on_credentials(
    config_name: str,
    expected_error: type[Exception] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential-required configs must either load successfully
    (hardware check deferred to build) or fail on credentials — never
    on ConfigValidationError for adapter_kind structure.
    """
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    if expected_error is None:
        # meshtastic-serial loads OK — hardware check is at build time.
        config, _source, _paths = _load_any(config_name, tmp_path)
        assert config is not None
        assert hasattr(config, "runtime")
    else:
        with pytest.raises(expected_error) as exc_info:
            _load_any(config_name, tmp_path)
        assert not isinstance(exc_info.value, ConfigValidationError)
