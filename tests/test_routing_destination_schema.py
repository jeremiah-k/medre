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
        "dest_adapters": dest_adapters or ["target"],
        "dest_destination": destination,
    }


def test_lxmf_destination_requires_32_hex_hash() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=_route({"kind": "lxmf_destination"}), schema=_SCHEMA
        )
    jsonschema.validate(
        instance=_route(
            {
                "kind": "lxmf_destination",
                "destination_hash": "0123456789abcdef0123456789abcdef",
            }
        ),
        schema=_SCHEMA,
    )


def test_name_destinations_require_name_and_reject_hash() -> None:
    for kind in ("channel", "matrix_room"):
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


def test_meshcore_contact_requires_nonempty_hash_or_name() -> None:
    for destination in (
        {"kind": "meshcore_contact"},
        {"kind": "meshcore_contact", "destination_hash": None},
        {"kind": "meshcore_contact", "destination_name": None},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=_route(destination), schema=_SCHEMA)


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
                "channel_room_map": {"0": {"room": "!room:example.org"}},
            },
            schema=_SCHEMA,
        )
