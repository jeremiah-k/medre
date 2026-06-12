"""Tests for medre.runtime.builder: multi-adapter construction,
disabled adapters, error handling."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.model import (
    AdapterConfigSet,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContract
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from tests.helpers.runtime_builder import (
    clean_path_env,
    make_all_enabled_config,
    make_disabled_config,
    make_empty_config,
    make_fake_lxmf_config,
    make_fake_matrix_config,
    make_fake_meshcore_config,
    make_fake_meshtastic_config,
)

if TYPE_CHECKING:
    from medre.adapters.lxmf.renderer import LxmfRenderer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_path_env(monkeypatch)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


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
        config = make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        assert builder is not None

    def test_builder_stores_config(self, tmp_paths: MedrePaths) -> None:
        config = make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        assert builder._config is config
        assert builder._paths is tmp_paths


# ---------------------------------------------------------------------------
# Build with mocked adapters
# ---------------------------------------------------------------------------


class TestBuildWithMockedAdapters:
    """Test builder.build() with mocked adapter factories."""

    def test_build_returns_medre_app(self, tmp_paths: MedrePaths) -> None:
        config = make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert isinstance(app, MedreApp)

    def test_build_with_mocked_matrix_adapter(self, tmp_paths: MedrePaths) -> None:
        """When Matrix adapter factory succeeds, adapter appears in app."""
        config = make_all_enabled_config()
        builder = RuntimeBuilder(config, tmp_paths)

        mock_adapter = MagicMock(spec=AdapterContract)
        with patch.object(builder, "_build_single_adapter", return_value=mock_adapter):
            app = builder.build()

        # The app should have adapters
        assert isinstance(app, MedreApp)
        assert len(app.adapters) > 0

    def test_build_creates_subsystems(self, tmp_paths: MedrePaths) -> None:
        """Builder creates all expected subsystem references."""
        config = make_empty_config()
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
        config = make_disabled_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        # Disabled adapters should not be in the adapters dict
        assert "matrix_off" not in app.adapters
        assert "mesh_off" not in app.adapters

    def test_empty_adapters_config_builds(self, tmp_paths: MedrePaths) -> None:
        config = make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        assert app.adapters == {}


# ---------------------------------------------------------------------------
# Adapter contexts and IDs
# ---------------------------------------------------------------------------


class TestAdapterIds:
    """Adapter configs carry the correct adapter_id."""

    def test_matrix_adapter_id(self) -> None:
        cfg = make_fake_matrix_config()
        rt = MatrixRuntimeConfig(adapter_id="custom_id", enabled=True, config=cfg)
        assert rt.adapter_id == "custom_id"

    def test_meshtastic_adapter_id(self) -> None:
        cfg = make_fake_meshtastic_config()
        rt = MeshtasticRuntimeConfig(adapter_id="my_radio", enabled=True, config=cfg)
        assert rt.adapter_id == "my_radio"

    def test_meshcore_adapter_id(self) -> None:
        cfg = make_fake_meshcore_config()
        rt = MeshCoreRuntimeConfig(adapter_id="my_node", enabled=True, config=cfg)
        assert rt.adapter_id == "my_node"

    def test_lxmf_adapter_id(self) -> None:
        cfg = make_fake_lxmf_config()
        rt = LxmfRuntimeConfig(adapter_id="my_lxmf", enabled=True, config=cfg)
        assert rt.adapter_id == "my_lxmf"


# ---------------------------------------------------------------------------
# all_enabled / all_configs
# ---------------------------------------------------------------------------


class TestAdapterConfigSet:
    """AdapterConfigSet helper methods return correct results."""

    def test_all_enabled_returns_only_enabled(self) -> None:
        matrix_on = MatrixRuntimeConfig(
            adapter_id="on",
            enabled=True,
            config=make_fake_matrix_config(),
        )
        matrix_off = MatrixRuntimeConfig(
            adapter_id="off",
            enabled=False,
            config=make_fake_matrix_config(),
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
            adapter_id="on",
            enabled=True,
            config=make_fake_matrix_config(),
        )
        matrix_off = MatrixRuntimeConfig(
            adapter_id="off",
            enabled=False,
            config=make_fake_matrix_config(),
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
        config = make_empty_config()
        builder = RuntimeBuilder(config, tmp_paths)

        with pytest.raises(RuntimeConfigError, match="Unknown transport type"):
            builder._build_single_adapter("unknown_transport", "test_id", MagicMock())

    def test_real_adapter_construction_error_wrapped(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Real adapter construction exception is wrapped in RuntimeConfigError."""
        cfg = make_fake_matrix_config()
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
            config=make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"fake_matrix": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "fake_matrix" in adapters
        assert adapters["fake_matrix"].platform == "matrix"

    def test_fake_meshtastic_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake Meshtastic adapter builds without meshtastic SDK."""
        rt = MeshtasticRuntimeConfig(
            adapter_id="fake_mesh",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshtastic={"fake_mesh": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "fake_mesh" in adapters
        assert adapters["fake_mesh"].platform == "meshtastic"

    def test_fake_meshcore_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake MeshCore adapter builds without meshcore SDK."""
        rt = MeshCoreRuntimeConfig(
            adapter_id="fake_core",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshcore_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshcore={"fake_core": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "fake_core" in adapters
        assert adapters["fake_core"].platform == "meshcore"

    def test_fake_lxmf_builds(self, tmp_paths: MedrePaths) -> None:
        """Fake LXMF adapter builds without Reticulum."""
        rt = LxmfRuntimeConfig(
            adapter_id="fake_lxmf",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_lxmf_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(lxmf={"fake_lxmf": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "fake_lxmf" in adapters
        assert adapters["fake_lxmf"].platform == "lxmf"

    def test_fake_unknown_transport_raises(self, tmp_paths: MedrePaths) -> None:
        """Fake adapter for unknown transport raises RuntimeConfigError."""
        rt = MatrixRuntimeConfig(
            adapter_id="bad",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
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
                matrix={
                    "fm": MatrixRuntimeConfig(
                        adapter_id="fm",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_matrix_config(),
                    )
                },
                meshtastic={
                    "ft": MeshtasticRuntimeConfig(
                        adapter_id="ft",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_meshtastic_config(),
                    )
                },
                meshcore={
                    "fc": MeshCoreRuntimeConfig(
                        adapter_id="fc",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_meshcore_config(),
                    )
                },
                lxmf={
                    "fl": LxmfRuntimeConfig(
                        adapter_id="fl",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_lxmf_config(),
                    )
                },
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
            MatrixRuntimeConfig.from_toml_dict(
                "test",
                {
                    "enabled": True,
                    "adapter_kind": "invalid",
                    "homeserver": "https://matrix.test",
                    "user_id": "@bot:test",
                    "access_token": "tok",
                },
            )

    def test_default_adapter_kind_is_real(self) -> None:
        rt = MatrixRuntimeConfig(
            adapter_id="test",
            enabled=True,
            config=make_fake_matrix_config(),
        )
        assert rt.adapter_kind == "real"

    def test_fake_adapter_kind_accepted(self) -> None:
        rt = MatrixRuntimeConfig.from_toml_dict(
            "test",
            {
                "enabled": True,
                "adapter_kind": "fake",
                "homeserver": "https://matrix.test",
                "user_id": "@bot:test",
                "access_token": "tok",
            },
        )
        assert rt.adapter_kind == "fake"


# ---------------------------------------------------------------------------
# LXMF relay prefix wiring through runtime builder
# ---------------------------------------------------------------------------


class TestLxmfRelayPrefixWiring:
    """Builder wires configured lxmf_relay_prefix into LxmfRenderer.

    Multi-LXMF rule: first non-empty ``lxmf_relay_prefix`` across LXMF
    runtime configs in deterministic (adapter_id sorted) order; empty
    if none.
    """

    def _find_lxmf_renderer(self, app: MedreApp) -> LxmfRenderer | None:
        """Return the LxmfRenderer registered in the rendering pipeline."""
        from medre.adapters.lxmf.renderer import LxmfRenderer as _LxmfRenderer

        for _pri, _seq, renderer in app.rendering_pipeline._renderers:
            if getattr(renderer, "name", None) == "lxmf":
                assert isinstance(renderer, _LxmfRenderer)
                return renderer
        return None

    def test_prefix_wired_into_lxmf_renderer(self, tmp_paths: MedrePaths) -> None:
        """Configured lxmf_relay_prefix appears on the registered renderer."""
        lxmf_cfg = LxmfConfig(
            adapter_id="lxmf_a",
            connection_type="fake",
            lxmf_relay_prefix="[{source_display_name}] ",
        ).validate()
        rt = LxmfRuntimeConfig(
            adapter_id="lxmf_a",
            enabled=True,
            adapter_kind="fake",
            config=lxmf_cfg,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(lxmf={"lxmf_a": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        renderer = self._find_lxmf_renderer(app)
        assert renderer is not None, "LxmfRenderer not registered in pipeline"
        assert renderer._relay_prefix == "[{source_display_name}] "

    def test_empty_prefix_default_when_not_configured(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Default empty prefix when lxmf_relay_prefix is not set."""
        lxmf_cfg = LxmfConfig(
            adapter_id="lxmf_b",
            connection_type="fake",
        ).validate()
        rt = LxmfRuntimeConfig(
            adapter_id="lxmf_b",
            enabled=True,
            adapter_kind="fake",
            config=lxmf_cfg,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(lxmf={"lxmf_b": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        renderer = self._find_lxmf_renderer(app)
        assert renderer is not None
        assert renderer._relay_prefix == ""

    def test_multi_lxmf_first_non_empty_prefix_wins(
        self, tmp_paths: MedrePaths
    ) -> None:
        """With multiple LXMF adapters, first non-empty prefix wins."""
        cfg_a = LxmfConfig(
            adapter_id="lxmf_alpha",
            connection_type="fake",
            lxmf_relay_prefix="",
        ).validate()
        cfg_b = LxmfConfig(
            adapter_id="lxmf_beta",
            connection_type="fake",
            lxmf_relay_prefix="<{shortname}> ",
        ).validate()
        rt_a = LxmfRuntimeConfig(
            adapter_id="lxmf_alpha",
            enabled=True,
            adapter_kind="fake",
            config=cfg_a,
        )
        rt_b = LxmfRuntimeConfig(
            adapter_id="lxmf_beta",
            enabled=True,
            adapter_kind="fake",
            config=cfg_b,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                lxmf={"alpha": rt_a, "beta": rt_b},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        renderer = self._find_lxmf_renderer(app)
        assert renderer is not None
        # lxmf_alpha sorts before lxmf_beta, but alpha prefix is empty,
        # so beta's prefix is used.
        assert renderer._relay_prefix == "<{shortname}> "
