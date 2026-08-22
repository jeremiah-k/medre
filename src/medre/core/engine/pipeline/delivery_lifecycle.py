"""Delivery lifecycle service - owns retry, dead-letter, and receipt lifecycle decisions.

This module provides :class:`DeliveryLifecycleService`, the central authority
for delivery state transitions within the pipeline.  It owns retry decisions,
retry scheduling, attempt context, retry lineage, dead-letter progression,
atomic queued->sent finalization, suppression receipt creation, outbox
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

``outbox_id`` + ``attempt_number`` correlation
    ``outbox_id`` and ``attempt_number`` are the primary internal
    correlation keys for exact queued→sent receipt matching and
    stale-callback protection.  ``delivery_plan_id`` is a validation
    field checked against the outbox item, but is NOT the correlation
    selector.  Callbacks missing ``outbox_id`` or ``attempt_number``
    are hard-rejected.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.engine.pipeline.delivery_state import (
    is_terminal_outbox_status as _is_terminal_outbox_status,
)
from medre.core.engine.pipeline.delivery_state import (
    is_valid_queued_to_sent_transition as _is_valid_queued_to_sent_transition,
)
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events.canonical import DeliveryReceipt, NativeMessageRef
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.storage.backend import DeliveryOutboxItem

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# Failed receipt evidence without a canonical taxonomy value is unsafe to
# retry.  Treat it as a permanent adapter failure so claim reconciliation can
# terminate the row instead of repeating the same invariant violation forever.
_MALFORMED_RETRY_EVIDENCE_KIND = DeliveryFailureKind.ADAPTER_PERMANENT


# ---------------------------------------------------------------------------
# Delivery lifecycle storage contract
# ---------------------------------------------------------------------------


@runtime_checkable
class DeliveryLifecycleStorage(Protocol):
    """Storage surface required by :class:`DeliveryLifecycleService`."""

    async def append_receipt(self, receipt: DeliveryReceipt) -> None: ...

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]: ...

    async def list_receipts_for_plan(
        self,
        delivery_plan_id: str,
        target_adapter: str,
    ) -> list[DeliveryReceipt]: ...

    async def finalize_queued_delivery(
        self,
        native_ref: NativeMessageRef,
        receipt: DeliveryReceipt,
        *,
        outbox_id: str,
        attempt_number: int,
    ) -> bool: ...

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None: ...

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None: ...

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None: ...

    async def mark_outbox_retry_wait(
        self,
        outbox_id: str,
        next_attempt_at: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
    ) -> None: ...

    async def mark_outbox_dead_lettered(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
    ) -> None: ...

    async def mark_outbox_abandoned(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Retry-attempt reconciliation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryAttemptFinalization:
    """Durable lifecycle result after reconciling one retry exception.

    ``RetryWorker`` owns operational counters and runtime events, but it must
    not infer durable delivery state from receipt rows.  This value is the
    lifecycle authority's compact answer after it has inspected the current
    attempt's evidence and committed the corresponding outbox transition.
    """

    outcome: Literal["accepted", "suppressed", "retry_wait", "dead_lettered"]
    receipt_id: str | None
    failure_kind: str | None
    attempt_number: int
    next_retry_at: datetime | None = None


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
        storage: DeliveryLifecycleStorage,
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
        outbox_id: str | None,
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
        outbox_id:
            Durable outbox correlation key for the delivery attempt, when
            this delivery is outbox-backed.
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
            outbox_id=outbox_id,
        )
        await storage.append_receipt(dead_receipt)
        return dead_receipt

    # -- Suppression receipt creation ---------------------------------------

    async def build_and_persist_suppression_receipt(
        self,
        storage: DeliveryLifecycleStorage,
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
        receipt = build_delivery_receipt(
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            status="suppressed",
            error=error,
            failure_kind=failure_kind.value,
            source=source,
            replay_run_id=replay_run_id,
        )
        await storage.append_receipt(receipt)
        return receipt

    # -- Source-aware candidate selection ------------------------------------

    def _select_source_preferred_candidate(
        self,
        candidates: list[DeliveryReceipt],
        record: OutboundNativeRefRecord,
    ) -> DeliveryReceipt | None:
        """Select the best queued receipt candidate with source awareness.

        When multiple candidates match the same
        ``(delivery_plan_id, adapter, channel)``, prefer non-replay
        (``"live"`` / ``"retry"``) candidates over ``"replay"`` candidates.
        This prevents a live callback from silently linking to a replay queued
        receipt when a live candidate exists.

        Among the preferred source group the most-recent (last in
        append-order) candidate wins, preserving the existing retry-lineage
        behaviour.

        If only replay candidates are available the method returns ``None``
        after logging an operator-visible warning.  ``OutboundNativeRefRecord``
        carries no trusted ``source`` / ``replay_run_id`` provenance, so
        replay-only queued receipts cannot be safely used for callback
        correlation without risking live recovery state mutation.

        Parameters
        ----------
        candidates:
            Non-empty list of matching queued receipts (already filtered by
            plan_id and optionally channel).
        record:
            The outbound native reference record from the adapter callback.
            Used for log context only.

        Returns
        -------
        DeliveryReceipt | None
            The selected receipt, or ``None`` if no trustworthy candidate is
            available (empty list or replay-only candidates).
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            candidate = candidates[0]
            if candidate.source == "replay":
                self._log.warning(
                    "Supplemental queued→sent correlation: only replay-sourced "
                    "queued receipt found for delivery_plan_id=%s event_id=%s "
                    "adapter=%s channel=%s; skipping replay candidate %s "
                    "(source=%s, replay_run_id=%s). OutboundNativeRefRecord "
                    "carries no trusted replay provenance — correlation "
                    "skipped to prevent live recovery state mutation.",
                    record.delivery_plan_id,
                    record.event_id,
                    record.adapter,
                    candidate.target_channel,
                    candidate.receipt_id,
                    candidate.source,
                    candidate.replay_run_id,
                )
                return None
            return candidate

        live_candidates = [r for r in candidates if r.source != "replay"]
        replay_candidates = [r for r in candidates if r.source == "replay"]

        if live_candidates:
            # Prefer the latest non-replay candidate.
            return live_candidates[-1]

        # Only replay-sourced candidates — skip with warning.
        self._log.warning(
            "Supplemental queued→sent correlation: only replay-sourced "
            "queued receipts found for delivery_plan_id=%s event_id=%s "
            "adapter=%s (%d candidates); skipping all replay candidates. "
            "OutboundNativeRefRecord carries no trusted replay provenance — "
            "correlation skipped to prevent live recovery state mutation.",
            record.delivery_plan_id,
            record.event_id,
            record.adapter,
            len(replay_candidates),
        )
        return None

    # -- Atomic queued->sent finalization ------------------------------------

    async def finalize_queued_delivery(
        self,
        storage: DeliveryLifecycleStorage,
        record: OutboundNativeRefRecord,
        now: datetime,
    ) -> None:
        """Finalize a queue-backed delivery that transitioned from
        ``enqueued`` to ``sent``.

        **Correlation strategy**:

        **Exact ``outbox_id`` correlation** (required).
        The callback MUST carry ``outbox_id``.  When present, the method
        looks up the outbox item directly and validates it:

        - The outbox item must exist and its ``status`` must be
          ``"queued"`` or ``"in_progress"``.  If the status is anything
          else (terminal, stale-reclaimed), the callback is rejected as
          stale and the method logs a warning and returns.
        - The outbox item's ``event_id`` must match *record.event_id*.
        - The outbox item's ``attempt_number`` must match the queued
          receipt's ``attempt_number``.  A mismatch indicates a stale
          callback from a prior attempt.
        - The queued receipt is found by exact ``outbox_id`` match among
          queued receipts after the outbox row has been validated.
          ``delivery_plan_id`` and ``native_channel_id`` are validation
          metadata only.

        Callbacks without ``outbox_id`` are hard-rejected — there is no
        ``delivery_plan_id``-only fallback.  All queue-based adapters
        must propagate ``outbox_id`` through their queues for exact
        correlation.

        **Stale-callback protection**: when the outbox item has been
        reclaimed by a retry (status is no longer ``queued`` or
        ``in_progress``), the callback is rejected.  This prevents an old
        in-memory queue callback from finalizing a newly retried outbox
        attempt.

        After correlation, the method validates that the selected queued
        receipt can transition to ``sent`` using the delivery_state
        transition helper.  If the status is invalid, the method logs
        and returns.

        Finally, the method builds the outbound native ref and immutable sent
        receipt, then asks storage to commit those facts together with the
        exact outbox ``queued|in_progress -> sent`` transition in one
        transaction. The storage transaction re-checks outbox ID, attempt
        number, and status so a concurrent reclaim cannot partially commit.

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

        queued_receipt: DeliveryReceipt | None = None
        # Track the validated outbox item for exact transition below.
        validated_outbox: DeliveryOutboxItem | None = None

        if record.outbox_id is not None:
            # --- Exact outbox_id correlation (required) ---
            # Look up the outbox item directly for exact, stale-safe matching.
            outbox_item = await storage.get_outbox_item(record.outbox_id)
            if outbox_item is None:
                self._log.warning(
                    "Stale callback: outbox_id=%s not found for "
                    "event_id=%s adapter=%s; skipping supplemental receipt",
                    record.outbox_id,
                    record.event_id,
                    record.adapter,
                )
                return

            # Stale-callback protection: only accept callbacks for outbox
            # items that are still in a queued or in-progress state.
            if outbox_item.status not in ("queued", "in_progress"):
                self._log.warning(
                    "Stale callback rejected: outbox_id=%s has status=%s "
                    "(expected queued or in_progress) for event_id=%s "
                    "adapter=%s; the outbox item was likely reclaimed by "
                    "a retry attempt",
                    record.outbox_id,
                    outbox_item.status,
                    record.event_id,
                    record.adapter,
                )
                return

            # Validate event_id matches (prevents cross-event corruption).
            if outbox_item.event_id != record.event_id:
                self._log.warning(
                    "Outbox event_id mismatch: outbox_id=%s has "
                    "event_id=%s but callback has event_id=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    outbox_item.event_id,
                    record.event_id,
                )
                return

            # Validate adapter matches the outbox item's target.
            if record.adapter != outbox_item.target_adapter:
                self._log.warning(
                    "Adapter mismatch: outbox_id=%s callback adapter=%s "
                    "but outbox target_adapter=%s for event_id=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    record.adapter,
                    outbox_item.target_adapter,
                    record.event_id,
                )
                return

            # Validate delivery_plan_id matches (when present on record).
            if (
                record.delivery_plan_id is not None
                and record.delivery_plan_id != outbox_item.delivery_plan_id
            ):
                self._log.warning(
                    "delivery_plan_id mismatch: outbox_id=%s callback "
                    "plan_id=%s but outbox plan_id=%s for event_id=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    record.delivery_plan_id,
                    outbox_item.delivery_plan_id,
                    record.event_id,
                )
                return

            # Validate native_channel_id matches outbox target_channel
            # (when present on record).
            if record.native_channel_id is not None and (
                record.native_channel_id or None
            ) != (outbox_item.target_channel or None):
                self._log.warning(
                    "native_channel_id mismatch: outbox_id=%s callback "
                    "channel=%s but outbox target_channel=%s for "
                    "event_id=%s; skipping supplemental receipt",
                    record.outbox_id,
                    record.native_channel_id,
                    outbox_item.target_channel,
                    record.event_id,
                )
                return

            # Validate attempt_number — required for queue callbacks.
            if record.attempt_number is None:
                self._log.warning(
                    "Missing attempt_number: outbox_id=%s callback has "
                    "attempt_number=None for event_id=%s adapter=%s; "
                    "queue callbacks must carry attempt_number — rejecting",
                    record.outbox_id,
                    record.event_id,
                    record.adapter,
                )
                return
            if record.attempt_number != outbox_item.attempt_number:
                self._log.warning(
                    "attempt_number mismatch: outbox_id=%s callback "
                    "attempt=%d but outbox attempt=%d for event_id=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    record.attempt_number,
                    outbox_item.attempt_number,
                    record.event_id,
                )
                return

            # Find the queued receipt matching by outbox_id (exact).
            # Candidate filtering happens after outbox validation so that
            # malformed callbacks always produce deterministic rejection logs.
            candidates = [
                r
                for r in existing
                if r.status == "queued" and r.target_adapter == record.adapter
            ]
            # Filter by BOTH outbox_id and attempt_number: historical or
            # malformed queued receipts can share an outbox_id across
            # attempts, and source-preference must never finalize another
            # attempt's receipt for this callback.
            outbox_matches = [
                r
                for r in candidates
                if r.outbox_id == record.outbox_id
                and r.attempt_number == record.attempt_number
            ]

            if not outbox_matches:
                self._log.debug(
                    "No queued receipt matched outbox_id=%s "
                    "(plan_id=%s channel=%s) for event_id=%s adapter=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    outbox_item.delivery_plan_id,
                    outbox_item.target_channel,
                    record.event_id,
                    record.adapter,
                )
                return

            # Use source-aware selection among outbox-matching candidates.
            queued_receipt = self._select_source_preferred_candidate(
                outbox_matches,
                record,
            )
            if queued_receipt is None:
                return

            # Enforce attempt_number correlation: the outbox item and the
            # selected queued receipt must agree on the attempt number.
            # A mismatch indicates a stale callback from a prior attempt.
            if outbox_item.attempt_number != queued_receipt.attempt_number:
                self._log.warning(
                    "Attempt number mismatch: outbox_id=%s has "
                    "attempt_number=%d but queued receipt %s has "
                    "attempt_number=%d for event_id=%s adapter=%s; "
                    "stale callback from a prior attempt — rejecting",
                    record.outbox_id,
                    outbox_item.attempt_number,
                    queued_receipt.receipt_id,
                    queued_receipt.attempt_number,
                    record.event_id,
                    record.adapter,
                )
                return

            validated_outbox = outbox_item

        else:
            # No outbox_id on callback — hard reject.  All queued
            # callbacks MUST carry outbox_id for exact correlation.
            # Plan-id-only and no-key callbacks are no longer accepted.
            self._log.warning(
                "Hard reject: supplemental queued→sent callback lacks "
                "outbox_id for event_id=%s adapter=%s "
                "delivery_plan_id=%s native_channel_id=%s; exact "
                "outbox_id correlation is required — no fallback",
                record.event_id,
                record.adapter,
                record.delivery_plan_id,
                record.native_channel_id,
            )
            return

        # queued_receipt is guaranteed non-None here (every branch above
        # either sets it and continues or returns early).

        # Validate that the selected queued receipt can transition to sent.
        if not _is_valid_queued_to_sent_transition(queued_receipt.status):
            self._log.warning(
                "Selected queued receipt %s has status=%s which cannot "
                "transition to sent; skipping supplemental receipt for "
                "event_id=%s adapter=%s",
                queued_receipt.receipt_id,
                queued_receipt.status,
                record.event_id,
                record.adapter,
            )
            return

        supplemental = build_delivery_receipt(
            event_id=record.event_id,
            delivery_plan_id=queued_receipt.delivery_plan_id,
            target_adapter=record.adapter,
            target_channel=outbox_item.target_channel or queued_receipt.target_channel,
            route_id=queued_receipt.route_id,
            status="sent",
            adapter_message_id=record.native_message_id,
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
            outbox_id=record.outbox_id,
            confirmation_level=record.confirmation_level,
        )
        if validated_outbox is None:
            return

        native_ref = NativeMessageRef(
            id=f"nref-outbound-{uuid.uuid4()}",
            event_id=record.event_id,
            adapter=record.adapter,
            native_channel_id=record.native_channel_id,
            native_message_id=record.native_message_id,
            native_thread_id=record.native_thread_id,
            native_relation_id=record.native_relation_id,
            direction="outbound",
            metadata=dict(record.metadata),
            created_at=now,
        )
        committed = await storage.finalize_queued_delivery(
            native_ref,
            supplemental,
            outbox_id=validated_outbox.outbox_id,
            attempt_number=supplemental.attempt_number,
        )
        if not committed:
            self._log.warning(
                "Queued delivery finalization lost its outbox guard: "
                "outbox_id=%s event_id=%s adapter=%s attempt=%d; "
                "no native ref or sent receipt was committed",
                validated_outbox.outbox_id,
                record.event_id,
                record.adapter,
                supplemental.attempt_number,
            )

    # -- Retry-worker outbox transitions -----------------------------------

    async def abandon_retry_outbox(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        *,
        error_summary: str,
    ) -> None:
        """Persist terminal abandonment for an unreconstructable retry item."""
        await storage.mark_outbox_abandoned(
            item.outbox_id,
            error_summary=error_summary,
        )

    @staticmethod
    def _classify_retry_exception(error: Exception) -> DeliveryFailureKind:
        """Resolve a retry exception to the canonical failure taxonomy.

        Target-delivery exceptions carry a pre-classified ``failure_kind``
        and, for adapter failures, the original transport exception.  The
        retry authority consumes those attributes structurally so the runtime
        worker does not import target-delivery private exception classes.
        """
        classified = getattr(error, "failure_kind", None)
        if isinstance(classified, DeliveryFailureKind):
            return classified
        if isinstance(classified, str):
            try:
                return DeliveryFailureKind(classified)
            except ValueError:
                pass

        original = getattr(error, "original", None)
        if isinstance(original, Exception):
            return RetryExecutor.classify_failure(original)
        return RetryExecutor.classify_failure(error)

    def _classify_retry_receipt(
        self,
        receipt: DeliveryReceipt,
    ) -> DeliveryFailureKind:
        """Resolve canonical failed-receipt evidence to the failure taxonomy.

        Failed retry receipts are internal state-machine evidence.  Missing or
        invalid ``failure_kind`` values are invariant violations.  They use a
        conservative permanent fallback so claim reconciliation terminates the
        outbox row instead of repeatedly reclaiming malformed evidence.
        """
        if receipt.failure_kind is not None:
            try:
                return DeliveryFailureKind(receipt.failure_kind)
            except ValueError:
                pass

        self._log.error(
            "Retry failed receipt has malformed failure_kind; "
            "dead-lettering with fallback %s: receipt_id=%s outbox_id=%s "
            "failure_kind=%r",
            _MALFORMED_RETRY_EVIDENCE_KIND.value,
            receipt.receipt_id,
            receipt.outbox_id,
            receipt.failure_kind,
        )
        return _MALFORMED_RETRY_EVIDENCE_KIND

    @staticmethod
    def _retry_attempt_evidence(
        receipts: list[DeliveryReceipt],
        item: DeliveryOutboxItem,
        attempt_number: int,
    ) -> DeliveryReceipt | None:
        """Return evidence produced by exactly one outbox-backed retry attempt.

        ``outbox_id`` is the correlation authority.  Higher-attempt evidence
        is deliberately ignored so a stale worker snapshot cannot adopt a
        later attempt.  Dead-letter evidence is one lineage step after the
        failed attempt and must point back to evidence for this exact outbox.
        """
        target_receipts = [
            receipt
            for receipt in receipts
            if receipt.outbox_id == item.outbox_id
        ]
        malformed_current = [
            receipt
            for receipt in receipts
            if receipt.source == "retry"
            and receipt.outbox_id is None
            and receipt.attempt_number == attempt_number
            and (receipt.target_channel or None) == (item.target_channel or None)
        ]
        if malformed_current:
            raise ValueError(
                "Retry receipt is missing required outbox_id: "
                f"receipt_id={malformed_current[-1].receipt_id} "
                f"attempt_number={attempt_number}"
            )
        wrong_channel = [
            receipt
            for receipt in target_receipts
            if (receipt.target_channel or None) != (item.target_channel or None)
        ]
        if wrong_channel:
            raise ValueError(
                "Retry receipt outbox correlation has target_channel mismatch: "
                f"outbox_id={item.outbox_id} receipt_id={wrong_channel[-1].receipt_id}"
            )

        current = [
            receipt
            for receipt in target_receipts
            if receipt.attempt_number == attempt_number
        ]
        if current:
            current_ids = {receipt.receipt_id for receipt in current}
            malformed_dead_letters = [
                receipt
                for receipt in receipts
                if receipt.source == "retry"
                and receipt.outbox_id is None
                and receipt.status == "dead_lettered"
                and receipt.attempt_number == attempt_number + 1
                and receipt.parent_receipt_id in current_ids
            ]
            if malformed_dead_letters:
                raise ValueError(
                    "Retry dead-letter receipt is missing required outbox_id: "
                    f"receipt_id={malformed_dead_letters[-1].receipt_id}"
                )
            linked_dead_letters = [
                receipt
                for receipt in target_receipts
                if receipt.status == "dead_lettered"
                and receipt.attempt_number == attempt_number + 1
                and receipt.parent_receipt_id in current_ids
            ]
            if linked_dead_letters:
                return linked_dead_letters[-1]
            return current[-1]
        return None

    async def _finalize_retry_evidence(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        evidence: DeliveryReceipt | None,
        unpersisted_failure_kind: DeliveryFailureKind | None,
        unpersisted_error_summary: str | None,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization:
        """Commit the outbox state implied by one retry attempt's evidence."""
        attempt_number = item.attempt_number + 1

        if evidence is not None and evidence.status in {"queued", "sent", "suppressed"}:
            accepted = await self.finalize_retry_success(storage, item, evidence)
            return RetryAttemptFinalization(
                outcome="accepted" if accepted else "suppressed",
                receipt_id=evidence.receipt_id,
                failure_kind=evidence.failure_kind,
                attempt_number=evidence.attempt_number,
            )

        if evidence is not None and evidence.status == "dead_lettered":
            terminal_kind = evidence.failure_kind or "retry_exhausted"
            await storage.mark_outbox_dead_lettered(
                item.outbox_id,
                receipt_id=evidence.receipt_id,
                failure_kind=terminal_kind,
                error_summary=evidence.error[:512] if evidence.error else None,
                attempt_number=attempt_number,
            )
            return RetryAttemptFinalization(
                outcome="dead_lettered",
                receipt_id=evidence.receipt_id,
                failure_kind=terminal_kind,
                attempt_number=attempt_number,
            )

        failure_kind = unpersisted_failure_kind
        receipt_id: str | None = None
        error_summary = (
            unpersisted_error_summary[:512]
            if unpersisted_error_summary is not None
            else None
        )
        if evidence is not None and evidence.status == "failed":
            receipt_id = evidence.receipt_id
            error_summary = evidence.error[:512] if evidence.error else None
            failure_kind = self._classify_retry_receipt(evidence)

        if failure_kind is None:
            raise ValueError(
                "Retry failure finalization requires failed receipt evidence "
                "or an exception classification"
            )
        if error_summary is None:
            error_summary = "Retry delivery failed"

        executor = RetryExecutor(retry_policy)
        if not failure_kind.is_retryable or executor.is_exhausted(attempt_number):
            terminal_kind = (
                "retry_exhausted" if failure_kind.is_retryable else failure_kind.value
            )
            await storage.mark_outbox_dead_lettered(
                item.outbox_id,
                receipt_id=receipt_id,
                failure_kind=terminal_kind,
                error_summary=error_summary,
                attempt_number=attempt_number,
            )
            return RetryAttemptFinalization(
                outcome="dead_lettered",
                receipt_id=receipt_id,
                failure_kind=terminal_kind,
                attempt_number=attempt_number,
            )

        if evidence is not None and evidence.next_retry_at is not None:
            next_attempt_at = evidence.next_retry_at
            await storage.mark_outbox_retry_wait(
                item.outbox_id,
                next_attempt_at=next_attempt_at.isoformat(),
                receipt_id=receipt_id,
                failure_kind=failure_kind.value,
                error_summary=error_summary,
                attempt_number=attempt_number,
            )
        else:
            next_attempt_at = await self.defer_retry_outbox(
                storage,
                item,
                retry_policy,
                failure_kind=failure_kind.value,
                attempt_number=attempt_number,
                receipt_id=receipt_id,
                error_summary=error_summary,
                now=now,
            )

        return RetryAttemptFinalization(
            outcome="retry_wait",
            receipt_id=receipt_id,
            failure_kind=failure_kind.value,
            attempt_number=attempt_number,
            next_retry_at=next_attempt_at,
        )

    async def reconcile_retry_claim(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization | None:
        """Repair a claimed outbox row from already-persisted next-attempt evidence.

        This preflight closes the partial-persistence window where target
        delivery appended receipt evidence but the corresponding outbox
        transition failed.  If evidence for ``item.attempt_number + 1``
        already exists, the lifecycle authority commits the missing outbox
        transition and the caller MUST NOT invoke the transport again.

        ``None`` means no uncommitted next-attempt evidence exists and normal
        retry delivery may proceed.
        """
        attempt_number = item.attempt_number + 1
        receipts = await storage.list_receipts_for_plan(
            item.delivery_plan_id,
            item.target_adapter,
        )
        evidence = self._retry_attempt_evidence(receipts, item, attempt_number)
        if evidence is None:
            return None

        return await self._finalize_retry_evidence(
            storage,
            item,
            retry_policy,
            evidence=evidence,
            unpersisted_failure_kind=None,
            unpersisted_error_summary=None,
            now=now,
        )

    async def finalize_retry_attempt_error(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        error: Exception,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization:
        """Reconcile durable evidence after a retry delivery raises.

        This is the retry failure-classification authority.  It selects only
        evidence attributable to the current outbox attempt, treats durable
        ``queued``/``sent`` evidence as acceptance even when a later
        persistence step raised, honours existing dead-letter evidence,
        terminates non-retryable failures immediately, and otherwise commits
        the retry-wait transition.

        Storage errors are intentionally not swallowed.  If evidence lookup
        or the selected outbox transition cannot be persisted, the worker must
        not report a lifecycle state that storage did not commit; the claimed
        row remains recoverable through its lease-expiry path, and claim
        reconciliation will repair persisted attempt evidence before resend.
        """
        attempt_number = item.attempt_number + 1
        receipts = await storage.list_receipts_for_plan(
            item.delivery_plan_id,
            item.target_adapter,
        )
        evidence = self._retry_attempt_evidence(receipts, item, attempt_number)
        failure_kind = self._classify_retry_exception(error)
        error_summary = f"{type(error).__name__}: {error}"
        return await self._finalize_retry_evidence(
            storage,
            item,
            retry_policy,
            evidence=evidence,
            unpersisted_failure_kind=failure_kind,
            unpersisted_error_summary=error_summary,
            now=now,
        )

    async def defer_retry_outbox(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        failure_kind: str,
        attempt_number: int,
        receipt_id: str | None = None,
        error_summary: str | None = None,
        now: datetime | None = None,
    ) -> datetime:
        """Schedule one retry attempt with lifecycle-owned backoff."""
        backoff = RetryExecutor(retry_policy).compute_backoff(attempt_number)
        next_attempt_at = (now or datetime.now(timezone.utc)) + backoff
        await storage.mark_outbox_retry_wait(
            item.outbox_id,
            next_attempt_at=next_attempt_at.isoformat(),
            receipt_id=receipt_id,
            failure_kind=failure_kind,
            error_summary=error_summary,
            attempt_number=attempt_number,
        )
        return next_attempt_at

    async def finalize_retry_success(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        receipt: DeliveryReceipt,
    ) -> bool:
        """Persist a retry result and report whether it represents success."""
        if receipt.status == "queued":
            await storage.mark_outbox_queued(
                item.outbox_id,
                receipt_id=receipt.receipt_id,
                attempt_number=receipt.attempt_number,
            )
            return True
        if receipt.status == "sent":
            await storage.mark_outbox_sent(
                item.outbox_id,
                receipt_id=receipt.receipt_id,
                attempt_number=receipt.attempt_number,
            )
            return True
        if receipt.status == "suppressed":
            await storage.mark_outbox_abandoned(
                item.outbox_id,
                error_summary=receipt.error,
            )
            return False
        raise ValueError(
            "Retry success finalization requires a queued, sent, or suppressed "
            f"receipt; got {receipt.status!r}"
        )

    # -- Outbox finalization ------------------------------------------------

    async def finalize_outbox_outcome(
        self,
        storage: DeliveryLifecycleStorage,
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
                    )
                else:
                    await storage.mark_outbox_sent(
                        outbox_id,
                        receipt_id=receipt.receipt_id,
                    )
            elif failure_kind_val is not None:
                receipt_ref_id: str | None = (
                    receipt.receipt_id if receipt is not None else None
                )
                # NOTE: attempt_number is NOT passed to mark_outbox_* calls.
                # The outbox row's attempt_number is set correctly at creation
                # time by _create_outbox_for_delivery() and must not be
                # overwritten — doing so would risk UNIQUE constraint violations
                # when the receipt's attempt_number (computed from receipt
                # lineage) differs from the outbox's attempt_number (computed
                # from max existing outbox rows).
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
                        )
                    else:
                        # No persisted receipt.  Derive attempt number from
                        # the outbox row so backoff reflects the real count.
                        outbox_item = await storage.get_outbox_item(outbox_id)
                        retry_attempt = outbox_item.attempt_number if outbox_item else 1
                        executor = RetryExecutor(retry_policy)
                        if executor.is_exhausted(retry_attempt):
                            await storage.mark_outbox_dead_lettered(
                                outbox_id,
                                receipt_id=receipt_ref_id,
                                failure_kind=failure_kind_val.value,
                                error_summary=error_summary,
                            )
                        else:
                            backoff_duration = executor.compute_backoff(retry_attempt)
                            next_attempt_at = (
                                datetime.now(timezone.utc) + backoff_duration
                            ).isoformat()
                            await storage.mark_outbox_retry_wait(
                                outbox_id,
                                next_attempt_at=next_attempt_at,
                                receipt_id=receipt_ref_id,
                                failure_kind=failure_kind_val.value,
                                error_summary=error_summary,
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
