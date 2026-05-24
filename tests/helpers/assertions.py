"""Shared assertion helpers for bridge and delivery tests.

Provides focused assertion functions that check receipt status, target
adapters, and accounting counters without obscuring test intent.
"""

from __future__ import annotations

from typing import Any

from medre.core.supervision.accounting import RuntimeAccounting


def assert_receipt_status(
    receipts: Any,
    expected_status: str = "sent",
) -> None:
    """Assert every receipt in *receipts* has the expected *status*."""
    for receipt in receipts:
        assert (
            receipt.status == expected_status
        ), f"Expected status {expected_status!r}, got {receipt.status!r}"


def assert_receipt_targets(
    receipts: Any,
    expected_targets: set[str],
) -> None:
    """Assert the set of ``target_adapter`` values matches *expected_targets*."""
    actual = {receipt.target_adapter for receipt in receipts}
    assert (
        actual == expected_targets
    ), f"Expected target adapters {expected_targets!r}, got {actual!r}"


def snap_value(accounting: RuntimeAccounting, key: str) -> int:
    """Read a single counter from a RuntimeAccounting snapshot."""
    return accounting.snapshot()[key]
