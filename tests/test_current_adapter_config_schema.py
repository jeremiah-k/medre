# medre-test-tier: compatibility
"""Current provider configuration can evolve without rewriting release snapshots."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1] / "docs/schemas"


def schema(current=False):
    path = ROOT / "current" if current else ROOT
    return json.loads((path / "adapter-config.schema.json").read_text())


def test_current_schema_is_only_the_reviewed_optional_addition():
    released = schema()
    current = schema(True)
    Draft202012Validator.check_schema(current)
    expected = deepcopy(released)
    expected["$id"] = current["$id"]
    arm = next(x for x in expected["anyOf"] if x["title"] == "MeshtasticConfig")
    field = next(x for x in current["anyOf"] if x["title"] == "MeshtasticConfig")[
        "properties"
    ]["startup_packet_drain_seconds"]
    assert field["type"] == "number" and field["minimum"] == 0
    assert field["maximum"] == sys.float_info.max
    from medre.config.adapters.meshtastic import MeshtasticConfig

    assert (
        field["default"]
        == MeshtasticConfig(adapter_id="radio").startup_packet_drain_seconds
        == 15
    )
    arm["properties"]["startup_packet_drain_seconds"] = field
    assert current == expected
    assert current["$id"].endswith("/current/adapter-config.schema.json")


@pytest.mark.parametrize("window", [0, 0.5, 15, 60])
def test_current_schema_accepts_drain_configs_without_changing_the_baseline(window):
    config = {"adapter_id": "radio", "connection_type": "fake"}
    assert Draft202012Validator(schema()).is_valid(config)
    assert Draft202012Validator(schema(True)).is_valid(config)
    config["startup_packet_drain_seconds"] = window
    assert not Draft202012Validator(schema()).is_valid(config)
    assert Draft202012Validator(schema(True)).is_valid(config)


@pytest.mark.parametrize("window", [True, False, -1, "15", None, 10**309])
def test_current_schema_rejects_invalid_drain_values(window):
    assert not Draft202012Validator(schema(True)).is_valid(
        {
            "adapter_id": "radio",
            "connection_type": "fake",
            "startup_packet_drain_seconds": window,
        }
    )
