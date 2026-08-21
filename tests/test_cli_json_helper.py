"""Tests for the unified :func:`medre.cli.json.to_json` helper (D4).

Verifies the helper handles dicts, dataclasses, and ``msgspec.Struct``
inputs and produces the canonical sorted-keys/indented shape used by
every ``--json`` CLI command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


def test_to_json_datetime_falls_through_to_str() -> None:
    """``datetime`` values are not msgspec-native, so the helper falls
    back to ``default=str`` and renders them as ISO strings instead of
    raising ``TypeError``.
    """

    class _Struct(msgspec.Struct):
        when: Any

    out = to_json(_Struct(when="2026-01-01T00:00:00"))
    parsed = json.loads(out)
    assert parsed == {"when": "2026-01-01T00:00:00"}


def test_to_json_list_at_root_is_supported() -> None:
    """Top-level lists (not wrapped in a dict) work — common shape for
    trace event timelines.
    """
    out = to_json([{"id": 2}, {"id": 1}])
    parsed = json.loads(out)
    assert parsed == [{"id": 2}, {"id": 1}]
