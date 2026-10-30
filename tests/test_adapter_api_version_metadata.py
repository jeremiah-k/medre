"""SDK-free revision metadata survives provider discovery without loading SDKs."""

from dataclasses import replace

import pytest

from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import ADAPTER_API_VERSION
from tests.helpers.adapter_spec import make_adapter_spec


def test_declared_api_version_is_preserved_by_registry() -> None:
    assert type(ADAPTER_API_VERSION) is int
    spec = replace(make_adapter_spec("sensor"), api_version=1)
    registered = AdapterTypeRegistry((spec,)).require("sensor")
    assert registered is spec
    assert registered.api_version == 1


def test_unknown_api_version_is_not_replaced_by_core_version() -> None:
    spec = make_adapter_spec("sensor")
    assert spec.api_version is None
    assert AdapterTypeRegistry((spec,)).require("sensor").api_version is None


@pytest.mark.parametrize("version", [0, 2, True, False, "1", 1.0])
def test_explicit_incompatible_api_versions_are_rejected(version) -> None:
    spec = replace(make_adapter_spec("sensor"), api_version=version)
    with pytest.raises(ValueError, match="API version"):
        AdapterTypeRegistry((spec,))
