# medre-test-tier: compatibility
"""Source drift detection between docs/schemas JSON schemas and source models.

Asserts that schema top-level properties match source dataclass /
msgspec.Struct fields for stable models where the mapping is 1:1.  If a
source model adds or renames a field without updating the schema, these
tests fail.  Split from the schema-example validation suite so each file
stays well under the test-file size ceiling.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths / helpers
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _ROOT / "docs" / "schemas"


def _load_json(path: Path) -> dict[str, Any]:
    """Load and parse a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Source drift detection for stable models
# ---------------------------------------------------------------------------


def test_canonical_event_schema_matches_source() -> None:
    """canonical-event.schema.json properties must match CanonicalEvent fields."""
    from medre.core.events.canonical import CanonicalEvent

    schema = _load_json(_SCHEMAS_DIR / "canonical-event.schema.json")
    schema_props = set(schema.get("properties", {}).keys())
    source_fields = set(CanonicalEvent.__struct_fields__)
    missing = source_fields - schema_props
    assert not missing, f"CanonicalEvent fields missing from schema: {sorted(missing)}"
    extra = schema_props - source_fields
    assert not extra, f"Schema properties absent from CanonicalEvent: {sorted(extra)}"


def test_delivery_receipt_schema_matches_source() -> None:
    """delivery-receipt.schema.json properties must match DeliveryReceipt fields."""
    from medre.core.events.canonical import DeliveryReceipt

    schema = _load_json(_SCHEMAS_DIR / "delivery-receipt.schema.json")
    schema_props = set(schema.get("properties", {}).keys())
    source_fields = set(DeliveryReceipt.__struct_fields__)
    missing = source_fields - schema_props
    assert not missing, f"DeliveryReceipt fields missing from schema: {sorted(missing)}"
    extra = schema_props - source_fields
    assert not extra, f"Schema properties absent from DeliveryReceipt: {sorted(extra)}"


def test_delivery_result_schema_matches_source() -> None:
    """delivery-result.schema.json properties must match AdapterHandoffResult fields."""
    from medre.core.contracts.delivery import AdapterHandoffResult

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    schema_props = set(schema.get("properties", {}).keys())
    source_fields = set(AdapterHandoffResult.__struct_fields__)
    missing = source_fields - schema_props
    assert (
        not missing
    ), f"AdapterHandoffResult fields missing from schema: {sorted(missing)}"
    extra = schema_props - source_fields
    assert (
        not extra
    ), f"Schema properties absent from AdapterHandoffResult: {sorted(extra)}"


def test_route_config_schema_matches_source() -> None:
    """routing-config.schema.json RouteConfig arm must cover all
    RouteConfig dataclass fields (source → schema drift detection).

    This would have caught missing source_origin_label / dest_origin_label
    documentation in the schema.
    """
    from medre.config.routes import RouteConfig

    schema = _load_json(_SCHEMAS_DIR / "routing-config.schema.json")
    route_arm = next(
        (arm for arm in schema["oneOf"] if arm.get("title") == "RouteConfig"),
        None,
    )
    assert route_arm is not None, "routing-config.schema.json missing RouteConfig arm"
    schema_props = set(route_arm.get("properties", {}).keys())
    source_fields = {f.name for f in dc_fields(RouteConfig)}
    missing = source_fields - schema_props
    assert not missing, (
        f"RouteConfig fields missing from routing-config.schema.json: "
        f"{sorted(missing)}"
    )
    # Reverse drift: schema → source. Catches phantom properties whose
    # dataclass field was removed (e.g. MeshCore channel_mapping).
    extra = schema_props - source_fields
    assert not extra, (
        f"routing-config.schema.json properties not in RouteConfig: " f"{sorted(extra)}"
    )


