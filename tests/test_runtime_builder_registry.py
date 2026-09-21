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
