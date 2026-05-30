"""Tests for delivery_target_identity canonical JSON serialization."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest

from medre.core.planning.delivery_plan import delivery_target_identity
from medre.core.routing.models import RouteDestination, RouteTarget

# ---------------------------------------------------------------------------
# 1. Basic identity string
# ---------------------------------------------------------------------------


def test_basic_identity_string() -> None:
    """Channel-only target produces a valid JSON identity string."""
    target = RouteTarget(adapter="matrix", channel="general")
    result = delivery_target_identity(target)

    assert isinstance(result, str)
    assert '"adapter":"matrix"' in result
    assert '"channel":"general"' in result


# ---------------------------------------------------------------------------
# 2. Different adapters produce different identities
# ---------------------------------------------------------------------------


def test_targets_with_different_adapters_differ() -> None:
    a = RouteTarget(adapter="matrix", channel="general")
    b = RouteTarget(adapter="lxmf", channel="general")
    assert delivery_target_identity(a) != delivery_target_identity(b)


# ---------------------------------------------------------------------------
# 3. Nested dict metadata serializes deterministically
# ---------------------------------------------------------------------------


def test_nested_dict_metadata_serializes_deterministically() -> None:
    dest_a = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"a": {"b": 1}},
    )
    dest_b = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"a": {"b": 1}},
    )
    ta = RouteTarget(adapter="matrix", channel=None, destination=dest_a)
    tb = RouteTarget(adapter="matrix", channel=None, destination=dest_b)
    assert delivery_target_identity(ta) == delivery_target_identity(tb)


# ---------------------------------------------------------------------------
# 4. Equivalent metadata produces identical identity
# ---------------------------------------------------------------------------


def test_equivalent_metadata_produces_identical_identity() -> None:
    meta1 = {"room_id": "!abc:example.com", "priority": 1}
    meta2 = {"room_id": "!abc:example.com", "priority": 1}
    dest1 = RouteDestination(
        kind="matrix_room",
        destination_hash=None,
        destination_name=None,
        metadata=meta1,
    )
    dest2 = RouteDestination(
        kind="matrix_room",
        destination_hash=None,
        destination_name=None,
        metadata=meta2,
    )
    ta = RouteTarget(adapter="matrix", channel=None, destination=dest1)
    tb = RouteTarget(adapter="matrix", channel=None, destination=dest2)
    assert delivery_target_identity(ta) == delivery_target_identity(tb)


# ---------------------------------------------------------------------------
# 5. Different metadata produces different identity
# ---------------------------------------------------------------------------


def test_different_metadata_produces_different_identity() -> None:
    dest1 = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"key": "alpha"},
    )
    dest2 = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"key": "beta"},
    )
    ta = RouteTarget(adapter="matrix", channel=None, destination=dest1)
    tb = RouteTarget(adapter="matrix", channel=None, destination=dest2)
    assert delivery_target_identity(ta) != delivery_target_identity(tb)


# ---------------------------------------------------------------------------
# 6. List metadata values
# ---------------------------------------------------------------------------


def test_list_metadata_values() -> None:
    dest = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"tags": ["urgent", "notify"]},
    )
    target = RouteTarget(adapter="matrix", channel=None, destination=dest)
    result = delivery_target_identity(target)
    assert '"tags":["urgent","notify"]' in result


# ---------------------------------------------------------------------------
# 7. Sorted dict keys produce canonical output
# ---------------------------------------------------------------------------


def test_sorted_dict_keys_produce_canonical_output() -> None:
    dest_a = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"b": 1, "a": 2},
    )
    dest_b = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"a": 2, "b": 1},
    )
    ta = RouteTarget(adapter="matrix", channel=None, destination=dest_a)
    tb = RouteTarget(adapter="matrix", channel=None, destination=dest_b)
    assert delivery_target_identity(ta) == delivery_target_identity(tb)


# ---------------------------------------------------------------------------
# 8. Unsupported types raise TypeError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        uuid.uuid4(),
        b"bytes",
        {1, 2, 3},
        complex(1, 2),
        Decimal("1.5"),
    ],
)
def test_unsupported_type_raises_typeerror(bad_value: object) -> None:
    dest = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={"bad": bad_value},
    )
    target = RouteTarget(adapter="matrix", channel=None, destination=dest)
    with pytest.raises(TypeError, match="Unsupported type in target identity"):
        delivery_target_identity(target)


# ---------------------------------------------------------------------------
# 9. Empty metadata produces stable identity
# ---------------------------------------------------------------------------


def test_empty_metadata_produces_stable_id() -> None:
    dest = RouteDestination(
        kind="channel",
        destination_hash=None,
        destination_name=None,
        metadata={},
    )
    target = RouteTarget(adapter="matrix", channel=None, destination=dest)
    id1 = delivery_target_identity(target)
    id2 = delivery_target_identity(target)
    assert id1 == id2
    assert '"metadata":{}' in id1
