"""Samples depend on installed capabilities, without native field inference."""

import json
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import AdapterSample, SymbolRef
from medre.config._yaml import parse_yaml_config
from medre.config.loader import load_config
from medre.config.sample import generate_sample_config
from tests.helpers.adapter_spec import make_adapter_spec


def sample():
    return AdapterSample(
        {"label": "private sensor", "contexts": ["opaque-conversation"]},
        "opaque-conversation",
    )


def test_core_only_sample_loads_without_unknown_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(()))
    text = generate_sample_config()
    data = parse_yaml_config(text)
    assert data["adapters"] == data["routes"] == {}
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/runtime-config.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(data)
    assert "Install an adapter" in text
    path = tmp_path / "sample.yaml"
    path.write_text(text)
    load_config(str(path))


def test_arbitrary_provider_owns_sample_fields_and_addresses(monkeypatch):
    spec = replace(
        make_adapter_spec("independent_chat"),
        sample_config=SymbolRef(__name__, "sample"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    data = parse_yaml_config(generate_sample_config())
    assert set(data["adapters"]) == {"independent_chat"}
    assert set(data["adapters"]["independent_chat"]) == {"sample_1", "sample_2"}
    assert data["routes"]["sample_bridge"]["dest_channel"] == "opaque-conversation"
    assert data["adapters"]["independent_chat"]["sample_1"]["label"] == "private sensor"


def test_provider_without_fake_is_not_given_a_fake_sample(monkeypatch):
    spec = replace(
        make_adapter_spec("real_only"),
        fake_adapter=None,
        sample_config=SymbolRef(__name__, "sample"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    assert parse_yaml_config(generate_sample_config())["adapters"] == {}


def test_independent_sample_contributors_never_collide_on_instance_ids(monkeypatch):
    specs = tuple(
        replace(make_adapter_spec(name), sample_config=SymbolRef(__name__, "sample"))
        for name in ("first", "second")
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(specs)
    )
    data = parse_yaml_config(generate_sample_config())
    assert set(data["adapters"]["first"]) == {"sample_1"}
    assert set(data["adapters"]["second"]) == {"sample_2"}
    assert data["routes"]["sample_bridge"]["source_adapters"] == ["sample_1"]
