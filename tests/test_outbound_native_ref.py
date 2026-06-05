"""Tests for OutboundNativeRefRecord._unwrap MappingProxyType recursion.

The ``_unwrap`` closure inside ``OutboundNativeRefRecord.__post_init__``
recursively converts ``MappingProxyType`` instances to plain ``dict`` so that
``json.dumps`` can serialise the structure.  These tests exercise lines 365-370
of ``src/medre/core/contracts/adapter.py`` through the public constructor.
"""

from __future__ import annotations

from collections import UserDict
from types import MappingProxyType

from medre.core.contracts.adapter import OutboundNativeRefRecord


def _make(**meta: object) -> OutboundNativeRefRecord:
    """Helper: build a record with the given metadata dict."""
    return OutboundNativeRefRecord(
        event_id="evt-1",
        adapter="mesh-1",
        native_channel_id="0",
        native_message_id="42",
        metadata=meta,
    )


# -- Line 365-366: MappingProxyType → plain dict at top level ---------------


def test_top_level_mapping_proxy_accepted() -> None:
    """MappingProxyType as the top-level metadata value is unwrapped to dict."""
    proxy = MappingProxyType({"packet_id": 7})
    record = _make(nested=proxy)
    # metadata is stored as MappingProxyType, but the inner value is now a plain dict
    inner = record.metadata["nested"]
    assert isinstance(inner, dict)
    assert inner == {"packet_id": 7}


# -- Line 367-368: dict → recurse into values -------------------------------


def test_deeply_nested_mapping_proxy_unwrapped() -> None:
    """MappingProxyType nested two levels deep is fully unwrapped."""
    deep = MappingProxyType({"inner_key": "inner_val"})
    mid = MappingProxyType({"deep": deep})
    record = _make(top=mid)
    top_val = record.metadata["top"]
    assert isinstance(top_val, dict)
    assert isinstance(top_val["deep"], dict)
    assert top_val["deep"] == {"inner_key": "inner_val"}


# -- Line 369-370: list/tuple → recurse into items --------------------------


def test_list_with_mapping_proxy_items_unwrapped() -> None:
    """MappingProxyType inside a list is unwrapped for each item."""
    item = MappingProxyType({"a": 1})
    record = _make(items=[item])
    items = record.metadata["items"]
    assert isinstance(items, list)
    assert isinstance(items[0], dict)
    assert items[0] == {"a": 1}


def test_tuple_with_mapping_proxy_preserves_type_and_unwraps() -> None:
    """MappingProxyType inside a tuple: tuple type preserved, items unwrapped."""
    item = MappingProxyType({"b": 2})
    record = _make(items=(item,))
    items = record.metadata["items"]
    assert isinstance(items, tuple)
    assert isinstance(items[0], dict)
    assert items[0] == {"b": 2}


def test_mixed_nested_structure_unwrapped() -> None:
    """Complex nesting: dict → list → MappingProxyType all unwrapped."""
    leaf = MappingProxyType({"x": "y"})
    record = _make(data={"list": [leaf, "scalar"]})
    data = record.metadata["data"]
    assert isinstance(data, dict)
    assert isinstance(data["list"], list)
    assert isinstance(data["list"][0], dict)
    assert data["list"][0] == {"x": "y"}
    assert data["list"][1] == "scalar"


# -- Plain dict metadata (baseline) -----------------------------------------


def test_plain_dict_metadata_no_error() -> None:
    """Plain dict metadata passes through without error."""
    record = _make(packet_id=1, label="test")
    assert dict(record.metadata) == {"packet_id": 1, "label": "test"}


def test_top_level_userdict_metadata_accepted() -> None:
    """Non-dict Mapping metadata is normalized and accepted."""
    record = OutboundNativeRefRecord(
        event_id="evt-1",
        adapter="mesh-1",
        native_channel_id="0",
        native_message_id="42",
        metadata=UserDict({"packet_id": 7}),
    )
    assert dict(record.metadata) == {"packet_id": 7}
