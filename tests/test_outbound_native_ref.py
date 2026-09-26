"""Tests for deeply immutable adapter hand-off metadata.

Deferred completion owns exactly one hand-off fact. Transport metadata therefore
lives on ``DeferredHandoffCompleted.handoff`` rather than being duplicated on
the feedback envelope. These tests pin normalization of generic ``Mapping``
inputs and deep immutability at the adapter/core boundary.
"""

from __future__ import annotations

from collections import UserDict
from types import MappingProxyType

import pytest

from medre.core.contracts.delivery import AdapterHandoffResult, DeferredHandoffCompleted
from tests.helpers.delivery_callbacks import (
    make_attempt_provenance,
    make_deferred_completion,
)


def _make(**meta: object) -> DeferredHandoffCompleted:
    """Build one completion carrying *meta* on its transport hand-off fact."""
    return make_deferred_completion(
        event_id="evt-1",
        adapter="mesh-1",
        native_channel_id="0",
        native_message_id="42",
        metadata=meta,
        attempt_provenance=make_attempt_provenance(
            event_id="evt-1",
            target_adapter="mesh-1",
            outbox_id="obox-contract",
            attempt_number=1,
            target_channel="0",
        ),
    )


def test_top_level_mapping_proxy_accepted_and_frozen() -> None:
    """Mapping values normalize to immutable dict-compatible metadata."""
    record = _make(nested=MappingProxyType({"packet_id": 7}))
    inner = record.handoff.metadata["nested"]
    assert isinstance(inner, dict)
    assert dict(inner) == {"packet_id": 7}
    with pytest.raises(TypeError):
        inner["packet_id"] = 8  # type: ignore[index]


def test_deeply_nested_mapping_proxy_is_frozen() -> None:
    """Nested mapping proxies normalize recursively without duplicate ownership."""
    deep = MappingProxyType({"inner_key": "inner_val"})
    mid = MappingProxyType({"deep": deep})
    top = record_top = _make(top=mid).handoff.metadata["top"]
    assert isinstance(record_top, dict)
    nested = top["deep"]
    assert isinstance(nested, dict)
    assert dict(nested) == {"inner_key": "inner_val"}
    with pytest.raises(TypeError):
        nested["inner_key"] = "changed"  # type: ignore[index]


def test_list_with_mapping_items_becomes_immutable_sequence() -> None:
    """Lists become tuples and nested mappings remain immutable."""
    record = _make(items=[MappingProxyType({"a": 1})])
    items = record.handoff.metadata["items"]
    assert isinstance(items, tuple)
    assert isinstance(items[0], dict)
    assert dict(items[0]) == {"a": 1}


def test_tuple_with_mapping_proxy_remains_immutable_sequence() -> None:
    """Tuple input remains tuple-shaped after deep freezing."""
    record = _make(items=(MappingProxyType({"b": 2}),))
    items = record.handoff.metadata["items"]
    assert isinstance(items, tuple)
    assert dict(items[0]) == {"b": 2}


def test_mixed_nested_structure_is_deeply_frozen() -> None:
    """Complex mapping/sequence input normalizes to immutable JSON-safe values."""
    leaf = MappingProxyType({"x": "y"})
    data = _make(data={"list": [leaf, "scalar"]}).handoff.metadata["data"]
    assert isinstance(data, dict)
    items = data["list"]
    assert isinstance(items, tuple)
    assert dict(items[0]) == {"x": "y"}
    assert items[1] == "scalar"


def test_plain_dict_metadata_is_frozen_on_handoff() -> None:
    """Plain mapping metadata is exposed through the single hand-off authority."""
    record = _make(packet_id=1, label="test")
    assert dict(record.handoff.metadata) == {"packet_id": 1, "label": "test"}
    with pytest.raises(TypeError):
        record.handoff.metadata["packet_id"] = 2


def test_reserved_top_level_metadata_key_is_rejected() -> None:
    """Lifecycle-looking keys cannot shadow the closed hand-off vocabulary."""
    with pytest.raises(ValueError, match="reserved by the delivery contract"):
        AdapterHandoffResult(metadata={"status": "sent"})


def test_top_level_userdict_metadata_accepted() -> None:
    """Non-dict Mapping input is normalized by the contract boundary."""
    record = make_deferred_completion(
        event_id="evt-1",
        adapter="mesh-1",
        native_channel_id="0",
        native_message_id="42",
        metadata=UserDict({"packet_id": 7}),  # type: ignore[arg-type]
        attempt_provenance=make_attempt_provenance(
            event_id="evt-1",
            target_adapter="mesh-1",
            outbox_id="obox-contract",
            attempt_number=1,
            target_channel="0",
        ),
    )
    assert dict(record.handoff.metadata) == {"packet_id": 7}