def test_adapter_config_schema_matches_source() -> None:
    """adapter-config.schema.json each oneOf arm must cover all fields
    from the corresponding source dataclass (source → schema drift).

    This would have caught F-001 (missing Meshtastic packet-routing
    fields) at the moment they were added to the dataclass.
    """
    from medre.adapter_registry import get_adapter_spec

    schema = _load_json(_SCHEMAS_DIR / "adapter-config.schema.json")
    arms = {arm["title"]: arm for arm in schema["oneOf"]}
    providers = _load_json(_ROOT / ".github" / "adapter-compatibility.json")
    checks = []
    for provider in providers:
        transport = provider["transport"]
        spec = get_adapter_spec(transport)
        assert spec is not None, f"Compatibility provider is not installed: {transport}"
        config_cls = spec.config.load()
        checks.append((config_cls.__name__, config_cls))
    assert set(arms) == {title for title, _ in checks}
    for title, datacls in checks:
        assert title in arms, f"Schema missing oneOf arm {title!r}"
        schema_props = set(arms[title].get("properties", {}).keys())
        source_fields = {f.name for f in dc_fields(datacls)}
        missing = source_fields - schema_props
        assert (
            not missing
        ), f"{title}: source fields missing from schema: {sorted(missing)}"
        # Reverse drift: schema → source. Catches phantom properties
        # whose dataclass field was removed (e.g. MeshCore
        # channel_mapping / sync_timeout_ms).
        extra = schema_props - source_fields
        assert (
            not extra
        ), f"{title}: schema properties not in source dataclass: {sorted(extra)}"


def test_delivery_observation_schema_matches_source() -> None:
    """delivery-observation.schema.json matches DeliveryObservation fields."""
    from medre.core.events.canonical import DeliveryObservation

    schema = _load_json(_SCHEMAS_DIR / "delivery-observation.schema.json")
    schema_props = set(schema.get("properties", {}).keys())
    source_fields = set(DeliveryObservation.__struct_fields__)
    missing = source_fields - schema_props
    assert (
        not missing
    ), f"DeliveryObservation fields missing from schema: {sorted(missing)}"
    extra = schema_props - source_fields
    assert (
        not extra
    ), f"Schema properties absent from DeliveryObservation: {sorted(extra)}"


def test_delivery_result_schema_accepts_nonempty_native_ids() -> None:
    """Machine schema accepts the same ordinary native IDs as source."""
    from jsonschema import Draft202012Validator

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    validator = Draft202012Validator(schema)
    payload = {
        "disposition": "transport_handoff",
        "native_message_id": "$event-1",
        "native_channel_id": "room-1",
        "native_thread_id": "thread-1",
        "native_relation_id": "relation-1",
        "confirmation_level": "remote_service",
        "note": "",
        "metadata": {},
    }
    assert not list(validator.iter_errors(payload))


def test_delivery_result_schema_rejects_empty_native_ids() -> None:
    """Machine schema enforces the same non-empty native-ID boundary as source."""
    from jsonschema import Draft202012Validator

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    validator = Draft202012Validator(schema)
    payload = {
        "disposition": "transport_handoff",
        "native_message_id": "message-1",
        "native_channel_id": "",
        "confirmation_level": "local_transport",
        "note": "",
        "metadata": {},
    }
    assert list(validator.iter_errors(payload)), "empty native IDs must be rejected"


def test_delivery_result_schema_rejects_whitespace_only_native_ids() -> None:
    """Machine schema matches source rejection of whitespace-only native IDs."""
    from jsonschema import Draft202012Validator

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    validator = Draft202012Validator(schema)
    payload = {
        "disposition": "transport_handoff",
        "native_message_id": "message-1",
        "native_channel_id": "   ",
        "confirmation_level": "local_transport",
        "note": "",
        "metadata": {},
    }
    assert list(
        validator.iter_errors(payload)
    ), "whitespace-only native IDs must be rejected"


def test_delivery_result_schema_rejects_native_message_on_deferred_handoff() -> None:
    """Deferred admission cannot claim a transport-native message before hand-off."""
    from jsonschema import Draft202012Validator

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    validator = Draft202012Validator(schema)
    payload = {
        "disposition": "deferred",
        "native_message_id": "too-early",
        "native_channel_id": "0",
        "confirmation_level": "local_queue",
        "note": "",
        "metadata": {},
    }
    assert list(
        validator.iter_errors(payload)
    ), "deferred hand-off must not claim a native_message_id"


