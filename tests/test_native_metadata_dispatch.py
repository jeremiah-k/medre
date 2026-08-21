"""Contract tests for shared built-in native-metadata namespace dispatch."""

from __future__ import annotations

import pytest

from medre.adapters._attribution_dispatch import detect_source_platform
from medre.adapters._native_metadata_dispatch import (
    current_native_namespace,
    versioned_native_namespace,
)
from medre.adapters.lxmf.event_shape import LXMF_NATIVE_SCHEMA_VERSION
from medre.adapters.matrix.event_shape import MATRIX_NATIVE_SCHEMA_VERSION
from medre.adapters.meshcore.event_shape import MESHCORE_NATIVE_SCHEMA_VERSION
from medre.adapters.meshtastic.event_shape import MESHTASTIC_NATIVE_SCHEMA_VERSION

_CURRENT_VERSIONS = {
    "matrix": MATRIX_NATIVE_SCHEMA_VERSION,
    "meshtastic": MESHTASTIC_NATIVE_SCHEMA_VERSION,
    "meshcore": MESHCORE_NATIVE_SCHEMA_VERSION,
    "lxmf": LXMF_NATIVE_SCHEMA_VERSION,
}


@pytest.mark.parametrize("transport", sorted(_CURRENT_VERSIONS))
def test_current_native_namespace_accepts_only_current_version(transport: str) -> None:
    current_version = _CURRENT_VERSIONS[transport]
    current = {transport: {"schema_version": current_version, "marker": transport}}
    future = {transport: {"schema_version": current_version + 1, "marker": transport}}

    assert current_native_namespace(current, transport)["marker"] == transport
    assert current_native_namespace(future, transport) == {}


@pytest.mark.parametrize("transport", sorted(_CURRENT_VERSIONS))
def test_versioned_native_namespace_accepts_future_positive_version(
    transport: str,
) -> None:
    future = {
        transport: {
            "schema_version": _CURRENT_VERSIONS[transport] + 1,
            "marker": transport,
        }
    }

    assert versioned_native_namespace(future, transport)["marker"] == transport


@pytest.mark.parametrize("transport", sorted(_CURRENT_VERSIONS))
@pytest.mark.parametrize("bad_version", [None, True, 0, -1, 1.0, "1"])
def test_versioned_native_namespace_rejects_invalid_versions(
    transport: str,
    bad_version: object,
) -> None:
    native = {transport: {"schema_version": bad_version}}

    assert versioned_native_namespace(native, transport) == {}


def test_native_namespace_dispatch_rejects_unknown_transport() -> None:
    native = {"unknown": {"schema_version": 1}}

    assert current_native_namespace(native, "unknown") == {}
    assert versioned_native_namespace(native, "unknown") == {}


@pytest.mark.parametrize("transport", sorted(_CURRENT_VERSIONS))
def test_future_native_schema_still_identifies_platform(transport: str) -> None:
    native = {transport: {"schema_version": _CURRENT_VERSIONS[transport] + 1}}

    assert detect_source_platform("generic", native) == transport
