"""Focused machine-schema constraints for transport-native snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS = _ROOT / "docs" / "schemas"
_EXAMPLES = _SCHEMAS / "examples"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_meshtastic_snapshot_schema_rejects_non_json_nested_values() -> None:
    """Arbitrary snapshot keys remain allowed, but nested values stay JSON-safe."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_json(_SCHEMAS / "meshtastic-native-metadata.schema.json")
    example = _load_json(_EXAMPLES / "meshtastic-native-metadata-example.json")
    meshtastic = example["meshtastic"]
    assert isinstance(meshtastic, dict)
    packet = meshtastic["packet"]
    assert isinstance(packet, dict)
    packet["unsafe"] = object()

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=example, schema=schema)
