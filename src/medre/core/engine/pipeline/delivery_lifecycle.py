"""Delivery lifecycle service - owns retry, dead-letter, and receipt lifecycle decisions.

This module provides :class:`DeliveryLifecycleService`, the central authority
for delivery state transitions within the pipeline.  It owns retry decisions,
retry scheduling, attempt context, retry lineage, dead-letter progression,
supplemental queued->sent receipts, suppression receipt creation, outbox
finalization decisions, and terminal-state determination.

Architecture
~~~~~~~~~~~~
The pipeline uses two shared collaborator services::

    PipelineRunner
      ├── DeliveryLifecycleService   (lifecycle/state decisions)
      └── TargetDeliveryService      (per-target execution)

:class:`PipelineRunner` retains orchestration responsibilities (route
planning, target selection, relation enrichment, runtime coordination,
capacity orchestration, initial outbox creation, lease renewal).  It
delegates lifecycle/state decisions to :class:`DeliveryLifecycleService`
and per-target execution to :class:`TargetDeliveryService`.

:class:`TargetDeliveryService` retains per-target execution responsibilities
(rendering, adapter invocation, rendering evidence, native-ref persistence,
adapter result interpretation, primary single-attempt receipt construction).
It accepts lifecycle-computed values (attempt context, retry fields,
next_retry_at) rather than computing them internally.

State Vocabularies (observed, not enforced)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This section documents the **current observed** state vocabularies and
transitions.  It does **not** introduce a new state machine or enforce
transitions beyond what already exists in the codebase.

DeliveryReceipt statuses
    ``queued``, ``sent``, ``failed``, ``dead_lettered``, ``suppressed``.

Outbox statuses
    ``pending``, ``in_progress``, ``queued``, ``sent``, ``retry_wait``,
    ``dead_lettered``, ``cancelled``, ``abandoned``.

DeliveryOutcome statuses
    ``success``, ``queued``, ``transient_failure``, ``permanent_failure``,
    ``skipped``.

Adapter delivery_status
    ``sent``, ``enqueued``.

Retry representation
    Retry is represented as ``failed`` receipt + ``adapter_transient``
    failure kind + ``next_retry_at`` on the receipt - **not** as a distinct
    receipt status.

Observed transitions
    - Receipt: ``queued`` -> ``sent`` (supplemental, via callback)
    - Receipt: ``failed`` -> ``dead_lettered`` (exhausted retry)
    - Outbox: ``pending`` / ``retry_wait`` / stale ``queued`` / expired
      ``in_progress`` -> ``in_progress`` (lease acquisition)
    - Outbox: ``in_progress`` -> ``queued`` / ``sent`` / ``retry_wait`` /
      ``dead_lettered`` (delivery outcome)
    - Outbox: ``queued`` -> ``sent`` (callback confirmation)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.engine.pipeline.delivery_state import (
    is_terminal_outbox_status as _is_terminal_outbox_status,
)
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DeliveryLifecycleService
# ---------------------------------------------------------------------------


class DeliveryLifecycleService:
    """Owns delivery lifecycle decisions: retry, dead-letter, attempt
    progression, supplemental receipts, suppression receipts, and outbox
    finalization.

    Created by :class:`~medre.core.engine.pipeline.runner.PipelineRunner`
    and shared with
    :class:`~medre.core.engine.pipeline.target_delivery.TargetDeliveryService`
    so that lifecycle logic is centralised in one place.

    Parameters
    ----------
    logger:
        Logger instance.  Defaults to the module logger.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._log: logging.Logger = logger or _logger

    # -- Attempt context ----------------------------------------------------

    @staticmethod
    def compute_attempt_context(
        previous_receipt: DeliveryReceipt | None,
    ) -> tuple[int, str | None]:
        """Compute ``attempt_number`` and ``parent_receipt_id`` from the
        previous receipt.

        Parameters
        ----------
        previous_receipt:
            The receipt from the previous delivery attempt, or ``None``
            for the first attempt.

        Returns
        -------
        tuple[int, str | None]
            ``(attempt_number, parent_receipt_id)``.  For the first
            attempt: ``(1, None)``.
        """
        if previous_receipt is not None:
            return (
                previous_receipt.attempt_number + 1,
                previous_receipt.receipt_id,
            )
        return 1, None

    # -- Retry field extraction ---------------------------------------------

    @staticmethod
    def extract_retry_fields(plan: DeliveryPlan) -> dict[str, Any]:
        """Extract retry policy fields for receipt construction.

        Parameters
        ----------
        plan:
            The delivery plan whose retry policy (if any) is extracted.

        Returns
        -------
        dict[str, Any]
            Keys: ``retry_max_attempts``, ``retry_backoff_base``,
            ``retry_max_delay``, ``retry_jitter``.  Values are ``None``
            when no retry policy is configured.
        """
        rp = plan.retry_policy
        return {
            "retry_max_attempts": rp.max_attempts if rp else None,
            "retry_backoff_base": rp.backoff_base if rp else None,
            "retry_max_delay": rp.max_delay_seconds if rp else None,
            "retry_jitter": rp.jitter if rp else None,
        }

    # -- Failure classification ---------------------------------------------

    @staticmethod
    def classify_failure(
        error: Exception,
        *,
        adapter_registered: bool = True,
    ) -> DeliveryFailureKind:
        """Classify a delivery failure using :class:`RetryExecutor`.

        Thin passthrough to :meth:`RetryExecutor.classify_failure` so
        callers go through the lifecycle service rather than reaching
        directly for the planning-layer utility.

        Parameters
        ----------
        error:
            The exception that caused the failure.
        adapter_registered:
            Whether the target adapter was found in the pipeline config.

        Returns
        -------
        DeliveryFailureKind
        """
        return RetryExecutor.classify_failure(
            error, adapter_registered=adapter_registered
        )

    # -- Retryable / permanent classification -------------------------------

    @staticmethod
    def is_retryable(failure_kind: DeliveryFailureKind) -> bool:
        """Return ``True`` if *failure_kind* is retryable.

        Parameters
        ----------
        failure_kind:
            The classified delivery failure kind.

        Returns
        -------
        bool
        """
        return failure_kind.is_retryable

    # -- Dead-letter determination ------------------------------------------

    @staticmethod
    def should_dead_letter(
        status: str,
        plan: DeliveryPlan,
        attempt_number: int,
    ) -> bool:
        """Determine if retries are exhausted and a dead-letter receipt
        should be created.

        Parameters
        ----------
        status:
            The primary receipt status (e.g. ``"failed"``).
        plan:
            The delivery plan (may have a ``retry_policy``).
        attempt_number:
            The 1-indexed attempt number that just failed.

        Returns
        -------
        bool
            ``True`` when the failure is terminal and retries are
            exhausted.
        """
        return (
            status == "failed"
            and plan.retry_policy is not None
            and RetryExecutor(plan.retry_policy).is_exhausted(attempt_number)
        )

    # -- Next retry time computation ----------------------------------------

    @staticmethod
    def compute_next_retry_at(
        status: str,
        failure_kind: DeliveryFailureKind | None,
        plan: DeliveryPlan,
        attempt_number: int,
        now: datetime,
    ) -> datetime | None:
        """Compute ``next_retry_at`` for retryable transient failures.

        Parameters
        ----------
        status:
            The primary receipt status.
        failure_kind:
            The classified failure kind enum, or ``None``.
        plan:
            The delivery plan with optional retry policy.
        attempt_number:
            The 1-indexed attempt number.
        now:
            Persistence-time timestamp used as the base for backoff.

        Returns
        -------
        datetime | None
            The computed next-retry timestamp, or ``None`` when the
            failure is not retryable or no retry policy exists.
        """
        if (
            status == "failed"
            and failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
            and plan.retry_policy is not None
        ):
            executor = RetryExecutor(plan.retry_policy)
            if not executor.is_exhausted(attempt_number):
                backoff = executor.compute_backoff(attempt_number)
                return now + backoff
        return None

    # -- Terminal-state determination ----------------------------------------

    @staticmethod
    def is_terminal_outbox_status(status: str) -> bool:
        """Return ``True`` if *status* is a terminal outbox status.

        Terminal statuses: ``sent``, ``dead_lettered``, ``cancelled``,
        ``abandoned``.

        Delegates to
        :func:`~medre.core.engine.pipeline.delivery_state.is_terminal_outbox_status`.

        Parameters
        ----------
        status:
            The outbox item status to check.

        Returns
        -------
        bool
        """
        return _is_terminal_outbox_status(status)

    # -- Dead-letter receipt creation ---------------------------------------

    async def build_and_persist_dead_letter_receipt(
        self,
        storage: StorageBackend,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        previous_receipt_id: str,
        attempt_number: int,
        error: str | None,
        source: str,
        replay_run_id: str | None,
        target_channel: str | None,
        plan: DeliveryPlan,
    ) -> DeliveryReceipt:
        """Build and persist a dead-letter receipt after the primary
        failed receipt.

        Uses :meth:`RetryExecutor.build_dead_letter_receipt` for
        construction and appends to *storage*.

        Parameters
        ----------
        storage:
            The storage backend for receipt persistence.
        event_id:
            The canonical event ID.
        delivery_plan_id:
            ID of the delivery plan.
        target_adapter:
            Name of the target adapter.
        previous_receipt_id:
            Receipt ID of the primary failed receipt.
        attempt_number:
            The attempt number of the primary receipt (the dead-letter
            gets ``attempt_number + 1``).
        error:
            Human-readable error from the primary failure.
        source:
            Delivery origin (``"live"``, ``"retry"``, ``"replay"``).
        replay_run_id:
            Replay run identifier, if applicable.
        target_channel:
            Channel on the target adapter.
        plan:
            The delivery plan whose retry policy governs the dead-letter.

        Returns
        -------
        DeliveryReceipt
            The persisted dead-letter receipt.
        """
        if plan.retry_policy is None:
            raise RuntimeError(
                "build_and_persist_dead_letter_receipt requires a plan with "
                "a retry_policy; callers must guard with should_dead_letter()"
            )
        executor = RetryExecutor(plan.retry_policy)
        dead_receipt = executor.build_dead_letter_receipt(
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            previous_receipt_id=previous_receipt_id,
            attempt_number=attempt_number + 1,
            error=error or "Retry exhausted",
            source=source,
            replay_run_id=replay_run_id,
            target_channel=target_channel,
        )
        await storage.append_receipt(dead_receipt)
        return dead_receipt

    # -- Suppression receipt creation ---------------------------------------

    async def build_and_persist_suppression_receipt(
        self,
        storage: StorageBackend,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        route_id: str,
        failure_kind: DeliveryFailureKind,
        error: str,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        """Build and persist a lightweight suppression/rejection receipt.

        Creates a ``status="suppressed"`` :class:`DeliveryReceipt` with
        ``attempt_number=1``, no ``next_retry_at``, and the given
        *failure_kind*.

        Parameters
        ----------
        storage:
            The storage backend for receipt persistence.
        event_id:
            The canonical event ID (must already be persisted).
        delivery_plan_id:
            ID of the delivery plan.
        target_adapter:
            Name of the target adapter.
        target_channel:
            Channel on the target adapter, if applicable.
        route_id:
            ID of the route that triggered this delivery.
        failure_kind:
            The :class:`DeliveryFailureKind` for the suppression reason.
        error:
            Human-readable error/reason string.
        source:
            Origin of delivery (``"live"``, ``"retry"``, ``"replay"``).
        replay_run_id:
            Replay run identifier, if applicable.

        Returns
        -------
        DeliveryReceipt
            The persisted suppression receipt.
        """
        now = datetime.now(tz=timezone.utc)
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            status="suppressed",
            error=error,
            failure_kind=failure_kind.value,
            next_retry_at=None,
            created_at=now,
            attempt_number=1,
            parent_receipt_id=None,
            source=source,
            replay_run_id=replay_run_id,
        )
        await storage.append_receipt(receipt)
        return receipt

    # -- Supplemental queued->sent receipt -----------------------------------

    async def append_queued_to_sent_receipt(
        self,
        storage: StorageBackend,
        record: OutboundNativeRefRecord,
        now: datetime,
    ) -> None:
        """Append a supplemental ``status="sent"`` receipt for a
        queue-based delivery that transitioned from enqueued to sent.

        **Correlation strategy (priority order)**:

        1. **Exact ``delivery_plan_id`` match** (deterministic).  When
           *record.delivery_plan_id* is present, only queued receipts
           with the same ``delivery_plan_id`` are considered.  If no
           exact match is found, the method logs and returns — it does
           **not** fall back to the heuristic, preventing wrong-plan
           attachment.

        2. **Legacy heuristic** (fallback).  When *record.delivery_plan_id*
           is ``None``, the method falls back to filtering by
           event_id + adapter + channel and selecting the most recent
           queued receipt.  This path is retained for adapters that do
           not yet propagate ``delivery_plan_id``.  Ambiguous cases
           (multiple candidates, no channel) return silently.

        After correlation, the method appends a new immutable receipt
        with ``status="sent"`` and the real ``adapter_message_id``, and
        transitions the matching outbox item from ``queued`` -> ``sent``.

        If no matching ``"queued"`` receipt is found (e.g. non-queued
        adapter or replay context), the method returns silently.

        Parameters
        ----------
        storage:
            The storage backend for receipt/outbox persistence.
        record:
            The outbound native reference record from the adapter.
        now:
            Timestamp for the new receipt.
        """
        try:
            existing = await storage.list_receipts_for_event(record.event_id)
        except Exception:
            self._log.exception(
                "Failed to list receipts for supplemental queued->sent: "
                "event_id=%s adapter=%s native_channel_id=%s",
                record.event_id,
                record.adapter,
                record.native_channel_id,
            )
            return

        # Find queued receipts targeting this adapter.
        candidates: list[DeliveryReceipt] = [
            r
            for r in existing
            if r.status == "queued" and r.target_adapter == record.adapter
        ]

        if not candidates:
            return

        queued_receipt: DeliveryReceipt | None = None

        if record.delivery_plan_id is not None:
            # --- Deterministic correlation by delivery_plan_id ---
            plan_matches = [
                r for r in candidates if r.delivery_plan_id == record.delivery_plan_id
            ]

            if not plan_matches:
                self._log.debug(
                    "No queued receipt matched delivery_plan_id=%s for "
                    "event_id=%s adapter=%s; skipping supplemental receipt "
                    "(deterministic correlation — no heuristic fallback)",
                    record.delivery_plan_id,
                    record.event_id,
                    record.adapter,
                )
                return

            # Narrow by channel if the record carries one.
            if record.native_channel_id is not None:
                channel_matches = [
                    r
                    for r in plan_matches
                    if r.target_channel == record.native_channel_id
                ]
                if not channel_matches:
                    self._log.debug(
                        "No queued receipt matched delivery_plan_id=%s + "
                        "channel=%s for event_id=%s adapter=%s; "
                        "skipping supplemental receipt",
                        record.delivery_plan_id,
                        record.native_channel_id,
                        record.event_id,
                        record.adapter,
                    )
                    return
                # Most recent (last in append-order) wins for retries
                # under the same plan.
                queued_receipt = channel_matches[-1]
            elif len(plan_matches) == 1:
                queued_receipt = plan_matches[0]
            else:
                self._log.debug(
                    "Ambiguous queued receipt correlation: %d plan_id=%s "
                    "candidates for event_id=%s adapter=%s with no "
                    "channel; skipping supplemental receipt",
                    len(plan_matches),
                    record.delivery_plan_id,
                    record.event_id,
                    record.adapter,
                )
                return
        else:
            # --- Legacy heuristic: no delivery_plan_id on record ---
            if record.native_channel_id is not None:
                channel_matches = [
                    r
                    for r in candidates
                    if r.target_channel == record.native_channel_id
                ]
                if not channel_matches:
                    self._log.debug(
                        "No queued receipt matched channel %s for "
                        "event_id=%s adapter=%s; skipping supplemental receipt",
                        record.native_channel_id,
                        record.event_id,
                        record.adapter,
                    )
                    return
                # Most recent (last in list) wins - handles retries.
                queued_receipt = channel_matches[-1]
            else:
                # No channel on record - disambiguate by count.
                if len(candidates) == 1:
                    queued_receipt = candidates[0]
                else:
                    self._log.debug(
                        "Ambiguous queued receipt correlation: %d candidates "
                        "for event_id=%s adapter=%s with no channel "
                        "and no delivery_plan_id; skipping supplemental receipt",
                        len(candidates),
                        record.event_id,
                        record.adapter,
                    )
                    return

        if queued_receipt is None:
            self._log.warning(
                "Logic error: queued_receipt is None after correlation "
                "for event_id=%s adapter=%s; skipping supplemental receipt",
                record.event_id,
                record.adapter,
            )
            return

        supplemental = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=record.event_id,
            delivery_plan_id=queued_receipt.delivery_plan_id,
            target_adapter=record.adapter,
            target_channel=record.native_channel_id or queued_receipt.target_channel,
            route_id=queued_receipt.route_id,
            status="sent",
            error=None,
            failure_kind=None,
            adapter_message_id=record.native_message_id,
            next_retry_at=None,
            created_at=now,
            attempt_number=queued_receipt.attempt_number,
            parent_receipt_id=queued_receipt.receipt_id,
            source=queued_receipt.source,
            replay_run_id=queued_receipt.replay_run_id,
            retry_max_attempts=queued_receipt.retry_max_attempts,
            retry_backoff_base=queued_receipt.retry_backoff_base,
            retry_max_delay=queued_receipt.retry_max_delay,
            retry_jitter=queued_receipt.retry_jitter,
            rendering_evidence=queued_receipt.rendering_evidence,
        )
        await storage.append_receipt(supplemental)

        # Transition the matching outbox item from queued -> sent.
        # The item may still be in_progress if the callback fires before
        # _deliver_one() marks the outbox row as queued.  Prefer queued
        # status over in_progress so that a fully-queued row is always
        # selected first.
        try:
            outbox_item = await storage.get_outbox_item_for_delivery(
                event_id=record.event_id,
                delivery_plan_id=queued_receipt.delivery_plan_id,
                target_adapter=record.adapter,
                target_channel=queued_receipt.target_channel,
                status="queued",
            )
            if outbox_item is None:
                outbox_item = await storage.get_outbox_item_for_delivery(
                    event_id=record.event_id,
                    delivery_plan_id=queued_receipt.delivery_plan_id,
                    target_adapter=record.adapter,
                    target_channel=queued_receipt.target_channel,
                    status="in_progress",
                )
            if outbox_item is not None:
                await storage.mark_outbox_sent(
                    outbox_item.outbox_id,
                    receipt_id=supplemental.receipt_id,
                    attempt_number=supplemental.attempt_number,
                )
        except Exception:
            self._log.exception(
                "Failed to transition outbox queued->sent: event_id=%s adapter=%s",
                record.event_id,
                record.adapter,
            )

    # -- Outbox finalization ------------------------------------------------

    async def finalize_outbox_outcome(
        self,
        storage: StorageBackend,
        outbox_id: str | None,
        outbox_created: bool,
        receipt: DeliveryReceipt | None,
        failure_kind_val: DeliveryFailureKind | None,
        error: str | None,
        retry_policy: RetryPolicy | None,
    ) -> None:
        """Update the outbox item status based on the delivery outcome.

        Handles the queued / sent / retry_wait / dead_lettered state
        transitions.  Silently skips when no outbox item was created.

        Parameters
        ----------
        storage:
            The storage backend for outbox persistence.
        outbox_id:
            ID of the outbox item, or ``None`` if not created.
        outbox_created:
            Whether the outbox item was successfully created.
        receipt:
            The delivery receipt, if one was produced.
        failure_kind_val:
            The classified failure kind, if the delivery failed.
        error:
            Human-readable error description, if applicable.
        retry_policy:
            The retry policy governing backoff, if any.
        """
        if outbox_id is None or not outbox_created:
            return
        try:
            if receipt is not None and receipt.status != "failed":
                receipt_status = receipt.status
                if receipt_status == "queued":
                    await storage.mark_outbox_queued(
                        outbox_id,
                        receipt_id=receipt.receipt_id,
                        attempt_number=receipt.attempt_number,
                    )
                else:
                    await storage.mark_outbox_sent(
                        outbox_id,
                        receipt_id=receipt.receipt_id,
                        attempt_number=receipt.attempt_number,
                    )
            elif failure_kind_val is not None:
                receipt_ref_id: str | None = (
                    receipt.receipt_id if receipt is not None else None
                )
                attempt: int | None = (
                    receipt.attempt_number if receipt is not None else None
                )
                error_summary: str | None = error[:512] if error else None
                if failure_kind_val.is_retryable:
                    if retry_policy is None:
                        # No retry policy - treat as terminal.
                        await storage.mark_outbox_dead_lettered(
                            outbox_id,
                            receipt_id=receipt_ref_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=error_summary,
                        )
                    elif receipt is not None and receipt.next_retry_at is None:
                        # Receipt exists but next_retry_at is None despite
                        # having a retry policy and a retryable failure kind.
                        # compute_next_retry_at returned None, meaning retries
                        # are exhausted.  Mark outbox as dead_lettered rather
                        # than retry_wait to align with receipt-level state.
                        await storage.mark_outbox_dead_lettered(
                            outbox_id,
                            receipt_id=receipt_ref_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=error_summary,
                        )
                    elif receipt is not None and receipt.next_retry_at is not None:
                        # Receipt has a persisted next_retry_at - reuse it
                        # for outbox retry_wait rather than recomputing.
                        next_attempt_at = receipt.next_retry_at.isoformat()
                        await storage.mark_outbox_retry_wait(
                            outbox_id,
                            next_attempt_at=next_attempt_at,
                            receipt_id=receipt_ref_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=error_summary,
                            attempt_number=attempt or 1,
                        )
                    else:
                        # Defensive fallback: no persisted receipt to
                        # consult.  Compute backoff from scratch.
                        retry_attempt = attempt or 1
                        backoff_duration = RetryExecutor(retry_policy).compute_backoff(
                            retry_attempt
                        )
                        next_attempt_at = (
                            datetime.now(timezone.utc) + backoff_duration
                        ).isoformat()
                        await storage.mark_outbox_retry_wait(
                            outbox_id,
                            next_attempt_at=next_attempt_at,
                            receipt_id=receipt_ref_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=error_summary,
                            attempt_number=retry_attempt,
                        )
                else:
                    await storage.mark_outbox_dead_lettered(
                        outbox_id,
                        receipt_id=receipt_ref_id,
                        failure_kind=failure_kind_val.value,
                        error_summary=error_summary,
                    )
        except Exception:
            self._log.exception(
                "Failed to update outbox %s after delivery",
                outbox_id,
            )
