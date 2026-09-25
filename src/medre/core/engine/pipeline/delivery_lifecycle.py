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
    Attempt evidence uses ``queued``, ``sent``, ``failed``. Lifecycle evidence
    uses ``dead_lettered``, ``cancelled``, ``abandoned``, ``suppressed``.

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
    stale-callback protection.  A reserved retry attempt is never reused
    after claim recovery: an evidence-less reclaimed reservation is consumed
    as an ambiguous attempt before any later dispatch is allowed.  This keeps
    an attempt number a unique dispatch generation for one outbox row.
    ``delivery_plan_id`` is a validation field checked against the outbox
    item, but is NOT the correlation selector.  Callbacks missing
    ``outbox_id`` or ``attempt_number`` are hard-rejected.

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
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from medre.core.contracts.adapter import (
    MAX_ADAPTER_RETRY_AFTER_SECONDS,
    OutboundDeliveryObservationRecord,
    OutboundNativeRefRecord,
)
from medre.core.delivery_authority import (
    DeliveryIdentity,
    committed_receipt_for_outbox,
    delivery_attempt_provenance_mismatch,
    delivery_identity,
    effective_generation,
)
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.engine.pipeline.delivery_state import (
    is_terminal_outbox_status as _is_terminal_outbox_status,
)
from medre.core.engine.pipeline.delivery_state import (
    is_valid_queued_to_sent_transition as _is_valid_queued_to_sent_transition,
)
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events.canonical import (
    DeliveryObservation,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    QueuedDeliveryFinalization,
    TerminalOutboxFinalization,
)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

