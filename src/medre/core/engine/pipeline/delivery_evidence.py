"""Typed delivery-execution evidence shared across pipeline lifecycle boundaries.

A delivery execution may emit two distinct immutable evidence records:

* an ``attempt`` receipt describing the execution/transport generation; and
* a ``lifecycle`` receipt describing a state transition caused by that attempt.

Keeping those values in one validated object prevents orchestration code from
accidentally pairing unrelated receipts or treating a lifecycle transition as a
new dispatch attempt.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import DeliveryFailureKind

__all__ = ["DeliveryExecutionEvidence"]


def _normalized_channel(value: str | None) -> str | None:
    return value or None


@dataclass(frozen=True)
class DeliveryExecutionEvidence:
    """Evidence produced by one delivery execution generation.

    ``attempt_receipt`` is the primary evidence for the execution generation.
    ``authority_receipt`` is an optional lifecycle receipt that supersedes the
    attempt receipt as current lifecycle authority without creating a new
    attempt number (for example, ``dead_lettered`` after a failed attempt).

    The object validates lineage eagerly so the coordinator and outbox manager
    can move it as an opaque value; lifecycle interpretation stays centralized.
    """

    attempt_receipt: DeliveryReceipt | None = None
    authority_receipt: DeliveryReceipt | None = None
    failure_kind: DeliveryFailureKind | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        attempt = self.attempt_receipt
        authority = self.authority_receipt

        if attempt is not None and attempt.receipt_kind != "attempt":
            raise ValueError(
                "attempt_receipt must have receipt_kind='attempt'; "
                f"got {attempt.receipt_kind!r}"
            )
        if authority is not None and authority.receipt_kind != "lifecycle":
            raise ValueError(
                "authority_receipt must have receipt_kind='lifecycle'; "
                f"got {authority.receipt_kind!r}"
            )
        if attempt is None or authority is None:
            return

        if (
            authority.event_id != attempt.event_id
            or authority.delivery_plan_id != attempt.delivery_plan_id
            or authority.target_adapter != attempt.target_adapter
            or _normalized_channel(authority.target_channel)
            != _normalized_channel(attempt.target_channel)
            or authority.outbox_id != attempt.outbox_id
            or authority.attempt_number != attempt.attempt_number
            or authority.parent_receipt_id != attempt.receipt_id
        ):
            raise ValueError(
                "authority_receipt must be linked to the same delivery attempt "
                "as attempt_receipt"
            )

    @property
    def current_receipt(self) -> DeliveryReceipt | None:
        """Return the receipt eligible to become current lifecycle authority."""
        return self.authority_receipt or self.attempt_receipt

    @property
    def primary_receipt(self) -> DeliveryReceipt | None:
        """Return the receipt exposed on the delivery outcome.

        Attempt evidence remains the primary result when it exists. Lifecycle-
        only outcomes (for example suppression) expose their lifecycle receipt.
        """
        return self.attempt_receipt or self.authority_receipt
