"""Source drift detection between docs/schemas JSON schemas and source models.

Asserts that schema top-level properties match source dataclass /
msgspec.Struct fields for stable models where the mapping is 1:1.  If a
source model adds or renames a field without updating the schema, these
tests fail.  Split from the schema-example validation suite so each file
stays well under the test-file size ceiling.
"""

from __future__ import annotations

import json
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

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
    from medre.config.adapters.lxmf import LxmfConfig
    from medre.config.adapters.matrix import MatrixConfig
    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig

    schema = _load_json(_SCHEMAS_DIR / "adapter-config.schema.json")
    arms = {arm["title"]: arm for arm in schema["oneOf"]}

    checks = [
        ("MatrixConfig", MatrixConfig),
        ("MeshtasticConfig", MeshtasticConfig),
        ("MeshCoreConfig", MeshCoreConfig),
        ("LxmfConfig", LxmfConfig),
    ]
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
