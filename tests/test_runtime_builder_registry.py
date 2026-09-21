"""Registry-specific runtime builder contract tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.config.adapters.matrix import MatrixConfig
from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.rendering.renderer import RenderingPipeline
from medre.runtime import builder as builder_mod
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def test_renderer_factory_import_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered MEDRE renderer failures cannot silently fall back to text."""

    class BrokenRef:
        def load(self) -> object:
            raise ImportError("renderer implementation is broken")

    spec = SimpleNamespace(transport="matrix", renderer_factory=BrokenRef())
    monkeypatch.setattr(builder_mod, "iter_adapter_specs", lambda: (spec,))

    config = RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(),
    )
    with pytest.raises(ImportError, match="renderer implementation is broken"):
        builder_mod._register_adapter_renderers(RenderingPipeline(), config)


def test_runtime_preparation_failure_is_fail_closed_for_fake_adapter(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adapter config preflight failures abort build even for fake instances."""
    from medre.adapters.matrix import runtime as matrix_runtime

    def _fail_prepare(*args: object, **kwargs: object) -> object:
        raise ValueError("invalid prepared state")

    monkeypatch.setattr(matrix_runtime, "prepare_matrix_runtime_config", _fail_prepare)
    config = RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "matrix-key": MatrixRuntimeConfig(
                    adapter_id="matrix-runtime",
                    enabled=True,
                    adapter_kind="fake",
                    config=MatrixConfig(
                        adapter_id="matrix-runtime",
                        homeserver="https://matrix.test",
                        user_id="@bot:test",
                        access_token="test-token",
                    ),
                )
            }
        ),
    )

    with pytest.raises(
        RuntimeConfigError, match="Failed to prepare adapter.*matrix-runtime"
    ):
        RuntimeBuilder(config, tmp_paths).build()


def test_dependency_probe_import_failure_is_not_treated_as_sdk_absence() -> None:
    """Broken registered probe modules fail visibly instead of returning false."""

    class BrokenProbeRef:
        def load(self) -> object:
            raise ImportError("broken compatibility module")

    spec = SimpleNamespace(dependency_probe=BrokenProbeRef())
    with pytest.raises(ImportError, match="broken compatibility module"):
        builder_mod._dependency_available(spec)


# ---------------------------------------------------------------------------
# Renderer registration and dependency probing seams
# ---------------------------------------------------------------------------


def test_build_source_attribution_without_config_is_empty() -> None:
    assert builder_mod._build_source_attribution(None) == {}


def test_register_adapter_renderers_without_config_registers_nothing() -> None:
    pipeline = RenderingPipeline()
    assert builder_mod._register_adapter_renderers(pipeline, config=None) == {}
    assert pipeline._renderers == []


def test_register_adapter_renderers_skips_specs_without_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_paths: MedrePaths
) -> None:
    """A registered adapter with no renderer contributes no pipeline entry."""
    spec = SimpleNamespace(transport="matrix", renderer_factory=None)
    monkeypatch.setattr(builder_mod, "iter_adapter_specs", lambda: (spec,))
    config = RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="matrix-main",
                    enabled=True,
                    adapter_kind="fake",
                    config=MatrixConfig(
                        adapter_id="matrix-main",
                        homeserver="https://matrix.test",
                        user_id="@bot:test",
                    ),
                )
            }
        ),
    )
    pipeline = RenderingPipeline()
    attribution = builder_mod._register_adapter_renderers(pipeline, config=config)
    assert pipeline._renderers == []
    assert attribution["matrix-main"].platform == "matrix"


def test_dependency_available_without_probe_is_true() -> None:
    assert builder_mod._dependency_available(SimpleNamespace(dependency_probe=None))


def test_build_fake_adapter_failure_is_wrapped() -> None:
    class _Boom:
        def __init__(self, adapter_id: str) -> None:
            raise TypeError("bad fake")

    spec = SimpleNamespace(
        transport="sample",
        fake_adapter=SimpleNamespace(load=lambda: _Boom),
    )
    with pytest.raises(RuntimeConfigError, match="Could not construct fake adapter"):
        builder_mod._build_fake_adapter(spec, "sample-a")


def test_build_real_adapter_constructs_when_dependency_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[object] = []

    class _StubAdapter:
        def __init__(self, config: object) -> None:
            constructed.append(config)

    spec = SimpleNamespace(
        transport="sample", adapter=SimpleNamespace(load=lambda: _StubAdapter)
    )
    monkeypatch.setattr(builder_mod, "_dependency_available", lambda spec: True)
    adapter = builder_mod._build_real_adapter(spec, {"adapter_id": "x"})
    assert isinstance(adapter, _StubAdapter)
    assert constructed == [{"adapter_id": "x"}]


def test_sender_projection_tolerates_non_dict_native_data() -> None:
    fn = builder_mod._build_project_sender_metadata_fn({})
    event = SimpleNamespace(
        source_adapter="unknown-adapter", source_transport_id=None, metadata=None
    )
    assert fn(event) == {"source_platform": None}


# ---------------------------------------------------------------------------
# Fail-closed builder preflight paths
# ---------------------------------------------------------------------------


def test_invalid_storage_path_placeholder_fails_build(tmp_paths: MedrePaths) -> None:
    config = RuntimeConfig(
        storage=StorageConfig(backend="sqlite", path="{bogus}/medre.sqlite"),
        adapters=AdapterConfigSet(),
    )
    with pytest.raises(RuntimeConfigError, match="Invalid storage path"):
        RuntimeBuilder(config, tmp_paths).build()


def test_unknown_transport_during_preflight_fails_build(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport missing from the registry cannot reach adapter assembly."""
    monkeypatch.setattr(builder_mod, "get_adapter_spec", lambda transport: None)
    config = RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="matrix-main",
                    enabled=True,
                    adapter_kind="fake",
                    config=MatrixConfig(
                        adapter_id="matrix-main",
                        homeserver="https://matrix.test",
                        user_id="@bot:test",
                    ),
                )
            }
        ),
    )
    with pytest.raises(RuntimeConfigError, match="Unknown transport type"):
        RuntimeBuilder(config, tmp_paths).build()


