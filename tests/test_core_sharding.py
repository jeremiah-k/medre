"""Contract tests for the deterministic Windows core-only test partition."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.helpers.core_sharding import partition_core_tests, validate_core_shard


@dataclass(frozen=True)
class _Item:
    nodeid: str


@pytest.mark.parametrize(
    ("index", "count", "core_only", "error"),
    [
        (0, None, True, "together"),
        (None, 4, True, "together"),
        (0, 4, False, "requires --core-only"),
        (-1, 4, True, "0 <= --core-shard"),
        (4, 4, True, "0 <= --core-shard"),
        (0, 0, True, "0 <= --core-shard"),
    ],
)
def test_rejects_invalid_shard_configuration(index, count, core_only, error):
    with pytest.raises(ValueError, match=error):
        validate_core_shard(index=index, count=count, core_only=core_only)


def test_unsharded_configuration_is_valid():
    assert not validate_core_shard(index=None, count=None, core_only=False)
    assert validate_core_shard(index=0, count=8, core_only=True)


def test_partition_is_disjoint_complete_and_balanced():
    items = [
        _Item(f"tests/test_{module}.py::test_{case}")
        for module, count in enumerate([100, 75, 40, 39, 33, 32, 26, 25, 12, 8])
        for case in range(count)
    ]
    shards = [partition_core_tests(items, index=i, count=4) for i in range(4)]
    selected_ids = [{item.nodeid for item in selected} for selected, _ in shards]
    assert set.union(*selected_ids) == {item.nodeid for item in items}
    assert sum(len(s) for s in selected_ids) == len(items)
    assert all(
        not (a & b) for i, a in enumerate(selected_ids) for b in selected_ids[i + 1 :]
    )
    assert max(map(len, selected_ids)) - min(map(len, selected_ids)) <= 25
    assert all(
        len(selected) + len(deselected) == len(items) for selected, deselected in shards
    )


def test_module_cohesion_and_input_order_independence():
    items = [
        _Item(f"tests/test_{module}.py::test_{case}")
        for module in "abcde"
        for case in range(8)
    ]
    for i in range(3):
        first, _ = partition_core_tests(items, index=i, count=3)
        reversed_selected, _ = partition_core_tests(
            list(reversed(items)), index=i, count=3
        )
        assert {x.nodeid for x in first} == {x.nodeid for x in reversed_selected}
        for module in "abcde":
            module_ids = {x.nodeid for x in items if f"test_{module}.py" in x.nodeid}
            selected_ids = {x.nodeid for x in first}
            assert not (module_ids & selected_ids) or module_ids <= selected_ids


def test_normalizes_windows_collection_paths():
    items = [_Item("tests\\test_a.py::test_one"), _Item("tests/test_a.py::test_two")]
    for shard in range(2):
        selected, _ = partition_core_tests(items, index=shard, count=2)
        assert len(selected) in {0, 2}