def test_delivery_result_schema_rejects_strong_confirmation_on_deferred_handoff() -> (
    None
):
    """Deferred admission cannot claim evidence beyond local queue acceptance."""
    from jsonschema import Draft202012Validator

    schema = _load_json(_SCHEMAS_DIR / "delivery-result.schema.json")
    validator = Draft202012Validator(schema)
    payload = {
        "disposition": "deferred",
        "native_message_id": None,
        "native_channel_id": "0",
        "confirmation_level": "remote_service",
        "note": "",
        "metadata": {},
    }
    assert list(
        validator.iter_errors(payload)
    ), "deferred hand-off must not claim remote-service confirmation"


def test_canonical_nested_definitions_match_source_models() -> None:
    """Nested field sets and required constructor inputs are schema boundaries."""
    from medre.core.events import canonical, metadata

    schema = _load_json(_SCHEMAS_DIR / "canonical-event.schema.json")
    definitions = {"CanonicalEvent": schema, **schema["$defs"]}
    for name, definition in definitions.items():
        model = getattr(canonical, name, None) or getattr(metadata, name, None)
        assert model is not None, f"Missing source model for {name}"
        fields = model.__struct_fields__
        assert set(definition["properties"]) == set(fields), name
        required_count = len(fields) - len(model.__struct_defaults__)
        assert set(definition.get("required", [])) == set(fields[:required_count]), name


def test_canonical_json_and_yaml_validation_shapes_match() -> None:
    """Compare the entire schema, including nested definitions and constraints."""
    import yaml

    def validation_shape(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in {"title", "description", "$comment", "examples"}:
                    continue
                if key in {"default", "const", "enum", "dependentRequired"}:
                    result[key] = item
                elif key in {
                    "properties",
                    "patternProperties",
                    "$defs",
                    "definitions",
                    "dependentSchemas",
                    "dependencies",
                }:
                    result[key] = {
                        name: validation_shape(child) for name, child in item.items()
                    }
                else:
                    result[key] = validation_shape(item)
            return result
        if isinstance(value, list):
            return [validation_shape(item) for item in value]
        return value

    schema = _load_json(_SCHEMAS_DIR / "canonical-event.schema.json")
    mirror = yaml.safe_load((_SCHEMAS_DIR / "canonical-event.schema.yaml").read_text())
    assert validation_shape(schema) == validation_shape(mirror)


def test_mirror_audit_preserves_property_names_and_default_data(
    tmp_path, monkeypatch
) -> None:
    import pytest
    import yaml

    monkeypatch.setattr(sys.modules[__name__], "_SCHEMAS_DIR", tmp_path)
    schema = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "default": {"title": "original"},
    }
    (tmp_path / "canonical-event.schema.json").write_text(json.dumps(schema))
    for mirror in (
        {**schema, "properties": {"description": {"type": "integer"}}},
        {**schema, "default": {"title": "changed"}},
    ):
        (tmp_path / "canonical-event.schema.yaml").write_text(yaml.safe_dump(mirror))
        with pytest.raises(AssertionError):
            test_canonical_json_and_yaml_validation_shapes_match()


@pytest.mark.parametrize("platform", ["meshcore", "meshtastic"])
def test_radio_multipart_schema_matches_merged_provider_bounds(platform: str) -> None:
    """Each qualified radio part cap agrees with its provider and published example."""
    from jsonschema import Draft202012Validator

    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig

    config_type = {"meshcore": MeshCoreConfig, "meshtastic": MeshtasticConfig}[platform]
    schema = _load_json(_SCHEMAS_DIR / "adapter-config.schema.json")
    arm = next(
        part for part in schema["oneOf"] if part.get("title") == config_type.__name__
    )
    field = arm["properties"]["max_text_parts"]
    assert field["type"] == "integer"
    assert field["default"] == config_type(adapter_id="radio").max_text_parts == 1
    assert field["minimum"] == 1 and field["maximum"] == 128
    assert "guarded multipart" in field["description"]
    validator = Draft202012Validator(field)
    for valid in (1, 2, 128):
        assert validator.is_valid(valid)
    for invalid in (0, 129, True, False, 1.5, "2", None):
        assert not validator.is_valid(invalid)
    example = _load_json(
        _SCHEMAS_DIR / "examples" / f"adapter-config-{platform}-example.json"
    )
    assert example["max_text_parts"] == field["default"]