_AMBIGUOUS_DISPATCH_FAILURE_KIND = DeliveryFailureKind.ADAPTER_TRANSIENT.value
_AMBIGUOUS_DISPATCH_FAILURE_DETAIL = "dispatch_outcome_unknown"
_AMBIGUOUS_DISPATCH_ERROR = (
    "reserved retry dispatch was reclaimed without durable receipt evidence; "
    "outcome is unknown and the attempt identity is consumed"
)

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

    async def append_delivery_observation(
        self, observation: DeliveryObservation
    ) -> bool:
        """Append evidence for an admissible outbox attempt if not already stored.

        Return ``False`` for a duplicate ID or an outbox attempt that no
        longer permits an observation; return ``True`` for a new row.
        """
        ...

    async def list_receipts_for_delivery(
        self,
        identity: DeliveryIdentity,
    ) -> list[DeliveryReceipt]:
        """List immutable receipt history for one complete delivery identity."""
        ...

    async def finalize_queued_delivery(
        self,
        command: QueuedDeliveryFinalization,
    ) -> bool:
        """Commit sent evidence and the guarded outbox transition atomically.

        Return ``False`` if the outbox generation can no longer be finalized.
        """
        ...

    async def finalize_outbox_terminal(
        self,
        command: TerminalOutboxFinalization,
    ) -> bool:
        """Atomically append terminal evidence and advance outbox authority."""
        ...

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None: ...

    async def reserve_outbox_attempt(
        self,
        outbox_id: str,
        worker_id: str,
        from_attempt: int,
    ) -> int | None:
        """Reserve the next attempt for the current claim owner.

        The successful reservation also records ``dispatch_source='retry'``
        as durable provenance for callbacks that can beat receipt append.
        Return its number, or ``None`` if the row is no longer owned,
        in progress, or eligible for a new reservation. A failed
        reservation must not proceed to transport dispatch.
        """
        ...

    async def renew_outbox_lease(
        self,
        outbox_id: str,
        worker_id: str,
        lease_until: str,
    ) -> bool:
        """Extend the owned in-progress row's lease to ``lease_until``.

        Return ``False`` if the claim is no longer held by ``worker_id``.
        """
        ...

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Commit a sent outcome if the supplied attempt and owner still match.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Commit queue acceptance if the supplied attempt and owner still match.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_retry_wait(
        self,
        outbox_id: str,
        next_attempt_at: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Schedule the next attempt if the supplied attempt and owner match.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_dead_lettered(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Commit terminal failure if the supplied attempt and owner match.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_cancelled(
        self,
        outbox_id: str,
        error_summary: str | None = None,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Cancel the row only while its optional claim owner still matches.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_abandoned(
        self,
        outbox_id: str,
        error_summary: str | None = None,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Abandon the row only while its optional claim owner still matches.

        Return ``False`` when the guarded transition did not commit.
        """
        ...


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

    outcome: Literal[
        "accepted",
        "suppressed",
        "retry_wait",
        "dead_lettered",
        "cancelled",
        "abandoned",
    ]
    receipt_id: str | None
    failure_kind: str | None
    attempt_number: int
    next_retry_at: datetime | None = None


class RetryAttemptCommitRejected(RuntimeError):
    """A guarded retry outbox transition no longer owns the durable row.

    This is not a storage-I/O failure.  It means the compare-and-set guard
    rejected a stale attempt or a worker that no longer owns the claim.  The
    caller must not emit durable lifecycle success/failure evidence for a
    transition that did not commit.
    """


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

    # -- Attempt identity ----------------------------------------------------

    @staticmethod
    def effective_attempt(outbox: DeliveryOutboxItem) -> int:
        """Return the attempt callbacks must carry for *outbox*.

        A durably reserved in-flight attempt (``active_attempt``) is the
        live attempt identity from the moment dispatch reserves it until
        finalization consumes it; before reservation and after
        finalization the row's ``attempt_number`` is the live identity.
        Every callback validator compares against this value.
        """
        return effective_generation(outbox)

    @staticmethod
    def _require_retry_commit(
        committed: bool,
        item: DeliveryOutboxItem,
        *,
        transition: str,
        attempt_number: int | None,
    ) -> None:
        """Reject stale retry lifecycle reports after a guarded no-op."""
        if committed:
            return
        attempt = "none" if attempt_number is None else str(attempt_number)
        raise RetryAttemptCommitRejected(
            f"Retry outbox transition {transition!r} did not commit: "
            f"outbox_id={item.outbox_id} attempt_number={attempt} "
            f"worker_id={item.worker_id or 'none'}"
        )

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
        retry_after_seconds: float | None = None,
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
        retry_after_seconds:
            Optional adapter-provided minimum delay.  For retryable failures,
            the effective delay is the larger of policy backoff and this hint,
            clamped to ``MAX_ADAPTER_RETRY_AFTER_SECONDS`` so an absurd hint
            cannot
            produce an unrepresentable timestamp.

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
                if retry_after_seconds is not None:
                    hinted = timedelta(
                        seconds=min(
                            retry_after_seconds, MAX_ADAPTER_RETRY_AFTER_SECONDS
                        )
                    )
                    if hinted > backoff:
                        backoff = hinted
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

    # -- Terminal lifecycle evidence ----------------------------------------

    @staticmethod
    def is_terminal_failure(
        failure_kind: DeliveryFailureKind,
        *,
        next_retry_at: datetime | None,
    ) -> bool:
        """Return whether a failed execution has no remaining retry path.

        Retryability alone is insufficient: a retryable failure is terminal
        when no retry was scheduled (no policy or exhausted policy).
        """
        return not failure_kind.is_retryable or next_retry_at is None

    @staticmethod
    def build_terminal_lifecycle_receipt(
        previous_receipt: DeliveryReceipt,
        *,
        status: Literal["dead_lettered", "cancelled", "abandoned"],
        error: str | None = None,
        failure_kind: str | None = None,
    ) -> DeliveryReceipt:
        """Build terminal lifecycle evidence linked to *previous_receipt*.

        The transition preserves the causative attempt number. It is not a new
        dispatch generation and therefore never increments ``attempt_number``.
        Retry/rendering provenance is inherited from the causative receipt.
        """
        return build_delivery_receipt(
            event_id=previous_receipt.event_id,
            delivery_plan_id=previous_receipt.delivery_plan_id,
            target_adapter=previous_receipt.target_adapter,
            target_channel=previous_receipt.target_channel,
            route_id=previous_receipt.route_id,
            status=status,
            receipt_kind="lifecycle",
            error=error if error is not None else previous_receipt.error,
            failure_kind=(
                failure_kind
                if failure_kind is not None
                else previous_receipt.failure_kind
            ),
            attempt_number=previous_receipt.attempt_number,
            parent_receipt_id=previous_receipt.receipt_id,
            source=previous_receipt.source,
            replay_run_id=previous_receipt.replay_run_id,
            retry_max_attempts=previous_receipt.retry_max_attempts,
            retry_backoff_base=previous_receipt.retry_backoff_base,
            retry_max_delay=previous_receipt.retry_max_delay,
            retry_jitter=previous_receipt.retry_jitter,
            outbox_id=previous_receipt.outbox_id,
            confirmation_level=previous_receipt.confirmation_level,
        )

    async def build_and_persist_terminal_receipt(
        self,
        storage: DeliveryLifecycleStorage,
        previous_receipt: DeliveryReceipt,
        *,
        status: Literal["dead_lettered", "cancelled", "abandoned"],
        error: str | None = None,
        failure_kind: str | None = None,
    ) -> DeliveryReceipt:
        """Append terminal lifecycle evidence linked to one execution attempt."""
        receipt = self.build_terminal_lifecycle_receipt(
            previous_receipt,
            status=status,
            error=error,
            failure_kind=failure_kind,
        )
        await storage.append_receipt(receipt)
        return receipt

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
            The attempt number of the causative primary receipt. The lifecycle
            receipt keeps this same number because dead-lettering is a state
            transition, not another dispatch attempt.
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
            attempt_number=attempt_number,
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
            Dispatch mechanism (``"live"``, ``"retry"``, ``"replay"``).
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
            receipt_kind="lifecycle",
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
        """Select the queued receipt candidate for this exact row/attempt.

        The caller guarantees every candidate already matches the callback's
        ``outbox_id`` + ``attempt_number`` AND that the authoritative outbox
        row was validated for this exact callback (status, event, adapter,
        plan, channel, attempt).  Each candidate therefore belongs to this
        one delivery attempt; selecting it cannot mutate any other row.
        The receipt's own durable ``source`` / ``replay_run_id`` lineage is
        the trusted attempt provenance — the same recovery used by
        :meth:`~medre.core.engine.pipeline.outbox_manager.OutboxManager.record_terminal`
        for terminal failure callbacks — so a replay-sourced candidate is
        finalized exactly like a live one, with its replay lineage carried
        onto the supplemental ``sent`` receipt.

        When malformed history offers duplicates across sources for the
        same row/attempt (a row is single-sourced in normal operation),
        non-replay (``"live"`` / ``"retry"``) candidates are preferred over
        ``"replay"`` candidates; within the preferred group the most-recent
        (last in append-order) candidate wins, preserving retry-lineage
        behaviour.

        Parameters
        ----------
        candidates:
            Non-empty list of matching queued receipts (already filtered by
            outbox_id + attempt_number against the validated row).
        record:
            The outbound native reference record from the adapter callback.
            Used for log context only.

        Returns
        -------
        DeliveryReceipt | None
            The selected receipt, or ``None`` when no candidate exists.
        """
        if not candidates:
            return None
        live_candidates = [r for r in candidates if r.source != "replay"]
        if live_candidates:
            # Prefer the latest non-replay candidate.
            return live_candidates[-1]

        # Only replay-sourced candidates — this row belongs to a replay
        # execution and the callback matched its exact outbox_id +
        # attempt_number, so finalize it with its replay lineage.
        self._log.debug(
            "Supplemental queued→sent correlation: selecting replay-sourced "
            "queued receipt %s (replay_run_id=%s) for outbox_id=%s "
            "event_id=%s adapter=%s — exact row/attempt correlation",
            candidates[-1].receipt_id,
            candidates[-1].replay_run_id,
            record.outbox_id,
            record.event_id,
            record.adapter,
        )
        return candidates[-1]

    # -- Post-handoff observations ------------------------------------------

    async def record_delivery_observation(
        self,
        storage: DeliveryLifecycleStorage,
        record: OutboundDeliveryObservationRecord,
        now: datetime,
    ) -> bool:
        """Persist append-only transport evidence for one exact attempt.

        Observations never reopen or rewrite the receipt/outbox lifecycle.
        Exact ``outbox_id`` and ``attempt_number`` correlation is mandatory.
        Only attempts still being handed off (``in_progress``/``queued``) or
        already terminal ``sent`` may receive post-handoff evidence.  A late
        callback from an attempt that has moved into retry/dead-letter/cancel
        state is stale and is rejected.

        Attempt identity follows the durable reservation: the retry worker
        reserves the next attempt before invoking the transport, so a
        callback carrying the reserved (live) attempt number is admissible
        for the entire handoff — including before the outbox transition
        commits — while a callback carrying any earlier attempt number is
        stale the moment the reservation is durably recorded.  The storage
        append revalidates the same rule atomically.

        Returns ``True`` when a new observation row was appended.  Missing or
        mismatched correlation, stale attempts, ineligible outbox states, and
        duplicate notifications return ``False``.
        """
        if record.outbox_id is None or record.attempt_number is None:
            self._log.warning(
                "Rejecting uncorrelated delivery observation: event_id=%s "
                "adapter=%s state=%s outbox_id=%s attempt=%s",
                record.event_id,
                record.adapter,
                record.state,
                record.outbox_id,
                record.attempt_number,
            )
            return False

        outbox = await storage.get_outbox_item(record.outbox_id)
        if outbox is None:
            self._log.warning(
                "Rejecting delivery observation for missing outbox row: "
                "outbox_id=%s event_id=%s adapter=%s",
                record.outbox_id,
                record.event_id,
                record.adapter,
            )
            return False
        provenance = record.attempt_provenance
        if provenance is not None:
            mismatch = delivery_attempt_provenance_mismatch(provenance, outbox)
            if mismatch is not None:
                self._log.warning(
                    "Rejecting delivery observation with contradictory attempt "
                    "provenance: outbox_id=%s %s",
                    record.outbox_id,
                    mismatch,
                )
                return False
        if (
            outbox.event_id != record.event_id
            or outbox.target_adapter != record.adapter
            or self.effective_attempt(outbox) != record.attempt_number
        ):
            self._log.warning(
                "Rejecting stale/mismatched delivery observation: outbox_id=%s "
                "record=(event=%s adapter=%s attempt=%s) "
                "stored=(event=%s adapter=%s attempt=%s active_attempt=%s)",
                record.outbox_id,
                record.event_id,
                record.adapter,
                record.attempt_number,
                outbox.event_id,
                outbox.target_adapter,
                outbox.attempt_number,
                outbox.active_attempt,
            )
            return False
        if record.delivery_plan_id is not None and (
            record.delivery_plan_id != outbox.delivery_plan_id
        ):
            self._log.warning(
                "Rejecting delivery observation with plan mismatch: "
                "outbox_id=%s record_plan=%s stored_plan=%s",
                record.outbox_id,
                record.delivery_plan_id,
                outbox.delivery_plan_id,
            )
            return False
        if outbox.status not in {"in_progress", "queued", "sent"}:
            self._log.warning(
                "Rejecting delivery observation for non-handoff outbox state: "
                "outbox_id=%s status=%s attempt=%s state=%s",
                record.outbox_id,
                outbox.status,
                record.attempt_number,
                record.state,
            )
            return False

        identity = "\x1f".join(
            (
                record.outbox_id,
                str(record.attempt_number),
                record.adapter,
                record.native_message_id or "",
                record.state,
                record.confirmation_level,
            )
        )
        observation_id = "obs-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        observation = DeliveryObservation(
            observation_id=observation_id,
            event_id=record.event_id,
            delivery_plan_id=outbox.delivery_plan_id,
            target_adapter=record.adapter,
            target_channel=outbox.target_channel,
            native_channel_id=record.native_channel_id,
            outbox_id=outbox.outbox_id,
            attempt_number=record.attempt_number,
            adapter_message_id=record.native_message_id,
            state=record.state,
            confirmation_level=record.confirmation_level,
            error=record.error,
            metadata=dict(record.metadata),
            observed_at=now,
        )
        return await storage.append_delivery_observation(observation)

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
        - The outbox item's effective attempt — a reserved
          ``active_attempt`` while an attempt is being handed off,
          otherwise its stored ``attempt_number`` — must match the queued
          receipt's ``attempt_number``.  A mismatch indicates a stale
          callback from a superseded attempt.
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
        transaction. The storage transaction re-checks the complete delivery
        identity, outbox ID, attempt number, and status so a concurrent reclaim
        or sibling-target mismatch cannot partially commit.

        If no matching ``"queued"`` receipt is found (e.g. a non-queued
        adapter), the method returns silently.

        Parameters
        ----------
        storage:
            The storage backend for receipt/outbox persistence.
        record:
            The outbound native reference record from the adapter.
        now:
            Timestamp for the new receipt.
        """
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

            provenance = record.attempt_provenance
            if provenance is not None:
                mismatch = delivery_attempt_provenance_mismatch(
                    provenance, outbox_item
                )
                if mismatch is not None:
                    self._log.warning(
                        "Queued delivery callback rejected: contradictory attempt "
                        "provenance for outbox_id=%s: %s",
                        record.outbox_id,
                        mismatch,
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
            if record.attempt_number != self.effective_attempt(outbox_item):
                self._log.warning(
                    "attempt_number mismatch: outbox_id=%s callback "
                    "attempt=%d but outbox effective attempt=%d "
                    "(stored=%d active=%s) for event_id=%s; "
                    "skipping supplemental receipt",
                    record.outbox_id,
                    record.attempt_number,
                    self.effective_attempt(outbox_item),
                    outbox_item.attempt_number,
                    outbox_item.active_attempt,
                    record.event_id,
                )
                return

            # Historical lookup is scoped to the complete lifecycle identity
            # carried by the authoritative outbox row.  Event-wide scans can
            # merge sibling channels or targets and make exact callback
            # correlation depend on later ad-hoc filtering.
            try:
                existing = await storage.list_receipts_for_delivery(
                    delivery_identity(outbox_item)
                )
            except Exception:
                self._log.exception(
                    "Failed to list delivery receipt history for supplemental "
                    "queued->sent: outbox_id=%s event_id=%s plan_id=%s "
                    "adapter=%s channel=%s",
                    record.outbox_id,
                    outbox_item.event_id,
                    outbox_item.delivery_plan_id,
                    outbox_item.target_adapter,
                    outbox_item.target_channel,
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

            if provenance is not None:
                # Callback provenance is authoritative. Queued evidence supplies
                # immutable parent linkage and retry/render fields only, and must
                # agree with the envelope when already present.
                for candidate in outbox_matches:
                    if candidate.source != provenance.source or (
                        candidate.replay_run_id != provenance.replay_run_id
                    ):
                        self._log.warning(
                            "Queued delivery callback rejected: receipt provenance "
                            "contradicts callback for outbox_id=%s attempt=%d",
                            provenance.outbox_id,
                            provenance.attempt_number,
                        )
                        return
                queued_receipt = outbox_matches[-1]
            else:
                # Legacy/custom callback compatibility. Built-in queue adapters
                # carry attempt_provenance and do not take this path.
                queued_receipt = self._select_source_preferred_candidate(
                    outbox_matches,
                    record,
                )
                if queued_receipt is None:
                    return

            # Enforce attempt_number correlation: the outbox item and the
            # selected queued receipt must agree on the attempt number.
            # A mismatch indicates a stale callback from a prior attempt.
            if self.effective_attempt(outbox_item) != queued_receipt.attempt_number:
                self._log.warning(
                    "Attempt number mismatch: outbox_id=%s has "
                    "effective attempt=%d but queued receipt %s has "
                    "attempt_number=%d for event_id=%s adapter=%s; "
                    "stale callback from a prior attempt — rejecting",
                    record.outbox_id,
                    self.effective_attempt(outbox_item),
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
            source=(
                provenance.source if provenance is not None else queued_receipt.source
            ),
            replay_run_id=(
                provenance.replay_run_id
                if provenance is not None
                else queued_receipt.replay_run_id
            ),
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
            native_channel_id=(
                record.native_channel_id
                if record.native_channel_id is not None
                else validated_outbox.target_channel
            ),
            native_message_id=record.native_message_id,
            native_thread_id=record.native_thread_id,
            native_relation_id=record.native_relation_id,
            direction="outbound",
            metadata=dict(record.metadata),
            created_at=now,
        )
        committed = await storage.finalize_queued_delivery(
            QueuedDeliveryFinalization(
                native_ref=native_ref,
                receipt=supplemental,
            )
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
        committed = await storage.mark_outbox_abandoned(
            item.outbox_id,
            error_summary=error_summary,
            attempt_number=item.active_attempt,
            expected_worker_id=item.worker_id,
        )
        self._require_retry_commit(
            committed,
            item,
            transition="abandoned",
            attempt_number=item.active_attempt,
        )

    @staticmethod
    def _classify_retry_exception(error: Exception) -> DeliveryFailureKind:
        """Resolve a retry exception to the canonical failure taxonomy.

        Target-delivery exceptions carry a pre-classified ``failure_kind``
        and, for adapter failures, the original transport exception.  The
        retry authority consumes those attributes structurally so the runtime
        worker does not import target-delivery private exception classes.
        """
        evidence = getattr(error, "evidence", None)
        if isinstance(evidence, DeliveryExecutionEvidence):
            if evidence.failure_kind is not None:
                return evidence.failure_kind

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
        later attempt.  Lifecycle evidence (dead-letter, cancellation, or
        abandonment) must link to attempt evidence for this exact outbox.
        """
        target_receipts = [
            receipt for receipt in receipts if receipt.outbox_id == item.outbox_id
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
            attempt_ids = {
                receipt.receipt_id
                for receipt in current
                if receipt.receipt_kind == "attempt"
            }
            malformed_lifecycle = [
                receipt
                for receipt in receipts
                if receipt.source == "retry"
                and receipt.outbox_id is None
                and receipt.receipt_kind == "lifecycle"
                and receipt.attempt_number == attempt_number
                and receipt.parent_receipt_id in attempt_ids
            ]
            if malformed_lifecycle:
                raise ValueError(
                    "Retry lifecycle receipt is missing required outbox_id: "
                    f"receipt_id={malformed_lifecycle[-1].receipt_id}"
                )
            terminal_lifecycle = [
                receipt
                for receipt in current
                if receipt.receipt_kind == "lifecycle"
                and receipt.status in {"dead_lettered", "cancelled", "abandoned"}
            ]
            unlinked_terminal = [
                receipt
                for receipt in terminal_lifecycle
                if receipt.parent_receipt_id not in attempt_ids
            ]
            if unlinked_terminal:
                raise ValueError(
                    "Retry terminal lifecycle receipt is not linked to same-attempt "
                    "attempt evidence: "
                    f"receipt_id={unlinked_terminal[-1].receipt_id} "
                    f"attempt_number={attempt_number}"
                )
            if terminal_lifecycle:
                return terminal_lifecycle[-1]
            return current[-1]
        return None

    @staticmethod
    def _resolve_retry_attempt(
        item: DeliveryOutboxItem,
        attempt_number: int | None,
    ) -> int:
        """Resolve the retry attempt a finalization commits.

        *attempt_number* is authoritative when the caller reserved it
        (the dispatch-begin reservation).  Otherwise a reservation still
        durable on the row is the live attempt, and only an unreserved row
        falls back to the lineage rule ``stored attempt + 1``.
        """
        if attempt_number is not None:
            return attempt_number
        if item.active_attempt is not None:
            return item.active_attempt
        return item.attempt_number + 1

    async def _finalize_retry_evidence(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        evidence: DeliveryReceipt | None,
        unpersisted_failure_kind: DeliveryFailureKind | None,
        unpersisted_error_summary: str | None,
        attempt_number: int | None = None,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization:
        """Commit the outbox state implied by one retry attempt's evidence."""
        attempt_number = self._resolve_retry_attempt(item, attempt_number)

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
            committed = await storage.mark_outbox_dead_lettered(
                item.outbox_id,
                receipt_id=evidence.receipt_id,
                failure_kind=terminal_kind,
                error_summary=evidence.error[:512] if evidence.error else None,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="dead_lettered",
                attempt_number=attempt_number,
            )
            return RetryAttemptFinalization(
                outcome="dead_lettered",
                receipt_id=evidence.receipt_id,
                failure_kind=terminal_kind,
                attempt_number=attempt_number,
            )

        if evidence is not None and evidence.status in {"cancelled", "abandoned"}:
            mark_terminal = (
                storage.mark_outbox_cancelled
                if evidence.status == "cancelled"
                else storage.mark_outbox_abandoned
            )
            committed = await mark_terminal(
                item.outbox_id,
                error_summary=evidence.error[:512] if evidence.error else None,
                receipt_id=evidence.receipt_id,
                failure_kind=evidence.failure_kind,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition=evidence.status,
                attempt_number=attempt_number,
            )
            return RetryAttemptFinalization(
                outcome=evidence.status,
                receipt_id=evidence.receipt_id,
                failure_kind=evidence.failure_kind,
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
            if evidence is not None and evidence.status == "failed":
                lifecycle_receipt = self.build_terminal_lifecycle_receipt(
                    evidence,
                    status="dead_lettered",
                    error=error_summary,
                    failure_kind=terminal_kind,
                )
                committed = await storage.finalize_outbox_terminal(
                    TerminalOutboxFinalization(
                        lifecycle_receipt=lifecycle_receipt,
                        expected_worker_id=item.worker_id,
                    )
                )
                receipt_id = lifecycle_receipt.receipt_id
            else:
                committed = await storage.mark_outbox_dead_lettered(
                    item.outbox_id,
                    receipt_id=receipt_id,
                    failure_kind=terminal_kind,
                    error_summary=error_summary,
                    attempt_number=attempt_number,
                    expected_worker_id=item.worker_id,
                )
            self._require_retry_commit(
                committed,
                item,
                transition="dead_lettered",
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
            committed = await storage.mark_outbox_retry_wait(
                item.outbox_id,
                next_attempt_at=next_attempt_at.isoformat(),
                receipt_id=receipt_id,
                failure_kind=failure_kind.value,
                error_summary=error_summary,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="retry_wait",
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

    async def reserve_retry_attempt(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
    ) -> int | None:
        """Durably reserve the next attempt on a row this worker claimed.

        This is the dispatch-begin boundary for attempt identity.  Once the
        reservation commits, callbacks carrying the reserved number are
        live for the entire handoff — including before the outbox
        transition commits — while callbacks carrying any earlier attempt
        number are stale.  The reservation is consumed atomically by the
        finalization that commits the attempt's outcome.

        Returns the reserved attempt number, or ``None`` when the row is
        no longer owned by this worker (lost claim, lease theft, or a
        competing reservation); the caller MUST NOT invoke the transport
        in that case.
        """
        if item.worker_id is None:
            return None
        return await storage.reserve_outbox_attempt(
            item.outbox_id,
            item.worker_id,
            item.attempt_number,
        )

    async def renew_retry_lease(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        *,
        lease_seconds: int,
    ) -> bool:
        """Extend the dispatch lease on a row this worker still owns.

        ``lease_seconds`` is measured from the current UTC time. The retry
        worker calls this during dispatch; process death allows the lease to
        expire for claim reconciliation. Returns ``False`` if this worker no
        longer owns the row or the row is no longer in progress.
        """
        if item.worker_id is None:
            return False
        lease_until = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        return await storage.renew_outbox_lease(
            item.outbox_id,
            item.worker_id,
            lease_until,
        )

    async def reconcile_retry_claim(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization | None:
        """Repair a claimed outbox row from already-persisted attempt evidence.

        This preflight closes the partial-persistence window where target
        delivery appended receipt evidence but the corresponding outbox
        transition failed.  Two cases exist:

        * The claimed row carries a durable attempt reservation
          (``active_attempt``): evidence for exactly that attempt means a
          prior worker dispatched it and died before finalization — commit
          the missing transition and return it; the caller MUST NOT invoke
          the transport again.  A reservation without evidence is ambiguous:
          the prior process may have died before transport invocation, or the
          transport may still have accepted the send while receipt persistence
          was lost.  The reserved identity is therefore consumed as a failed
          attempt and the row moves to ``retry_wait`` (or ``dead_lettered``
          when the attempt budget is exhausted).  A later dispatch MUST use a
          strictly newer attempt number; retry recovery never reuses a reserved
          dispatch identity.
        * No reservation: check for next-attempt evidence at
          ``item.attempt_number + 1`` defensively (a superseded engine
          generation may have persisted evidence without the reservation
          discipline) and commit it the same way.

        ``None`` means no uncommitted attempt evidence exists and normal
        retry delivery may proceed.
        """
        receipts = await storage.list_receipts_for_delivery(delivery_identity(item))
        if item.active_attempt is not None:
            evidence = self._retry_attempt_evidence(receipts, item, item.active_attempt)
            if evidence is not None:
                return await self._finalize_retry_evidence(
                    storage,
                    item,
                    retry_policy,
                    evidence=evidence,
                    unpersisted_failure_kind=None,
                    unpersisted_error_summary=None,
                    attempt_number=item.active_attempt,
                    now=now,
                )
            attempt_number = item.active_attempt
            executor = RetryExecutor(retry_policy)
            error_summary = _AMBIGUOUS_DISPATCH_ERROR

            if executor.is_exhausted(attempt_number):
                committed = await storage.mark_outbox_dead_lettered(
                    item.outbox_id,
                    failure_kind="retry_exhausted",
                    failure_kind_detail=_AMBIGUOUS_DISPATCH_FAILURE_DETAIL,
                    error_summary=error_summary,
                    attempt_number=attempt_number,
                    expected_worker_id=item.worker_id,
                )
                self._require_retry_commit(
                    committed,
                    item,
                    transition="dead_lettered",
                    attempt_number=attempt_number,
                )
                self._log.warning(
                    "Retry reservation %d for outbox %s was reclaimed without "
                    "durable evidence; consuming the ambiguous attempt and "
                    "dead-lettering because the retry budget is exhausted",
                    attempt_number,
                    item.outbox_id,
                )
                return RetryAttemptFinalization(
                    outcome="dead_lettered",
                    receipt_id=None,
                    failure_kind="retry_exhausted",
                    attempt_number=attempt_number,
                )

            next_attempt_at = (
                now or datetime.now(timezone.utc)
            ) + executor.compute_backoff(attempt_number)
            committed = await storage.mark_outbox_retry_wait(
                item.outbox_id,
                next_attempt_at=next_attempt_at.isoformat(),
                failure_kind=_AMBIGUOUS_DISPATCH_FAILURE_KIND,
                failure_kind_detail=_AMBIGUOUS_DISPATCH_FAILURE_DETAIL,
                error_summary=error_summary,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="retry_wait",
                attempt_number=attempt_number,
            )
            self._log.warning(
                "Retry reservation %d for outbox %s was reclaimed without "
                "durable evidence; consuming the ambiguous attempt before "
                "any later dispatch",
                attempt_number,
                item.outbox_id,
            )
            return RetryAttemptFinalization(
                outcome="retry_wait",
                receipt_id=None,
                failure_kind=_AMBIGUOUS_DISPATCH_FAILURE_KIND,
                attempt_number=attempt_number,
                next_retry_at=next_attempt_at,
            )

        attempt_number = item.attempt_number + 1
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
            attempt_number=attempt_number,
            now=now,
        )

    async def finalize_retry_attempt_error(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        error: Exception,
        attempt_number: int | None = None,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization:
        """Reconcile durable evidence after a retry delivery raises.

        This is the retry failure-classification authority.  It selects only
        evidence attributable to the current outbox attempt, treats durable
        ``queued``/``sent`` evidence as acceptance even when a later
        persistence step raised, honours existing terminal lifecycle evidence,
        terminates non-retryable failures immediately, and otherwise commits
        the retry-wait transition.

        *attempt_number* is the reserved attempt the dispatch ran under;
        when omitted the row's durable reservation (or the lineage rule)
        resolves it.

        Storage errors are intentionally not swallowed.  If evidence lookup
        or the selected outbox transition cannot be persisted, the worker must
        not report a lifecycle state that storage did not commit; the claimed
        row remains recoverable through its lease-expiry path, and claim
        reconciliation will repair persisted attempt evidence before resend.
        """
        resolved_attempt = self._resolve_retry_attempt(item, attempt_number)
        receipts = await storage.list_receipts_for_delivery(delivery_identity(item))
        evidence = self._retry_attempt_evidence(receipts, item, resolved_attempt)
        failure_kind = self._classify_retry_exception(error)
        error_summary = f"{type(error).__name__}: {error}"
        return await self._finalize_retry_evidence(
            storage,
            item,
            retry_policy,
            evidence=evidence,
            unpersisted_failure_kind=failure_kind,
            unpersisted_error_summary=error_summary,
            attempt_number=resolved_attempt,
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
        committed = await storage.mark_outbox_retry_wait(
            item.outbox_id,
            next_attempt_at=next_attempt_at.isoformat(),
            receipt_id=receipt_id,
            failure_kind=failure_kind,
            error_summary=error_summary,
            attempt_number=attempt_number,
            expected_worker_id=item.worker_id,
        )
        self._require_retry_commit(
            committed,
            item,
            transition="retry_wait",
            attempt_number=attempt_number,
        )
        return next_attempt_at

    async def finalize_retry_success(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        receipt: DeliveryReceipt,
    ) -> bool:
        """Commit a queued, sent, or suppressed retry receipt to the outbox.

        Return ``True`` for queued or sent, and ``False`` for suppressed
        (which abandons the row). Raise :class:`RetryAttemptCommitRejected`
        if the guarded transition fails, or :class:`ValueError` for another
        receipt status.
        """
        if receipt.status == "queued":
            committed = await storage.mark_outbox_queued(
                item.outbox_id,
                receipt_id=receipt.receipt_id,
                attempt_number=receipt.attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="queued",
                attempt_number=receipt.attempt_number,
            )
            return True
        if receipt.status == "sent":
            committed = await storage.mark_outbox_sent(
                item.outbox_id,
                receipt_id=receipt.receipt_id,
                attempt_number=receipt.attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="sent",
                attempt_number=receipt.attempt_number,
            )
            return True
        if receipt.status == "suppressed":
            committed = await storage.mark_outbox_abandoned(
                item.outbox_id,
                error_summary=receipt.error,
                receipt_id=receipt.receipt_id,
                attempt_number=receipt.attempt_number,
                expected_worker_id=item.worker_id,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="abandoned",
                attempt_number=receipt.attempt_number,
            )
            return False
        raise ValueError(
            "Retry success finalization requires a queued, sent, or suppressed "
            f"receipt; got {receipt.status!r}"
        )

    async def reconcile_retry_success_commit_rejection(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        receipt: DeliveryReceipt,
    ) -> RetryAttemptFinalization | None:
        """Resolve a same-attempt outcome that beat retry success finalization.

        Queue-backed adapters can emit a terminal callback immediately after
        returning a ``queued`` receipt.  That callback may consume the live
        reservation and clear worker ownership before ``RetryWorker`` commits
        its own queued transition.  A rejected CAS is therefore not always a
        stale worker: when the authoritative row is already terminal at the
        exact same attempt, runtime observability may project that committed
        outcome instead of reporting a superseded transition.  Reconciliation
        additionally requires the outbox to point at durable receipt evidence
        for this exact outbox/attempt generation; a terminal row with no
        committed receipt is not attributed to the transport result.

        Returns ``None`` when the rejection is genuinely stale or ambiguous.
        The worker must not infer durable state on its own.
        """
        if receipt.status not in {"queued", "sent"}:
            return None
        current = await storage.get_outbox_item(item.outbox_id)
        if current is None:
            return None
        if current.attempt_number != receipt.attempt_number:
            return None
        if current.active_attempt is not None or current.receipt_id is None:
            return None

        receipts = await storage.list_receipts_for_delivery(delivery_identity(item))
        committed_receipt = committed_receipt_for_outbox(current, receipts)
        if committed_receipt is None:
            return None

        if current.status == "sent" and committed_receipt.status == "sent":
            return RetryAttemptFinalization(
                outcome="accepted",
                receipt_id=committed_receipt.receipt_id,
                failure_kind=None,
                attempt_number=receipt.attempt_number,
            )
        if current.status == "dead_lettered" and committed_receipt.status in {
            "failed",
            "dead_lettered",
        }:
            return RetryAttemptFinalization(
                outcome="dead_lettered",
                receipt_id=committed_receipt.receipt_id,
                failure_kind=current.failure_kind,
                attempt_number=receipt.attempt_number,
            )
        if current.status == "cancelled" and committed_receipt.status == "cancelled":
            return RetryAttemptFinalization(
                outcome="cancelled",
                receipt_id=committed_receipt.receipt_id,
                failure_kind=current.failure_kind,
                attempt_number=receipt.attempt_number,
            )
        if current.status == "abandoned" and committed_receipt.status in {
            "failed",
            "abandoned",
            "suppressed",
        }:
            return RetryAttemptFinalization(
                outcome="abandoned",
                receipt_id=committed_receipt.receipt_id,
                failure_kind=current.failure_kind,
                attempt_number=receipt.attempt_number,
            )
        return None

    # -- Outbox finalization ------------------------------------------------

    async def finalize_outbox_outcome(
        self,
        storage: DeliveryLifecycleStorage,
        *,
        outbox_id: str | None,
        outbox_created: bool,
        evidence: DeliveryExecutionEvidence,
        retry_policy: RetryPolicy | None,
        reserved_attempt_number: int | None = None,
        expected_worker_id: str | None = None,
    ) -> bool | None:
        """Commit mutable outbox state from validated execution evidence.

        Attempt receipts represent dispatch generations. Lifecycle receipts
        represent state transitions caused by those attempts and, when present,
        are the only receipts eligible to become terminal outbox authority.
        The structured evidence object validates cross-receipt lineage before
        this method is reached.
        """
        if outbox_id is None or not outbox_created:
            return None

        attempt = evidence.attempt_receipt
        authority = evidence.authority_receipt
        failure_kind = evidence.failure_kind
        error_summary = evidence.error[:512] if evidence.error else None

        # Derive failure classification from persisted attempt evidence only as
        # a compatibility fallback. Normal target execution always supplies the
        # typed value on DeliveryExecutionEvidence.
        if failure_kind is None and attempt is not None and attempt.failure_kind:
            try:
                failure_kind = DeliveryFailureKind(attempt.failure_kind)
            except ValueError:
                failure_kind = None

        # Normalize terminal failed-attempt evidence at the lifecycle boundary.
        # Producers may provide an explicit lifecycle receipt, but callers that
        # only know the failed attempt still converge on the same terminal
        # shape here. The receipt is constructed now and inserted atomically
        # with the outbox transition below.
        if (
            authority is None
            and attempt is not None
            and attempt.status == "failed"
            and failure_kind is not None
            and self.is_terminal_failure(
                failure_kind,
                next_retry_at=attempt.next_retry_at,
            )
        ):
            authority = self.build_terminal_lifecycle_receipt(
                attempt,
                status="dead_lettered",
                error=evidence.error,
                failure_kind=failure_kind.value,
            )

        try:
            committed: bool | None = None

            if authority is not None:
                if authority.status not in {
                    "dead_lettered",
                    "cancelled",
                    "abandoned",
                }:
                    raise ValueError(
                        f"unsupported outbox lifecycle authority status: "
                        f"{authority.status!r}"
                    )
                committed = await storage.finalize_outbox_terminal(
                    TerminalOutboxFinalization(
                        lifecycle_receipt=authority,
                        expected_worker_id=expected_worker_id,
                    )
                )

            elif attempt is not None and attempt.status == "queued":
                committed = await storage.mark_outbox_queued(
                    outbox_id,
                    receipt_id=attempt.receipt_id,
                    attempt_number=attempt.attempt_number,
                    expected_worker_id=expected_worker_id,
                )
            elif attempt is not None and attempt.status == "sent":
                committed = await storage.mark_outbox_sent(
                    outbox_id,
                    receipt_id=attempt.receipt_id,
                    attempt_number=attempt.attempt_number,
                    expected_worker_id=expected_worker_id,
                )
            elif failure_kind is not None:
                receipt_id = attempt.receipt_id if attempt is not None else None
                if (
                    failure_kind.is_retryable
                    and retry_policy is not None
                    and attempt is not None
                    and attempt.next_retry_at is not None
                ):
                    committed = await storage.mark_outbox_retry_wait(
                        outbox_id,
                        next_attempt_at=attempt.next_retry_at.isoformat(),
                        receipt_id=receipt_id,
                        failure_kind=failure_kind.value,
                        error_summary=error_summary,
                        attempt_number=attempt.attempt_number,
                        expected_worker_id=expected_worker_id,
                    )
                elif attempt is None and failure_kind.is_retryable and retry_policy:
                    # Failure occurred before durable attempt evidence could be
                    # appended. The durable dispatch reservation is still the
                    # attempt identity for exhaustion, backoff, and CAS. The
                    # coordinator supplies it directly; direct lifecycle tests
                    # and defensive callers may recover it from the row.
                    retry_attempt = reserved_attempt_number
                    if retry_attempt is None:
                        outbox_item = await storage.get_outbox_item(outbox_id)
                        retry_attempt = (
                            self.effective_attempt(outbox_item) if outbox_item else 1
                        )
                    executor = RetryExecutor(retry_policy)
                    if not executor.is_exhausted(retry_attempt):
                        next_attempt_at = (
                            datetime.now(timezone.utc)
                            + executor.compute_backoff(retry_attempt)
                        ).isoformat()
                        committed = await storage.mark_outbox_retry_wait(
                            outbox_id,
                            next_attempt_at=next_attempt_at,
                            receipt_id=None,
                            failure_kind=failure_kind.value,
                            error_summary=error_summary,
                            attempt_number=retry_attempt,
                            expected_worker_id=expected_worker_id,
                        )
                    else:
                        committed = await storage.mark_outbox_dead_lettered(
                            outbox_id,
                            receipt_id=None,
                            failure_kind=failure_kind.value,
                            error_summary=error_summary,
                            attempt_number=retry_attempt,
                            expected_worker_id=expected_worker_id,
                        )
                else:
                    # A terminal execution should normally carry lifecycle
                    # authority. This fallback protects generic exception paths
                    # that failed before evidence persistence.
                    terminal_attempt = reserved_attempt_number
                    if attempt is None and terminal_attempt is None:
                        outbox_item = await storage.get_outbox_item(outbox_id)
                        terminal_attempt = (
                            self.effective_attempt(outbox_item) if outbox_item else None
                        )
                    committed = await storage.mark_outbox_dead_lettered(
                        outbox_id,
                        receipt_id=receipt_id,
                        failure_kind=failure_kind.value,
                        error_summary=error_summary,
                        attempt_number=(
                            attempt.attempt_number
                            if attempt is not None
                            else terminal_attempt
                        ),
                        expected_worker_id=expected_worker_id,
                    )

            if committed is False:
                current = evidence.current_receipt
                self._log.warning(
                    "Outbox finalization rejected by ownership/state guard: "
                    "outbox_id=%s receipt_id=%s expected_worker_id=%s; "
                    "receipt remains append-only historical evidence",
                    outbox_id,
                    current.receipt_id if current is not None else None,
                    expected_worker_id,
                )
            return committed
        except Exception:
            self._log.exception(
                "Failed to update outbox %s after delivery",
                outbox_id,
            )
            return False
