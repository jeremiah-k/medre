"""Tests for the unified :func:`medre.cli.json.to_json` helper (D4).

Verifies the helper handles dicts, dataclasses, and ``msgspec.Struct``
inputs and produces the canonical sorted-keys/indented shape used by
every ``--json`` CLI command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import msgspec

from medre.cli.json import to_json


@dataclass(frozen=True)
class _SampleDataclass:
    name: str
    count: int
    items: tuple[str, ...]


class _SampleMsgspecStruct(msgspec.Struct):
    name: str
    count: int
    tags: list[str]


def test_to_json_dict_is_sorted_and_indented() -> None:
    out = to_json({"b": 2, "a": 1, "nested": {"y": 1, "x": 2}})
    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": 2, "nested": {"x": 2, "y": 1}}
    assert out.startswith("{\n  ")
    # Keys must be alphabetically sorted within each object.
    assert out.index('"a"') < out.index('"b"')
    assert out.index('"nested"') > out.index('"a"')


def test_to_json_dataclass_is_sorted_and_indented() -> None:
    dc = _SampleDataclass(name="alpha", count=3, items=("x", "y"))
    out = to_json(dc)
    parsed = json.loads(out)
    assert parsed == {"count": 3, "items": ["x", "y"], "name": "alpha"}
    # Dataclass tuple becomes a JSON array, not a string-keyed object.
    assert out.index('"count"') < out.index('"items"') < out.index('"name"')


def test_to_json_msgspec_struct_is_sorted_and_indented() -> None:
    struct = _SampleMsgspecStruct(name="alpha", count=2, tags=["a", "b"])
    out = to_json(struct)
    parsed = json.loads(out)
    assert parsed == {"count": 2, "name": "alpha", "tags": ["a", "b"]}
    assert out.index('"count"') < out.index('"name"') < out.index('"tags"')


def test_to_json_datetime_uses_msgspec_native_encoding() -> None:
    """Aware datetimes use msgspec's native RFC 3339 JSON encoding."""

    class _Struct(msgspec.Struct):
        when: Any

    out = to_json(_Struct(when=datetime(2026, 1, 1, tzinfo=UTC)))
    parsed = json.loads(out)
    assert parsed == {"when": "2026-01-01T00:00:00Z"}


def test_to_json_unsupported_value_falls_back_to_str() -> None:
    class _Unsupported:
        def __str__(self) -> str:
            return "unsupported-value"

    out = to_json({"value": _Unsupported()})
    assert json.loads(out) == {"value": "unsupported-value"}


def test_to_json_list_at_root_is_supported() -> None:
    """Top-level lists (not wrapped in a dict) work — common shape for
    trace event timelines.
    """
    out = to_json([{"id": 2}, {"id": 1}])
    parsed = json.loads(out)
    assert parsed == [{"id": 2}, {"id": 1}]
