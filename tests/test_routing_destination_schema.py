"""Contract tests for structured route-destination JSON schema rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1] / "docs/schemas/routing-config.schema.json"
    ).read_text(encoding="utf-8")
)


def _route(destination: dict, *, dest_adapters: list[str] | None = None) -> dict:
    return {
        "route_id": "structured-destination",
        "source_adapters": ["source"],
        "dest_adapters": ["target"] if dest_adapters is None else dest_adapters,
        "dest_destination": destination,
    }


def test_name_destinations_require_name_and_reject_hash() -> None:
    for kind in ("channel",):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=_route({"kind": kind}), schema=_SCHEMA)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                instance=_route(
                    {
                        "kind": kind,
                        "destination_name": "general",
                        "destination_hash": "opaque",
                    }
                ),
                schema=_SCHEMA,
            )


def test_structured_destination_rejects_selectors_and_multiple_adapters() -> None:
    valid = {
        "kind": "lxmf_destination",
        "destination_hash": "0123456789abcdef0123456789abcdef",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={**_route(valid), "dest_channel": "legacy"}, schema=_SCHEMA
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=_route(valid, dest_adapters=["one", "two"]), schema=_SCHEMA
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={
                **_route(valid),
                "context_map": {"0": {"dest_context": "!room:example.org"}},
            },
            schema=_SCHEMA,
        )


def test_route_config_ref_requires_nonempty_adapter_arrays() -> None:
    valid_destination = {
        "kind": "lxmf_destination",
        "destination_hash": "0123456789abcdef0123456789abcdef",
    }
    for field in ("source_adapters", "dest_adapters"):
        route = _route(valid_destination)
        route[field] = []
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={"routes": [route]}, schema=_SCHEMA)


def test_schema_accepts_community_addressing_kind() -> None:
    jsonschema.validate(
        _route({"kind": "community_device", "metadata": {"device_id": "sensor-1"}}),
        _SCHEMA,
    )
