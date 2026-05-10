"""Tests for medre.runtime.builder: multi-adapter construction,
disabled adapters, error handling."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.base import BaseAdapter
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.lxmf.config import LxmfConfig
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from medre.runtime.app import MedreApp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _make_fake_matrix_config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="matrix_main",
        homeserver="https://matrix.test",
        user_id="@bot:test",
        access_token="test-tok",
        encryption_mode="plaintext",
    )


def _make_fake_meshtastic_config() -> MeshtasticConfig:
    return MeshtasticConfig(
        adapter_id="mesh_radio",
        connection_type="fake",
    ).validate()


def _make_fake_meshcore_config() -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id="meshcore_node",
        connection_type="fake",
    ).validate()


def _make_fake_lxmf_config() -> LxmfConfig:
    return LxmfConfig(
        adapter_id="lxmf_local",
        connection_type="fake",
    ).validate()


def _make_all_enabled_config() -> RuntimeConfig:
    """RuntimeConfig with all adapter types enabled (fake connections)."""
    matrix_rt = MatrixRuntimeConfig(
        adapter_id="matrix_main",
        enabled=True,
        config=_make_fake_matrix_config(),
    )
    meshtastic_rt = MeshtasticRuntimeConfig(
        adapter_id="mesh_radio",
        enabled=True,
        config=_make_fake_meshtastic_config(),
    )
    meshcore_rt = MeshCoreRuntimeConfig(
        adapter_id="meshcore_node",
        enabled=True,
        config=_make_fake_meshcore_config(),
    )
    lxmf_rt = LxmfRuntimeConfig(
        adapter_id="lxmf_local",
        enabled=True,
        config=_make_fake_lxmf_config(),
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-builder"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="sqlite"),
        adapters=AdapterConfigSet(
            matrix={"main": matrix_rt},
            meshtastic={"radio": meshtastic_rt},
            meshcore={"node": meshcore_rt},
            lxmf={"local": lxmf_rt},
        ),
    )


def _make_disabled_config() -> RuntimeConfig:
    """RuntimeConfig where all adapters are disabled."""
    matrix_rt = MatrixRuntimeConfig(
        adapter_id="matrix_off",
        enabled=False,
        config=_make_fake_matrix_config(),
    )
    meshtastic_rt = MeshtasticRuntimeConfig(
        adapter_id="mesh_off",
        enabled=False,
        config=_make_fake_meshtastic_config(),
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(),
        logging=LoggingConfig(),
        storage=StorageConfig(),
        adapters=AdapterConfigSet(
            matrix={"off": matrix_rt},
            meshtastic={"off": meshtastic_rt},
        ),
    )


def _make_empty_config() -> RuntimeConfig:
    """RuntimeConfig with no adapters at all."""
    return RuntimeConfig(
        runtime=RuntimeOptions(),
        logging=LoggingConfig(),
        storage=StorageConfig(),
    )


# ---------------------------------------------------------------------------
# Builder imports
# ---------------------------------------------------------------------------


class TestBuilderImports:
    """RuntimeBuilder and related types are importable."""

    def test_import_runtime_builder(self) -> None:
        from medre.runtime.builder import RuntimeBuilder
        assert RuntimeBuilder is not None

    def test_import_medre_app(self) -> None:
        from medre.runtime.app import MedreApp
        assert MedreApp is not None

    def test_import_runtime_errors(self) -> None:
        from medre.runtime.errors import RuntimeConfigError
        assert RuntimeConfigError is not None


# ---------------------------------------------------------------------------
# Builder construction
# ---------------------------------------------------------------------------


class TestBuilderConstruction:
    """RuntimeBuilder can be constructed with config and paths."""

    def test_builder_init(self, tmp_paths: MedrePaths) -> None:
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        assert builder is not None

    def test_builder_stores_config(self, tmp_paths: MedrePaths) -> None:
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        assert builder._config is config
        assert builder._paths is tmp_paths


# ---------------------------------------------------------------------------
# Build with mocked adapters
# ---------------------------------------------------------------------------


class TestBuildWithMockedAdapters:
    """Test builder.build() with mocked adapter factories."""

    def test_build_returns_medre_app(self, tmp_paths: MedrePaths) -> None:
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert isinstance(app, MedreApp)

    def test_build_with_mocked_matrix_adapter(self, tmp_paths: MedrePaths) -> None:
        """When Matrix adapter factory succeeds, adapter appears in app."""
        config = _make_all_enabled_config()
        builder = RuntimeBuilder(config, tmp_paths)

        mock_adapter = MagicMock(spec=BaseAdapter)
        with patch.object(builder, "_build_single_adapter", return_value=mock_adapter):
            app = builder.build()

        # The app should have adapters
        assert isinstance(app, MedreApp)
        assert len(app.adapters) > 0

    def test_build_creates_subsystems(self, tmp_paths: MedrePaths) -> None:
        """Builder creates all expected subsystem references."""
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        assert app.event_bus is not None
        assert app.router is not None
        assert app.storage is not None
        assert app.pipeline_runner is not None
        assert app.shutdown_event is not None


# ---------------------------------------------------------------------------
# Disabled adapters
# ---------------------------------------------------------------------------


class TestDisabledAdapters:
    """Disabled adapters are not constructed."""

    def test_disabled_adapters_not_in_built_app(self, tmp_paths: MedrePaths) -> None:
        config = _make_disabled_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        # Disabled adapters should not be in the adapters dict
        assert "matrix_off" not in app.adapters
        assert "mesh_off" not in app.adapters

    def test_empty_adapters_config_builds(self, tmp_paths: MedrePaths) -> None:
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        assert app.adapters == {}


# ---------------------------------------------------------------------------
# Adapter contexts and IDs
# ---------------------------------------------------------------------------


class TestAdapterIds:
    """Adapter configs carry the correct adapter_id."""

    def test_matrix_adapter_id(self) -> None:
        cfg = _make_fake_matrix_config()
        rt = MatrixRuntimeConfig(adapter_id="custom_id", enabled=True, config=cfg)
        assert rt.adapter_id == "custom_id"

    def test_meshtastic_adapter_id(self) -> None:
        cfg = _make_fake_meshtastic_config()
        rt = MeshtasticRuntimeConfig(adapter_id="my_radio", enabled=True, config=cfg)
        assert rt.adapter_id == "my_radio"

    def test_meshcore_adapter_id(self) -> None:
        cfg = _make_fake_meshcore_config()
        rt = MeshCoreRuntimeConfig(adapter_id="my_node", enabled=True, config=cfg)
        assert rt.adapter_id == "my_node"

    def test_lxmf_adapter_id(self) -> None:
        cfg = _make_fake_lxmf_config()
        rt = LxmfRuntimeConfig(adapter_id="my_lxmf", enabled=True, config=cfg)
        assert rt.adapter_id == "my_lxmf"


# ---------------------------------------------------------------------------
# all_enabled / all_configs
# ---------------------------------------------------------------------------


class TestAdapterConfigSet:
    """AdapterConfigSet helper methods return correct results."""

    def test_all_enabled_returns_only_enabled(self) -> None:
        matrix_on = MatrixRuntimeConfig(
            adapter_id="on", enabled=True,
            config=_make_fake_matrix_config(),
        )
        matrix_off = MatrixRuntimeConfig(
            adapter_id="off", enabled=False,
            config=_make_fake_matrix_config(),
        )
        acs = AdapterConfigSet(
            matrix={"on": matrix_on, "off": matrix_off},
        )
        enabled = acs.all_enabled()
        ids = [aid for aid, _ in enabled]
        assert "on" in ids
        assert "off" not in ids

    def test_all_configs_returns_all(self) -> None:
        matrix_on = MatrixRuntimeConfig(
            adapter_id="on", enabled=True,
            config=_make_fake_matrix_config(),
        )
        matrix_off = MatrixRuntimeConfig(
            adapter_id="off", enabled=False,
            config=_make_fake_matrix_config(),
        )
        acs = AdapterConfigSet(
            matrix={"on": matrix_on, "off": matrix_off},
        )
        all_cfg = acs.all_configs()
        ids = [(t, aid) for t, aid, _ in all_cfg]
        assert ("matrix", "on") in ids
        assert ("matrix", "off") in ids

    def test_empty_adapters_returns_empty(self) -> None:
        acs = AdapterConfigSet()
        assert acs.all_enabled() == []
        assert acs.all_configs() == []


# ---------------------------------------------------------------------------
# Build with real fake adapters (if optional deps available)
# ---------------------------------------------------------------------------


class TestBuildRealFakeAdapters:
    """Build with real fake-transport adapters where available."""

    def test_matrix_config_constructs(self) -> None:
        """MatrixConfig can be constructed for fake use."""
        cfg = MatrixConfig(
            adapter_id="test",
            homeserver="https://matrix.test",
            user_id="@bot:test",
            access_token="tok",
            encryption_mode="plaintext",
        )
        assert cfg.adapter_id == "test"

    def test_meshtastic_fake_config_validates(self) -> None:
        cfg = MeshtasticConfig(adapter_id="test", connection_type="fake").validate()
        assert cfg.connection_type == "fake"

    def test_meshcore_fake_config_validates(self) -> None:
        cfg = MeshCoreConfig(adapter_id="test", connection_type="fake").validate()
        assert cfg.connection_type == "fake"

    def test_lxmf_fake_config_validates(self) -> None:
        cfg = LxmfConfig(adapter_id="test", connection_type="fake").validate()
        assert cfg.connection_type == "fake"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestBuilderErrorCases:
    """Edge cases and error handling in the builder."""

    def test_unknown_transport_type_skipped(self, tmp_paths: MedrePaths) -> None:
        """An adapter with a transport type not in _ADAPTER_BUILDERS is skipped."""
        # We can't easily create a config with an unknown transport type
        # since AdapterConfigSet has fixed fields. Instead, test that
        # _build_single_adapter handles unknown transports.
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)

        # Call _build_single_adapter with an unknown transport
        result = builder._build_single_adapter("unknown_transport", "test_id", MagicMock())
        assert result is None

    def test_adapter_with_none_config_skipped(self, tmp_paths: MedrePaths) -> None:
        """Adapter with config=None is skipped."""
        rt = MatrixRuntimeConfig(adapter_id="no_cfg", enabled=True, config=None)
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"no_cfg": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_single_adapter("matrix", "no_cfg", rt)
        assert result is None
