"""Loader handling of registered adapters without a dedicated wrapper class.

Built-ins that ship a ``runtime_config`` reference keep their compatibility
wrapper (e.g. :class:`MatrixRuntimeConfig`). A registered adapter without one
must fall back to the transport-neutral
:class:`~medre.config.model.GenericAdapterRuntimeConfig` without any change to
:mod:`medre.config.model`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import medre.config.loader as loader_mod
from medre.adapter_registry import AdapterSpec, SymbolRef
from medre.config.loader import load_config
from medre.config.model import GenericAdapterRuntimeConfig, MatrixRuntimeConfig


def _wrapper_free_matrix_spec() -> AdapterSpec:
    """Matrix spec stripped of its runtime-config wrapper reference."""
    return AdapterSpec(
        transport="matrix",
        config=SymbolRef("medre.config.adapters.matrix", "MatrixConfig"),
        runtime_config=None,
        adapter=SymbolRef("medre.adapters.matrix.adapter", "MatrixAdapter"),
        fake_adapter=SymbolRef("medre.adapters.fakes.matrix", "FakeMatrixAdapter"),
        renderer_factory=None,
        dependency_probe=None,
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
        native_namespace_reader=SymbolRef(
            "medre.adapters.matrix.event_shape", "matrix_namespace"
        ),
        versioned_namespace_reader=SymbolRef(
            "medre.adapters.matrix.event_shape", "matrix_versioned_namespace"
        ),
        attribution_projector=SymbolRef(
            "medre.adapters.matrix.attribution", "project_matrix_attribution"
        ),
    )


@pytest.fixture()
def wrapper_free_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "runtime: {}\n"
        "adapters:\n"
        "  matrix:\n"
        "    main:\n"
        "      adapter_id: matrix-main\n"
        "      adapter_kind: fake\n"
        "      homeserver: https://matrix.test\n"
        "      user_id: '@bot:test'\n"
        "      access_token: test-token\n"
    )
    return p


def test_transport_without_runtime_config_uses_generic_wrapper(
    wrapper_free_config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        loader_mod, "iter_adapter_specs", lambda: (_wrapper_free_matrix_spec(),)
    )
    config, _source, _paths = load_config(str(wrapper_free_config_file))

    rtc = config.adapters.for_transport("matrix")["main"]
    assert type(rtc) is GenericAdapterRuntimeConfig
    assert not isinstance(rtc, MatrixRuntimeConfig)
    assert rtc.adapter_id == "matrix-main"
    assert rtc.adapter_kind == "fake"
    assert rtc.config is not None and rtc.config.adapter_id == "matrix-main"


def test_transport_with_runtime_config_keeps_compat_wrapper(
    wrapper_free_config_file: Path,
) -> None:
    """Unpatched registry keeps the dedicated Matrix wrapper for parity."""
    config, _source, _paths = load_config(str(wrapper_free_config_file))
    rtc = config.adapters.for_transport("matrix")["main"]
    assert type(rtc) is MatrixRuntimeConfig