# ---------------------------------------------------------------------------
# Adapter-owned Matrix runtime hooks
# ---------------------------------------------------------------------------


def test_matrix_preparation_skips_disabled_routes() -> None:
    """Rooms from disabled routes are neither merged nor allowlist-checked."""
    from types import SimpleNamespace as _NS

    from medre.adapters.matrix.runtime import prepare_matrix_runtime_config

    disabled = _NS(
        enabled=False,
        source=_NS(adapter="matrix-main", channel="!secret:test"),
        targets=[_NS(adapter="matrix-main", channel="!target:test")],
    )
    enabled = _NS(
        enabled=True,
        source=_NS(adapter="other", channel="!elsewhere:test"),
        targets=[_NS(adapter="matrix-main", channel="!target:test")],
    )
    config = MatrixConfig(
        adapter_id="matrix-main",
        homeserver="https://matrix.test",
        user_id="@bot:test",
        room_allowlist={"!other:test"},
    )
    paths = _NS(
        adapter_transport_state_dir=lambda *args, **kwargs: Path("/tmp") / "state"
    )
    prepared = prepare_matrix_runtime_config(
        config, adapter_id="matrix-main", paths=paths, routes=[disabled, enabled]
    )
    assert prepared.auto_join_rooms == ("!target:test",)


def test_matrix_runtime_directories_prefers_configured_store_path(
    tmp_path: Path,
) -> None:
    from medre.adapters.matrix.runtime import matrix_runtime_directories

    config = MatrixConfig(
        adapter_id="matrix-main",
        homeserver="https://matrix.test",
        user_id="@bot:test",
        store_path=str(tmp_path / "custom-store"),
    )
    (directory,) = matrix_runtime_directories(
        config, adapter_id="matrix-main", paths=object()
    )
    assert directory == tmp_path / "custom-store"
