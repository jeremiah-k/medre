"""A synthetic community address for generic config-shape tests."""

from dataclasses import replace

import pytest
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import DestinationSpec, SymbolRef
from tests.helpers.adapter_spec import make_adapter_spec


def validate_destination(destination) -> None:
    """Accept opaque values; native address syntax is outside these tests."""


@pytest.fixture(autouse=True)
def registered_test_destination(monkeypatch):
    spec = replace(
        make_adapter_spec("test_config_provider"),
        destinations=(
            DestinationSpec(
                "test_address", SymbolRef(__name__, "validate_destination")
            ),
        ),
    )
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        AdapterTypeRegistry((*adapter_registry.iter_adapter_specs(), spec)),
    )
