"""Read-only binding assessment for one native observation and durable attempt.

The ledger proves only which MEDRE attempt a supplied statement names. It
cannot authenticate a provider, an independent recipient, or a compatibility
pin and never grants permission to replay or mutate a delivery generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from medre.core.evidence.native_evidence import NativeEvidenceObservation
from medre.core.storage.backend import StorageBackend

NativeLedgerBindingState = Literal["current", "historical", "unbound"]
NativeLedgerBindingReason = Literal[
    "exact",
    "attempt_superseded",
    "generation_ambiguous",
    "row_changed",
    "row_missing",
    "identity_mismatch",
    "attempt_unrecognized",
    "receipt_missing",
    "receipt_mismatch",
    "native_reference_mismatch",
]


@dataclass(frozen=True, slots=True)
class NativeLedgerBindingAssessment:
    """A metadata-only assessment, never a decision or execution token."""

    state: NativeLedgerBindingState
    reason: NativeLedgerBindingReason
    evidence_fingerprint: str


async def assess_native_evidence_ledger_binding(
    storage: StorageBackend,
    observation: NativeEvidenceObservation,
) -> NativeLedgerBindingAssessment:
    """Compare an observation with its exact outbox and immutable receipts.

    The outbox and receipt reads are intentionally separate: the result is a
    diagnostic point-in-time observation, not an atomic compare-and-swap. A
    writer must repeat every relevant check under its own storage authority.
    Backend errors propagate; they never become a negative proof or ``unbound``.
    """
    if not isinstance(observation, NativeEvidenceObservation):
        raise TypeError("observation must be a NativeEvidenceObservation")

    def assessed(
        state: NativeLedgerBindingState, reason: NativeLedgerBindingReason
    ) -> NativeLedgerBindingAssessment:
        return NativeLedgerBindingAssessment(
            state=state,
            reason=reason,
            evidence_fingerprint=observation.evidence_fingerprint,
        )

    item = await storage.get_outbox_item(observation.outbox_id, metadata_only=True)
    if item is None:
        return assessed("unbound", "row_missing")
    if (
        item.event_id != observation.event_id
        or item.delivery_plan_id != observation.delivery_plan_id
        or item.target_adapter != observation.target_adapter
        or (item.target_channel or None) != observation.identity.target_channel
    ):
        return assessed("unbound", "identity_mismatch")
    if (
        observation.attempt_number > item.attempt_number
        and observation.attempt_number != item.active_attempt
    ):
        return assessed("unbound", "attempt_unrecognized")

    # Positive native claims require a durable receipt or native reference to
    # correlate. A caller cannot turn a matching row alone into a proven send.
    if (
        observation.result == "accepted"
        and observation.receipt_id is None
        and observation.native_message_id is None
    ):
        return assessed("unbound", "receipt_missing")

    # An observation without a receipt is allowed for the explicit absence
    # statement or a pre-handoff refusal. No receipt is invented to bind it.
    if observation.receipt_id is not None or observation.native_message_id is not None:
        receipts = await storage.list_receipts_for_outbox(observation.outbox_id)
        candidates = [
            receipt
            for receipt in receipts
            if receipt.attempt_number == observation.attempt_number
            and receipt.event_id == observation.event_id
            and receipt.delivery_plan_id == observation.delivery_plan_id
            and receipt.target_adapter == observation.target_adapter
            and (receipt.target_channel or None) == observation.identity.target_channel
            and receipt.receipt_kind == "attempt"
        ]
        if not candidates:
            return assessed("unbound", "receipt_missing")
        if observation.receipt_id is not None:
            candidates = [
                receipt
                for receipt in candidates
                if receipt.receipt_id == observation.receipt_id
            ]
            if not candidates:
                return assessed("unbound", "receipt_mismatch")
        if observation.native_message_id is not None and not any(
            receipt.adapter_message_id == observation.native_message_id
            for receipt in candidates
        ):
            return assessed("unbound", "native_reference_mismatch")

    # A replay may allocate an entirely new outbox row for the same exact
    # identity. Comparing only this row's attempt number would incorrectly
    # classify an earlier completed generation as current.
    history = await storage.list_outbox_items_for_delivery(observation.identity)
    owning_rows = [
        entry for entry in history if entry.outbox_id == observation.outbox_id
    ]
    if not owning_rows:
        return assessed("unbound", "row_missing")
    if len(owning_rows) != 1:
        return assessed("unbound", "generation_ambiguous")
    owning = owning_rows[0]
    if (
        owning.attempt_number != item.attempt_number
        or owning.active_attempt != item.active_attempt
        or owning.updated_at != item.updated_at
    ):
        # Two separate read projections disagree about the same row. The
        # diagnostic must not present their combination as a current fact.
        return assessed("unbound", "generation_ambiguous")
    observed_generation = observation.attempt_number
    competing_generations = [
        max(entry.attempt_number, entry.active_attempt or 0)
        for entry in history
        if entry.outbox_id != observation.outbox_id
    ]
    if any(generation > observed_generation for generation in competing_generations):
        return assessed("historical", "attempt_superseded")
    if any(generation == observed_generation for generation in competing_generations):
        return assessed("unbound", "generation_ambiguous")
    if observation.attempt_number < item.attempt_number or (
        item.active_attempt is not None
        and observation.attempt_number < item.active_attempt
    ):
        return assessed("historical", "attempt_superseded")
    if (
        observation.observed_row_updated_at is not None
        and observation.observed_row_updated_at != item.updated_at
    ):
        return assessed("historical", "row_changed")
    return assessed("current", "exact")
