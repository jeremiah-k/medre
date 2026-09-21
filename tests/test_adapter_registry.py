"""Contract tests for the data-driven built-in adapter registry."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from medre.adapter_registry import (
    BUILTIN_ADAPTER_REGISTRY,
    AdapterSpec,
    AdapterTypeRegistry,
    SymbolRef,
    adapter_sdk_packages,
    native_detection_specs,
    registered_transports,
)
from medre.cli.transport_constants import RADIO_TRANSPORTS
from medre.cli.transports import TRANSPORTS
from medre.config.model import AdapterConfigSet
from medre.runtime.architecture_report import SESSION_ALLOWED_SDKS

_ROOT = Path(__file__).resolve().parents[1]


def _symbol(module: str = "builtins", name: str = "object") -> SymbolRef:
    return SymbolRef(module, name)


def _synthetic_spec(transport: str) -> AdapterSpec:
    return AdapterSpec(
        transport=transport,
        config=_symbol(),
        runtime_config=None,
        adapter=_symbol(),
        fake_adapter=_symbol(),
        renderer_factory=None,
        dependency_probe=None,
        distribution=f"{transport}-dist",
        import_names=(transport,),
        sdk_import_roots=(transport,),
        native_namespace_reader=_symbol(),
        versioned_namespace_reader=_symbol(),
        attribution_projector=_symbol(),
    )


def test_registry_is_ordered_unique_and_matches_config_groups() -> None:
    transports = registered_transports()
    assert transports == tuple(spec.transport for spec in BUILTIN_ADAPTER_REGISTRY)
    assert len(transports) == len(set(transports))
    assert tuple(name for name, _group in AdapterConfigSet().groups()) == transports


def test_registry_rejects_duplicate_transport() -> None:
    spec = _synthetic_spec("sample")
    with pytest.raises(ValueError, match="duplicate adapter transport"):
        AdapterTypeRegistry((spec, spec))


def test_registry_rejects_invalid_transport_name() -> None:
    spec = _synthetic_spec("Bad Transport")
    with pytest.raises(ValueError, match="invalid adapter transport name"):
        AdapterTypeRegistry((spec,))


def test_registry_rejects_negative_detection_priority() -> None:
    spec = replace(_synthetic_spec("sample"), native_detection_priority=-1)
    with pytest.raises(ValueError, match="native_detection_priority must be >= 0"):
        AdapterTypeRegistry((spec,))


def test_registry_rejects_duplicate_endpoint_support_field() -> None:
    spec = replace(_synthetic_spec("sample"), support_endpoint_fields=("host", "host"))
    with pytest.raises(ValueError, match="duplicate support endpoint field"):
        AdapterTypeRegistry((spec,))


def test_registry_rejects_duplicate_secret_support_field() -> None:
    spec = replace(_synthetic_spec("sample"), support_secret_fields=("pin", "pin"))
    with pytest.raises(ValueError, match="duplicate support secret field"):
        AdapterTypeRegistry((spec,))


def test_require_unknown_transport_names_known_transports() -> None:
    with pytest.raises(KeyError, match="unknown adapter transport 'nope'"):
        BUILTIN_ADAPTER_REGISTRY.require("nope")


def test_registered_transport_is_accepted_without_core_model_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdapterTypeRegistry((_synthetic_spec("sample"),))
    monkeypatch.setattr("medre.config.model.registered_transports", registry.transports)

    config = AdapterConfigSet(sample={})

    assert registry.transports() == ("sample",)
    assert registry.require("sample").install_extra == "sample"
    assert config.groups() == (("sample", {}),)


def test_registry_supports_sidecar_adapter_without_python_sdk() -> None:
    spec = replace(
        _synthetic_spec("sample"),
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
    )
    registry = AdapterTypeRegistry((spec,))
    assert registry.require("sample").has_python_sdk is False


def test_registry_requires_complete_cli_contribution_pair() -> None:
    spec = replace(_synthetic_spec("sample"), cli_register=_symbol())
    with pytest.raises(ValueError, match="must declare both"):
        AdapterTypeRegistry((spec,))


def test_registry_rejects_overlapping_support_fields() -> None:
    spec = replace(
        _synthetic_spec("sample"),
        support_endpoint_fields=("base_url",),
        support_secret_fields=("base_url",),
    )
    with pytest.raises(ValueError, match="cannot be both endpoint and secret"):
        AdapterTypeRegistry((spec,))


def test_matrix_cli_contribution_is_adapter_owned() -> None:
    matrix = BUILTIN_ADAPTER_REGISTRY.require("matrix")
    assert matrix.cli_register is not None
    assert matrix.cli_register.module == "medre.adapters.matrix.cli_contrib"
    assert matrix.cli_dispatch is not None
    assert matrix.cli_dispatch.module == "medre.adapters.matrix.cli_contrib"


def test_support_bundle_field_metadata_is_registry_owned() -> None:
    matrix = BUILTIN_ADAPTER_REGISTRY.require("matrix")
    meshcore = BUILTIN_ADAPTER_REGISTRY.require("meshcore")
    assert matrix.support_endpoint_fields == (
        "homeserver",
        "user_id",
        "room_allowlist",
    )
    assert matrix.support_secret_fields == ("access_token",)
    assert meshcore.support_secret_fields == ("ble_pin",)


def test_native_detection_priority_preserves_existing_precedence() -> None:
    assert tuple(spec.transport for spec in native_detection_specs()) == (
        "meshcore",
        "matrix",
        "meshtastic",
        "lxmf",
    )


def test_cli_transport_inventory_derives_from_registry() -> None:
    expected = [
        (spec.transport, spec.distribution, spec.import_names)
        for spec in BUILTIN_ADAPTER_REGISTRY
    ]
    assert TRANSPORTS == expected
    assert RADIO_TRANSPORTS == frozenset(
        spec.transport for spec in BUILTIN_ADAPTER_REGISTRY.with_trait("radio")
    )


def test_architecture_sdk_allowances_derive_from_registry() -> None:
    assert SESSION_ALLOWED_SDKS == {
        spec.transport: spec.sdk_import_roots for spec in BUILTIN_ADAPTER_REGISTRY
    }
    assert set(adapter_sdk_packages()) == {
        sdk for spec in BUILTIN_ADAPTER_REGISTRY for sdk in spec.sdk_import_roots
    }


def test_registered_symbols_resolve() -> None:
    for spec in BUILTIN_ADAPTER_REGISTRY:
        assert spec.config.load() is not None
        assert spec.adapter.load() is not None
        assert spec.fake_adapter.load() is not None
        assert spec.native_namespace_reader.load() is not None
        assert spec.versioned_namespace_reader.load() is not None
        assert spec.attribution_projector.load() is not None

        optional_refs = (
            spec.runtime_config,
            spec.renderer_factory,
            spec.dependency_probe,
            spec.runtime_config_preparer,
            spec.runtime_directories,
            spec.cli_register,
            spec.cli_dispatch,
        )
        for ref in optional_refs:
            if ref is not None:
                assert ref.load() is not None


def test_runtime_schema_transport_groups_match_registry() -> None:
    schema = json.loads(
        (_ROOT / "docs" / "schemas" / "runtime-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    groups = schema["properties"]["adapters"]["properties"]
    assert tuple(groups) == registered_transports()
