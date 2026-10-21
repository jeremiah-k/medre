"""Bounded, JSON-safe value snapshots for declarative contracts.

Copy caller-owned containers before freezing them; frozen dataclasses alone
cannot make nested dictionaries, lists, or sets immutable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from medre.core.events.metadata import FrozenDict

MAX_JSON_DEPTH = 64


def freeze_json_mapping(value: object, *, label: str) -> FrozenDict:
    """Copy and deeply freeze a finite JSON object, rejecting cycles.

    Tuples and mappings are accepted as immutable input equivalents of JSON
    arrays and objects. Keys must be strings. Depth is bounded before JSON
    encoding so invalid recursive inputs fail as contract errors.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    active: set[int] = set()

    def normalize(item: Any, depth: int) -> Any:
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{label} exceeds maximum JSON depth")
        if isinstance(item, (Mapping, list, tuple)):
            identity = id(item)
            if identity in active:
                raise ValueError(f"{label} contains a JSON cycle")
            active.add(identity)
            try:
                if isinstance(item, Mapping):
                    result = {}
                    for key, entry in item.items():
                        if not isinstance(key, str):
                            raise TypeError(f"{label} keys must be strings")
                        result[key] = normalize(entry, depth + 1)
                    return result
                return [normalize(entry, depth + 1) for entry in item]
            finally:
                active.remove(identity)
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        raise TypeError(f"{label} must contain only JSON-safe values")

    normalized = normalize(value, 0)
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must contain only JSON-safe values") from exc
    return FrozenDict(normalized)
