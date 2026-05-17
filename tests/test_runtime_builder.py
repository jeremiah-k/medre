"""Tests for medre.runtime.builder: multi-adapter construction,
disabled adapters, error handling."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
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
from medre.core.routing.router import Router
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from medre.runtime.app import MedreApp
from medre.runtime.route_engine import RouteValidationError, register_routes
from medre.runtime.routes import RouteConfig, RouteConfigSet


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
        """An adapter with a transport type not in _ADAPTER_BUILDERS raises."""
        config = _make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)

        with pytest.raises(RuntimeConfigError, match="Unknown transport type"):
            builder._build_single_adapter("unknown_transport", "test_id", MagicMock())

    def test_real_adapter_construction_error_wrapped(self, tmp_paths: MedrePaths) -> None:
        """Real adapter construction exception is wrapped in RuntimeConfigError."""
        cfg = _make_fake_matrix_config()
        rt = MatrixRuntimeConfig(
            adapter_id="fail_adapter",
            enabled=True,
            adapter_kind="real",
            config=cfg,
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"fail": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Patch _ADAPTER_BUILDERS to inject a factory that raises
        from medre.runtime.builder import _AdapterFactory
        failing_factory = MagicMock(spec=_AdapterFactory)
        failing_factory.build.side_effect = ImportError("no module")
        import medre.runtime.builder as builder_mod
        original = builder_mod._ADAPTER_BUILDERS["matrix"]
        builder_mod._ADAPTER_BUILDERS["matrix"] = failing_factory
        try:
            with pytest.raises(RuntimeConfigError, match="Failed to build adapter"):
                builder._build_single_adapter("matrix", "fail_adapter", rt)
        finally:
            builder_mod._ADAPTER_BUILDERS["matrix"] = original

    def test_adapter_with_none_config_skipped(self, tmp_paths: MedrePaths) -> None:
        """Adapter with config=None raises RuntimeConfigError."""
        rt = MatrixRuntimeConfig(adapter_id="no_cfg", enabled=True, config=None)
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"no_cfg": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        with pytest.raises(RuntimeConfigError, match="no config"):
            builder._build_single_adapter("matrix", "no_cfg", rt)


# ---------------------------------------------------------------------------
# adapter_kind = "fake" builder dispatch
# ---------------------------------------------------------------------------


class TestAdapterKindFake:
    """adapter_kind='fake' builds Fake*Adapter without optional SDKs."""

    def test_fake_matrix_builds_without_nio(self, tmp_paths: MedrePaths) -> None:
        """Fake Matrix adapter builds even without mindroom-nio."""
        rt = MatrixRuntimeConfig(
            adapter_id="fake_matrix",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"fake_matrix": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "fake_matrix" in adapters
        assert adapters["fake_matrix"].platform == "matrix"

    def test_fake_meshtastic_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake Meshtastic adapter builds without meshtastic SDK."""
        rt = MeshtasticRuntimeConfig(
            adapter_id="fake_mesh",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshtastic={"fake_mesh": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "fake_mesh" in adapters
        assert adapters["fake_mesh"].platform == "meshtastic"

    def test_fake_meshcore_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake MeshCore adapter builds without meshcore SDK."""
        rt = MeshCoreRuntimeConfig(
            adapter_id="fake_core",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshcore_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshcore={"fake_core": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "fake_core" in adapters
        assert adapters["fake_core"].platform == "meshcore"

    def test_fake_lxmf_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake LXMF adapter builds without Reticulum."""
        rt = LxmfRuntimeConfig(
            adapter_id="fake_lxmf",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_lxmf_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(lxmf={"fake_lxmf": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "fake_lxmf" in adapters
        assert adapters["fake_lxmf"].platform == "lxmf"

    def test_fake_unknown_transport_raises(self, tmp_paths: MedrePaths) -> None:
        """Fake adapter for unknown transport raises RuntimeConfigError."""
        rt = MatrixRuntimeConfig(
            adapter_id="bad",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"bad": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        with pytest.raises(RuntimeConfigError, match="Unknown transport type"):
            builder._build_single_adapter("nonexistent", "bad", rt)

    def test_fake_multi_adapter_all_build(self, tmp_paths: MedrePaths) -> None:
        """All four fake adapters build together."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="fake-multi-test"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": MatrixRuntimeConfig(
                    adapter_id="fm", enabled=True, adapter_kind="fake",
                    config=_make_fake_matrix_config(),
                )},
                meshtastic={"ft": MeshtasticRuntimeConfig(
                    adapter_id="ft", enabled=True, adapter_kind="fake",
                    config=_make_fake_meshtastic_config(),
                )},
                meshcore={"fc": MeshCoreRuntimeConfig(
                    adapter_id="fc", enabled=True, adapter_kind="fake",
                    config=_make_fake_meshcore_config(),
                )},
                lxmf={"fl": LxmfRuntimeConfig(
                    adapter_id="fl", enabled=True, adapter_kind="fake",
                    config=_make_fake_lxmf_config(),
                )},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert len(app.adapters) == 4
        platforms = {a.platform for a in app.adapters.values()}
        assert platforms == {"matrix", "meshtastic", "meshcore", "lxmf"}


# ---------------------------------------------------------------------------
# adapter_kind validation
# ---------------------------------------------------------------------------


class TestAdapterKindValidation:
    """adapter_kind must be 'real' or 'fake'."""

    def test_invalid_adapter_kind_raises(self) -> None:
        from medre.config.errors import ConfigValidationError
        with pytest.raises(ConfigValidationError, match="adapter_kind"):
            MatrixRuntimeConfig.from_toml_dict("test", {
                "enabled": True,
                "adapter_kind": "invalid",
                "homeserver": "https://matrix.test",
                "user_id": "@bot:test",
                "access_token": "tok",
            })

    def test_default_adapter_kind_is_real(self) -> None:
        rt = MatrixRuntimeConfig(
            adapter_id="test",
            enabled=True,
            config=_make_fake_matrix_config(),
        )
        assert rt.adapter_kind == "real"

    def test_fake_adapter_kind_accepted(self) -> None:
        rt = MatrixRuntimeConfig.from_toml_dict("test", {
            "enabled": True,
            "adapter_kind": "fake",
            "homeserver": "https://matrix.test",
            "user_id": "@bot:test",
            "access_token": "tok",
        })
        assert rt.adapter_kind == "fake"


# ---------------------------------------------------------------------------
# Matrix store_path derivation (Blocker 1)
# ---------------------------------------------------------------------------


class TestMatrixStorePathDerivation:
    """Builder injects per-adapter store_path from resolved state dir."""

    def _make_matrix_rt(
        self,
        adapter_id: str = "main",
        store_path: str | None = None,
    ) -> MatrixRuntimeConfig:
        cfg = MatrixConfig(
            adapter_id=adapter_id,
            homeserver="https://matrix.test",
            user_id="@bot:test",
            access_token="tok",
            store_path=store_path,
            encryption_mode="plaintext",
        )
        return MatrixRuntimeConfig(
            adapter_id=adapter_id,
            enabled=True,
            config=cfg,
        )

    def test_store_path_derived_when_unset(self, tmp_paths: MedrePaths) -> None:
        """MatrixConfig without store_path gets derived state store path."""
        rt = self._make_matrix_rt(adapter_id="mybot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"mybot": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "mybot")

        # The adapter was constructed with a derived store_path.
        expected = tmp_paths.state_dir / "adapters" / "mybot" / "matrix" / "store"
        assert injected_store_path == str(expected)

    def test_store_path_derived_medre_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEDRE_HOME produces $MEDRE_HOME/state/adapters/<adapter_id>/matrix/store."""
        medre_home = tmp_path / "medre-root"
        monkeypatch.setenv("MEDRE_HOME", str(medre_home))
        paths = resolve()

        rt = self._make_matrix_rt(adapter_id="e2ee-bot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"e2ee-bot": rt}),
        )
        builder = RuntimeBuilder(config, paths)

        expected_store = medre_home / "state" / "adapters" / "e2ee-bot" / "matrix" / "store"
        injected_store_path = self._capture_store_path(builder, rt, "e2ee-bot")
        assert injected_store_path == str(expected_store)

    def test_store_path_derived_xdg_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """XDG state produces $XDG_STATE_HOME/medre/adapters/<adapter_id>/matrix/store."""
        xdg_state = tmp_path / "xdg-state"
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        paths = resolve()

        rt = self._make_matrix_rt(adapter_id="xdg-bot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"xdg-bot": rt}),
        )
        builder = RuntimeBuilder(config, paths)

        expected_store = xdg_state / "medre" / "adapters" / "xdg-bot" / "matrix" / "store"
        injected_store_path = self._capture_store_path(builder, rt, "xdg-bot")
        assert injected_store_path == str(expected_store)

    def test_explicit_store_path_preserved(self, tmp_paths: MedrePaths) -> None:
        """Explicit store_path on MatrixConfig is not overridden."""
        explicit = "/custom/store/path"
        rt = self._make_matrix_rt(adapter_id="explicit", store_path=explicit)
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"explicit": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "explicit")
        assert injected_store_path == explicit

    def test_multiple_matrix_adapters_distinct_paths(self, tmp_paths: MedrePaths) -> None:
        """Multiple Matrix adapters get distinct store paths."""
        rt1 = self._make_matrix_rt(adapter_id="bot_a")
        rt2 = self._make_matrix_rt(adapter_id="bot_b")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(
                matrix={"a": rt1, "b": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        path_a = self._capture_store_path(builder, rt1, "bot_a")
        path_b = self._capture_store_path(builder, rt2, "bot_b")
        assert path_a != path_b
        assert path_a is not None and "bot_a" in path_a
        assert path_b is not None and "bot_b" in path_b

    def test_no_tempdir_in_derived_path(self, tmp_paths: MedrePaths) -> None:
        """Derived store path does not use the old tempdir pattern."""
        rt = self._make_matrix_rt(adapter_id="notmp")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"notmp": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "notmp")

        assert injected_store_path is not None
        # The old pattern was {tempdir}/medre-matrix-store/{adapter_id}
        # The new pattern is {state_dir}/adapters/{adapter_id}/matrix/store
        assert "medre-matrix-store" not in injected_store_path
        assert injected_store_path.endswith("adapters/notmp/matrix/store")

    def _capture_store_path(
        self,
        builder: RuntimeBuilder,
        rt: MatrixRuntimeConfig,
        adapter_id: str,
    ) -> str | None:
        """Build a single adapter via _build_single_adapter and capture the
        store_path that was injected into the config before it reached the
        factory."""
        import medre.runtime.builder as builder_mod
        from medre.runtime.builder import _AdapterFactory

        captured: list[str | None] = []
        original_factory = builder_mod._ADAPTER_BUILDERS.get("matrix")

        def _capture_factory_build(cfg: Any) -> BaseAdapter:
            captured.append(getattr(cfg, "store_path", None))
            return MagicMock(spec=BaseAdapter)

        capture_factory = MagicMock(spec=_AdapterFactory)
        capture_factory.build = MagicMock(side_effect=_capture_factory_build)
        builder_mod._ADAPTER_BUILDERS["matrix"] = capture_factory
        try:
            builder._build_single_adapter("matrix", adapter_id, rt)
        finally:
            if original_factory is not None:
                builder_mod._ADAPTER_BUILDERS["matrix"] = original_factory

        return captured[0] if captured else None


class TestEnsureDirsMatrixStore:
    """Runtime start creates the derived Matrix store directory."""

    def test_ensure_dirs_creates_matrix_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        rt = MatrixRuntimeConfig(
            adapter_id="dirtest",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(matrix={"dirtest": rt}),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        expected = paths.state_dir / "adapters" / "dirtest" / "matrix" / "store"
        assert expected.is_dir()


class TestEnsureDirsBaseDirectories:
    """_ensure_dirs creates state, data, cache, log, and database parent dirs."""

    def test_creates_all_base_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        assert paths.state_dir.is_dir()
        assert paths.data_dir.is_dir()
        assert paths.cache_dir.is_dir()
        assert paths.log_dir.is_dir()
        assert paths.database_path.parent.is_dir()

    def test_idempotent_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()
        app._ensure_dirs()  # second call must not raise

        assert paths.state_dir.is_dir()


class TestEnsureDirsMultiAdapterIsolation:
    """Multiple adapters get isolated state roots; no directory collision."""

    def test_two_adapters_isolated_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        rt_a = MatrixRuntimeConfig(
            adapter_id="alpha",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        rt_b = MeshtasticRuntimeConfig(
            adapter_id="beta",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"alpha": rt_a},
                meshtastic={"beta": rt_b},
            ),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        root_a = paths.adapter_state_dir("alpha")
        root_b = paths.adapter_state_dir("beta")

        assert root_a.is_dir()
        assert root_b.is_dir()
        assert root_a != root_b
        # Verify no overlap: neither is a parent of the other
        assert not str(root_a).startswith(str(root_b))
        assert not str(root_b).startswith(str(root_a))


# ---------------------------------------------------------------------------
# Degraded route validation (adapter build failures)
# ---------------------------------------------------------------------------


def _rc(
    route_id: str,
    source_adapters: tuple[str, ...],
    dest_adapters: tuple[str, ...],
    *,
    enabled: bool = True,
) -> RouteConfig:
    """Helper to create a RouteConfig with minimal boilerplate."""
    return RouteConfig(
        route_id=route_id,
        source_adapters=source_adapters,
        dest_adapters=dest_adapters,
        enabled=enabled,
    )


class TestDegradedRouteValidation:
    """Routes referencing adapters that failed to build are degraded,
    not fatal, as long as at least one adapter remains usable.

    Unknown/typo adapter IDs still raise RouteValidationError.
    """

    def test_route_with_all_working_adapters_survives(self) -> None:
        """Route referencing only successfully built adapters is registered."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(
            router, rcs,
            configured_adapter_ids := frozenset({"a", "b"}),
            built_adapter_ids := frozenset({"a", "b"}),
        )
        assert len(result.registered_routes) == 1
        assert result.registered_routes[0].id == "r1"

    def test_route_with_failed_source_adapter_skipped(self) -> None:
        """Route whose source adapter failed to build is entirely skipped."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        # "a" is configured but failed to build; "b" built OK
        result = register_routes(
            router, rcs,
            frozenset({"a", "b"}),
            frozenset({"b"}),
        )
        assert result.registered_routes == ()

    def test_route_with_failed_dest_adapter_degraded(self) -> None:
        """Route with a failed dest adapter gets that target removed."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b", "c")),
        ))
        router = Router()
        # "a" and "b" built OK; "c" failed to build
        result = register_routes(
            router, rcs,
            frozenset({"a", "b", "c"}),
            frozenset({"a", "b"}),
        )
        assert len(result.registered_routes) == 1
        route = result.registered_routes[0]
        assert route.source.adapter == "a"
        assert [t.adapter for t in route.targets] == ["b"]

    def test_route_all_dests_failed_skipped(self) -> None:
        """Route with all dest adapters failed is skipped entirely."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        # "a" built OK; "b" failed
        result = register_routes(
            router, rcs,
            frozenset({"a", "b"}),
            frozenset({"a"}),
        )
        assert result.registered_routes == ()

    def test_mixed_routes_partial_degradation(self) -> None:
        """Multiple routes: some survive, some degraded, some skipped."""
        rcs = RouteConfigSet(routes=(
            _rc("good_route", ("a",), ("b",)),       # both OK → survives
            _rc("degraded_route", ("a",), ("b", "c")),  # c failed → degraded
            _rc("dead_route", ("c",), ("b",)),        # c source failed → skipped
        ))
        router = Router()
        result = register_routes(
            router, rcs,
            frozenset({"a", "b", "c"}),
            frozenset({"a", "b"}),
        )
        ids = [r.id for r in result.registered_routes]
        assert "good_route" in ids
        assert "degraded_route" in ids
        assert "dead_route" not in ids
        # Verify degraded_route has only "b" as target
        degraded = next(r for r in result.registered_routes if r.id == "degraded_route")
        assert [t.adapter for t in degraded.targets] == ["b"]

    def test_unknown_adapter_still_raises(self) -> None:
        """Route referencing a truly unknown adapter ID still raises."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("typo_id",)),
        ))
        router = Router()
        with pytest.raises(RouteValidationError, match="typo_id"):
            register_routes(
                router, rcs,
                frozenset({"a"}),  # "typo_id" not configured at all
                frozenset({"a"}),
            )

    def test_no_built_adapter_ids_falls_back(self) -> None:
        """Calling register_routes without built_adapter_ids uses adapter_ids for both."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert len(result.registered_routes) == 1

    def test_unknown_adapter_ids_raise_without_built_ids(self) -> None:
        """Without built_adapter_ids, unknown adapter IDs still raise."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("unknown",)),
        ))
        router = Router()
        with pytest.raises(RouteValidationError):
            register_routes(router, rcs, frozenset({"a"}))

    def test_full_build_one_good_one_failed_adapter(
        self, tmp_paths: MedrePaths
    ) -> None:
        """RuntimeBuilder.build() with one fake adapter succeeding and one
        failing produces a degraded runtime with build_failures recorded
        and routes involving only the working adapter surviving."""
        # Adapter "fm" (fake matrix) will build fine.
        # Adapter "ft" (fake meshtastic) will be made to fail.
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        # Route from fm → ft (involves failed adapter as dest)
        route_fm_to_ft = RouteConfig(
            route_id="fm_to_ft",
            source_adapters=("fm",),
            dest_adapters=("ft",),
        )
        # Route from ft → fm (involves failed adapter as source)
        route_ft_to_fm = RouteConfig(
            route_id="ft_to_fm",
            source_adapters=("ft",),
            dest_adapters=("fm",),
        )
        routes = RouteConfigSet(routes=(route_fm_to_ft, route_ft_to_fm))

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=routes,
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Patch _build_single_adapter to make ft fail
        original_build = builder._build_single_adapter
        call_count = 0

        def _selective_build(transport: str, adapter_id: str, rtc: Any) -> BaseAdapter:
            nonlocal call_count
            call_count += 1
            if adapter_id == "ft":
                raise RuntimeConfigError("simulated build failure for ft")
            return original_build(transport, adapter_id, rtc)

        with patch.object(builder, "_build_single_adapter", side_effect=_selective_build):
            app = builder.build()

        # fm built successfully
        assert "fm" in app.adapters
        # ft did NOT build
        assert "ft" not in app.adapters
        # Build failures recorded
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "ft"

        # Routes: fm_to_ft has no surviving targets (ft failed) → skipped
        # Routes: ft_to_fm has failed source → skipped
        # So no routes registered
        assert len(app.router._routes) == 0

    def test_full_build_one_good_one_failed_unrelated_route_survives(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter builds, one fails; a route using only the good
        adapter as both source and dest survives, while routes involving
        the failed adapter are degraded."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        # Unrelated route: fm → fm (self-route not allowed by config, so use two matrix instances)
        # Actually self-route check prevents this. Let's use a third adapter.
        rt_core = MeshCoreRuntimeConfig(
            adapter_id="fc",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshcore_config(),
        )
        # Route between two working adapters
        route_good = RouteConfig(
            route_id="good_route",
            source_adapters=("fm",),
            dest_adapters=("fc",),
        )
        # Route involving failed adapter
        route_degraded = RouteConfig(
            route_id="degraded_route",
            source_adapters=("fm",),
            dest_adapters=("ft",),  # ft will fail
        )
        routes = RouteConfigSet(routes=(route_good, route_degraded))

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
                meshcore={"fc": rt_core},
            ),
            routes=routes,
        )
        builder = RuntimeBuilder(config, tmp_paths)

        original_build = builder._build_single_adapter

        def _selective_build(transport: str, adapter_id: str, rtc: Any) -> BaseAdapter:
            if adapter_id == "ft":
                raise RuntimeConfigError("simulated build failure for ft")
            return original_build(transport, adapter_id, rtc)

        with patch.object(builder, "_build_single_adapter", side_effect=_selective_build):
            app = builder.build()

        # Good adapters built
        assert "fm" in app.adapters
        assert "fc" in app.adapters
        assert "ft" not in app.adapters
        assert len(app.build_failures) == 1

        # good_route (fm→fc) should be registered
        # degraded_route (fm→ft) has no surviving targets → skipped
        route_ids = list(app.router._routes.keys())
        assert "good_route" in route_ids
        assert "degraded_route" not in route_ids


# ---------------------------------------------------------------------------
# Fake adapter ID propagation
# ---------------------------------------------------------------------------


class TestFakeAdapterIdPropagation:
    """Fake adapters receive and report the configured adapter_id."""

    def test_fake_matrix_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MatrixRuntimeConfig(
            adapter_id="custom_matrix_id",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"cm": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "custom_matrix_id" in adapters
        assert adapters["custom_matrix_id"].adapter_id == "custom_matrix_id"

    def test_fake_meshtastic_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MeshtasticRuntimeConfig(
            adapter_id="custom_mesh_id",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshtastic={"cm": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "custom_mesh_id" in adapters
        assert adapters["custom_mesh_id"].adapter_id == "custom_mesh_id"

    def test_fake_meshcore_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MeshCoreRuntimeConfig(
            adapter_id="custom_core_id",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshcore_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshcore={"cc": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "custom_core_id" in adapters
        assert adapters["custom_core_id"].adapter_id == "custom_core_id"

    def test_fake_lxmf_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = LxmfRuntimeConfig(
            adapter_id="custom_lxmf_id",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_lxmf_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(lxmf={"cl": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters)
        assert "custom_lxmf_id" in adapters
        assert adapters["custom_lxmf_id"].adapter_id == "custom_lxmf_id"

    def test_all_four_fakes_report_configured_ids(self, tmp_paths: MedrePaths) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="id-prop-test"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": MatrixRuntimeConfig(
                    adapter_id="fm_id", enabled=True, adapter_kind="fake",
                    config=_make_fake_matrix_config(),
                )},
                meshtastic={"ft": MeshtasticRuntimeConfig(
                    adapter_id="ft_id", enabled=True, adapter_kind="fake",
                    config=_make_fake_meshtastic_config(),
                )},
                meshcore={"fc": MeshCoreRuntimeConfig(
                    adapter_id="fc_id", enabled=True, adapter_kind="fake",
                    config=_make_fake_meshcore_config(),
                )},
                lxmf={"fl": LxmfRuntimeConfig(
                    adapter_id="fl_id", enabled=True, adapter_kind="fake",
                    config=_make_fake_lxmf_config(),
                )},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert len(app.adapters) == 4
        assert app.adapters["fm_id"].adapter_id == "fm_id"
        assert app.adapters["ft_id"].adapter_id == "ft_id"
        assert app.adapters["fc_id"].adapter_id == "fc_id"
        assert app.adapters["fl_id"].adapter_id == "fl_id"


# ---------------------------------------------------------------------------
# Deterministic build ordering
# ---------------------------------------------------------------------------


class TestDeterministicBuildOrdering:
    """Adapters are built in (transport, adapter_id) sorted order."""

    def test_build_adapters_sorted_by_transport_then_id(
        self, tmp_paths: MedrePaths
    ) -> None:
        """_build_adapters processes adapters in deterministic (transport, adapter_id) order."""
        # Configure adapters whose (transport, adapter_id) sort order differs
        # from a plain adapter_id sort.
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="z_matrix",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="a_mesh",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        rt_core = MeshCoreRuntimeConfig(
            adapter_id="m_core",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"zm": rt_matrix},
                meshtastic={"am": rt_mesh},
                meshcore={"mc": rt_core},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Track the order adapters are built by patching _build_single_adapter.
        build_order: list[str] = []
        original_build = builder._build_single_adapter

        def _tracking_build(transport: str, adapter_id: str, rtc: Any) -> BaseAdapter:
            build_order.append(f"{transport}.{adapter_id}")
            return original_build(transport, adapter_id, rtc)

        with patch.object(builder, "_build_single_adapter", side_effect=_tracking_build):
            adapters: dict[str, BaseAdapter] = {}
            builder._build_adapters(adapters)

        # Expected order: sorted by (transport, adapter_id):
        # ("lxmf", ...) — none configured
        # ("matrix", "z_matrix") — matrix < meshcore < meshtastic alphabetically
        # ("meshcore", "m_core")
        # ("meshtastic", "a_mesh")
        assert build_order == [
            "matrix.z_matrix",
            "meshcore.m_core",
            "meshtastic.a_mesh",
        ]

    def test_build_order_reproducible_across_builds(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Two builds with identical config produce identical adapter dict key order."""
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"m1": MatrixRuntimeConfig(
                    adapter_id="m1", enabled=True, adapter_kind="fake", config=None,
                )},
                meshtastic={"t1": MeshtasticRuntimeConfig(
                    adapter_id="t1", enabled=True, adapter_kind="fake", config=None,
                )},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        adapters1: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters1)
        order1 = list(adapters1.keys())

        adapters2: dict[str, BaseAdapter] = {}
        builder._build_adapters(adapters2)
        order2 = list(adapters2.keys())

        assert order1 == order2


# ---------------------------------------------------------------------------
# All-adapters-build-failure — builder returns app with empty adapters
# ---------------------------------------------------------------------------


class TestAllAdaptersBuildFailure:
    """When every enabled adapter fails construction, build() still returns
    a MedreApp (with empty adapters + populated build_failures).

    The CLI layer is responsible for checking this condition and exiting
    with EXIT_BUILD.  The builder itself does NOT raise — it records failures.
    """

    def test_all_single_adapter_build_failure(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Single enabled adapter fails -> build() returns app with empty adapters."""
        rt = MatrixRuntimeConfig(
            adapter_id="broken",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(matrix={"brk": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        with patch.object(
            builder,
            "_build_single_adapter",
            side_effect=RuntimeConfigError("simulated missing SDK"),
        ):
            app = builder.build()

        assert len(app.adapters) == 0
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "broken"

    def test_all_multiple_adapters_build_failure(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Multiple enabled adapters all fail -> empty adapters, all recorded."""
        rt1 = MatrixRuntimeConfig(
            adapter_id="broken_a",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        rt2 = MeshtasticRuntimeConfig(
            adapter_id="broken_b",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"ba": rt1},
                meshtastic={"bb": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        with patch.object(
            builder,
            "_build_single_adapter",
            side_effect=RuntimeConfigError("simulated missing SDK"),
        ):
            app = builder.build()

        assert len(app.adapters) == 0
        assert len(app.build_failures) == 2
        failed_ids = {bf.adapter_id for bf in app.build_failures}
        assert failed_ids == {"broken_a", "broken_b"}

    def test_partial_failure_not_all_empty(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter builds, one fails -> adapters non-empty, one build_failure."""
        rt1 = MatrixRuntimeConfig(
            adapter_id="good_one",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_matrix_config(),
        )
        rt2 = MeshtasticRuntimeConfig(
            adapter_id="bad_one",
            enabled=True,
            adapter_kind="fake",
            config=_make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"g": rt1},
                meshtastic={"b": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        original_build = builder._build_single_adapter

        def _selective_fail(transport: str, adapter_id: str, rtc: Any) -> BaseAdapter:
            if adapter_id == "bad_one":
                raise RuntimeConfigError("simulated failure")
            return original_build(transport, adapter_id, rtc)

        with patch.object(builder, "_build_single_adapter", side_effect=_selective_fail):
            app = builder.build()

        assert len(app.adapters) == 1
        assert "good_one" in app.adapters
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "bad_one"
