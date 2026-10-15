"""Direct construction cannot bypass policy checks or provider ownership."""

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import SymbolRef
from medre.config.errors import ConfigValidationError
from medre.config.model import AdapterConfigSet, RuntimeConfig, StorageConfig
from medre.config.paths import resolve
from medre.config.route_expansion import expand_route_config
from medre.config.routes import BridgePolicy, RouteConfig
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.adapter_spec import make_adapter_spec


@dataclass(frozen=True)
class IndependentRuntimeConfig:
    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "fake"
    config: object = None


def test_independent_provider_wrapper_does_not_require_core_inheritance(
    monkeypatch, tmp_path
):
    spec = replace(
        make_adapter_spec("private_chat"),
        fake_adapter=SymbolRef(
            "medre.adapters.fakes.transport", "FakeTransportAdapter"
        ),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    adapters = AdapterConfigSet(private_chat={"node": IndependentRuntimeConfig("node")})
    adapters.validate()
    app = RuntimeBuilder(
        RuntimeConfig(adapters=adapters, storage=StorageConfig(backend="memory")),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    ).build()
    assert set(app.adapters) == {"node"}


@pytest.mark.parametrize("field", [name for name, _ in BridgePolicy._ALLOWLIST_FIELDS])
@pytest.mark.parametrize("value", [None, False, 1, "message.text", (None,)])
def test_direct_policy_cannot_silently_remove_or_corrupt_a_filter(field, value):
    with pytest.raises(ConfigValidationError, match=field):
        policy = BridgePolicy(**{field: value})
        expand_route_config(
            RouteConfig(
                "guarded",
                ("source",),
                ("target",),
                policy=policy,
            )
        )


def test_direct_policy_copies_mutable_input():
    kinds = ["message.text"]
    policy = BridgePolicy(allowed_event_types=kinds)
    kinds.clear()
    assert policy.allowed_event_types == ("message.text",)


@pytest.mark.parametrize("block", ["policy", "retry", "context_map"])
def test_schemas_reject_explicit_null_route_controls(block):
    directory = Path(__file__).resolve().parents[1] / "docs/schemas"
    routing = Draft202012Validator(
        json.loads((directory / "routing-config.schema.json").read_text())
    )
    runtime = Draft202012Validator(
        json.loads((directory / "runtime-config.schema.json").read_text())
    )
    route = {
        "route_id": "guarded",
        "source_adapters": ["source"],
        "dest_adapters": ["target"],
    }
    assert routing.is_valid(route)
    assert not routing.is_valid({**route, block: None})
    config = {
        "routes": {
            "guarded": {key: value for key, value in route.items() if key != "route_id"}
        }
    }
    assert runtime.is_valid(config)
    config["routes"]["guarded"][block] = None
    assert not runtime.is_valid(config)
