"""Assertions for normalized delivery attempt/lifecycle evidence in tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _field(receipt: object, name: str) -> Any:
    if isinstance(receipt, Mapping):
        return receipt[name]
    return getattr(receipt, name)


def assert_terminal_failure_pair(
    receipts: Sequence[object],
    *,
    failure_kind: str | None = None,
) -> tuple[object, object]:
    """Assert one failed dispatch attempt followed by dead-letter lifecycle evidence."""
    ordered = sorted(
        receipts, key=lambda receipt: int(_field(receipt, "sequence") or 0)
    )
    assert len(ordered) == 2
    attempt, lifecycle = ordered
    assert (_field(attempt, "receipt_kind"), _field(attempt, "status")) == (
        "attempt",
        "failed",
    )
    assert (_field(lifecycle, "receipt_kind"), _field(lifecycle, "status")) == (
        "lifecycle",
        "dead_lettered",
    )
    assert _field(lifecycle, "attempt_number") == _field(attempt, "attempt_number")
    assert _field(lifecycle, "parent_receipt_id") == _field(attempt, "receipt_id")
    if failure_kind is not None:
        assert _field(attempt, "failure_kind") == failure_kind
        assert _field(lifecycle, "failure_kind") == failure_kind
    return attempt, lifecycle
