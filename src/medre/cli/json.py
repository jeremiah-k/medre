"""JSON serialisation helpers for CLI output."""
from __future__ import annotations


def _struct_to_json(obj: object) -> str:
    """Serialise a msgspec Struct (or list of Structs) to deterministic JSON."""
    import json
    import msgspec

    raw = msgspec.json.encode(obj)
    return json.dumps(json.loads(raw), sort_keys=True, indent=2)
