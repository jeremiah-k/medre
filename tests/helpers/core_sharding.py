"""Deterministic file-coherent partitioning of the adapter-free test suite.

Shard by collected test counts rather than by filename hash: large test modules
must not make one Windows job several times slower than another.  Assignment
is recomputed from each runner's actual collection, so platform-specific skips
and parameter sets cannot make a shard silently empty or lose a test.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def validate_core_shard(
    *, index: int | None, count: int | None, core_only: bool
) -> bool:
    """Validate explicit zero-based shard coordinates; return whether enabled."""
    if index is None and count is None:
        return False
    if index is None or count is None:
        raise ValueError("--core-shard and --core-shards must be provided together")
    if not core_only:
        raise ValueError("core sharding requires --core-only")
    if count < 1 or not 0 <= index < count:
        raise ValueError("core shard must satisfy 0 <= --core-shard < --core-shards")
    return True


def partition_core_tests(
    items: Sequence[T], *, index: int, count: int
) -> tuple[list[T], list[T]]:
    """Assign every collected item exactly once, keeping each module together.

    Largest-first balancing minimizes the maximum number of tests in any job.
    Ties use module paths and shard indices so input ordering cannot change
    assignments. The returned selected items retain pytest's collection order.
    """
    groups: dict[str, list[T]] = defaultdict(list)
    for item in items:
        nodeid = str(item.nodeid)
        module = nodeid.split("::", 1)[0].replace("\\", "/")
        groups[module].append(item)

    loads = [0] * count
    assigned: dict[str, int] = {}
    for module, group in sorted(
        groups.items(), key=lambda pair: (-len(pair[1]), pair[0])
    ):
        destination = min(range(count), key=lambda shard: (loads[shard], shard))
        assigned[module] = destination
        loads[destination] += len(group)

    selected, deselected = [], []
    for item in items:
        module = str(item.nodeid).split("::", 1)[0].replace("\\", "/")
        (selected if assigned[module] == index else deselected).append(item)
    return selected, deselected
