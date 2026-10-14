# medre-test-tier: compatibility
"""Fail native target mismatches before adapter construction or delivery."""

import pytest
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    GenericAdapterRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import resolve
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDestinationConfig,
)
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError


@pytest.mark.parametrize(
    ("transport", "kind", "address"),
    [
        ("matrix", "lxmf_destination", {"destination_hash": "0" * 32}),
        ("lxmf", "matrix_room", {"destination_name": "!room:test"}),
        ("meshcore", "matrix_room", {"destination_name": "!room:test"}),
    ],
)
@pytest.mark.parametrize("via_context_map", [False, True])
def test_native_destination_must_belong_to_target(
    transport: str,
    kind: str,
    address: dict,
    via_context_map: bool,
    tmp_path,
    monkeypatch,
) -> None:
    routes = RouteConfigSet(
        routes=(
            RouteConfig(
                route_id="mismatch",
                source_adapters=("source",),
                dest_adapters=("target",),
                **(
                    {
                        "context_map": {
                            "source": ContextMapEntry(
                                dest_destination=RouteDestinationConfig(
                                    kind=kind, **address
                                )
                            )
                        }
                    }
                    if via_context_map
                    else {
                        "dest_destination": RouteDestinationConfig(kind=kind, **address)
                    }
                ),
            ),
        )
    )
    with pytest.raises(ConfigValidationError, match="is not supported by adapter"):
        routes.validate_destinations({"target": transport})
    config = RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        routes=routes,
        adapters=AdapterConfigSet(
            groups={
                transport: {
                    "target": GenericAdapterRuntimeConfig(
                        adapter_id="target", adapter_kind="fake"
                    )
                }
            }
        ),
    )
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    builder = RuntimeBuilder(config, resolve())

    def unexpected(*args, **kwargs):
        raise AssertionError("adapter construction happened before address validation")

    monkeypatch.setattr(builder, "_build_adapters", unexpected)
    with pytest.raises(RuntimeConfigError, match="is not supported by adapter"):
        builder.build()
