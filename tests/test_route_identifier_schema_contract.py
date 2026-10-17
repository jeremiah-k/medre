"""Route identifiers agree across direct models, mapping keys and editor schemas."""

import json
from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.routes import RouteConfig

SCHEMAS = Path(__file__).resolve().parents[1] / "docs" / "schemas"


@pytest.mark.parametrize("route_id", ["relay", "relay\n", "relay\r\n"])
@pytest.mark.parametrize("location", ["mapping", "component"])
def test_schema_route_identifiers_match_model(route_id, location):
    jsonschema = pytest.importorskip("jsonschema")
    data = {"source_adapters": ["a"], "dest_adapters": ["b"]}
    if location == "mapping":
        schema = json.loads((SCHEMAS / "runtime-config.schema.json").read_text())
        instance = {"routes": {route_id: data}}
    else:
        schema = json.loads((SCHEMAS / "routing-config.schema.json").read_text())
        instance = {"route_id": route_id, **data}
    valid = jsonschema.Draft202012Validator(schema).is_valid(instance)
    if route_id == "relay":
        assert valid
        assert RouteConfig.from_dict(route_id, data).route_id == route_id
    else:
        assert not valid
        with pytest.raises(ConfigValidationError):
            RouteConfig.from_dict(route_id, data)


def test_entry_level_route_id_is_rejected_by_schema_and_model():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS / "runtime-config.schema.json").read_text())
    data = {"route_id": "other", "source_adapters": ["a"], "dest_adapters": ["b"]}
    assert not jsonschema.Draft202012Validator(schema).is_valid(
        {"routes": {"relay": data}}
    )
    with pytest.raises(ConfigValidationError, match="unknown key"):
        RouteConfig.from_dict("relay", data)
