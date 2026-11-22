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

Adapter hand-off dispositions
    ``transport_handoff``, ``deferred``. These are process-local adapter facts;
    durable receipt/outbox states remain the lifecycle authority.

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
    - Outbox: ``pending`` / ``retry_wait`` / expired ``in_progress`` ->
      ``in_progress`` (lease acquisition)
    - Outbox: ``in_progress`` -> ``queued`` / ``sent`` / ``retry_wait`` /
      ``dead_lettered`` (delivery outcome)
    - Outbox: ``queued`` -> ``sent`` (callback confirmation)

The queued outbox disposition
    A queued receipt records only adapter-local queue acceptance, so a
    ``queued`` outbox row is the durable, content-free
    confirmation-pending disposition: never claimable, never reclaimed
    for redispatch, and never counted as delivery success.  It leaves
    ``queued`` only through evidence-driven settlement (deferred
    completion, a provenance-matched ``sent`` receipt, or terminal
    lifecycle evidence) or the explicit operator confirmation-pending
    resolution.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol, TypedDict, runtime_checkable

from medre.core.contracts.adapter import MAX_ADAPTER_RETRY_AFTER_SECONDS
from medre.core.contracts.delivery import (
    DeferredHandoffCompleted,
    PostHandoffObservation,
)
from medre.core.delivery_authority import (
    DeliveryIdentity,
    committed_receipt_for_outbox,
    delivery_attempt_provenance_mismatch,
    delivery_attempt_receipt_provenance_mismatch,
    delivery_identity,
    dispatch_source_matches_attempt,
    effective_generation,
    queued_receipts_for_attempt,
    receipts_for_attempt,
)
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.engine.pipeline.delivery_state import (
    is_terminal_outbox_status as _is_terminal_outbox_status,
)
from medre.core.engine.pipeline.delivery_state import (
    is_valid_queued_to_sent_transition as _is_valid_queued_to_sent_transition,
)
from medre.core.engine.pipeline.receipt_factory import (
    build_delivery_receipt,
    capability_receipt_fields,
    plan_capability_receipt_fields,
)
from medre.core.engine.pipeline.retry_plan import resolve_capability_fallback
from medre.core.events.canonical import (
    DeliveryObservation,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.delivery import DeliverySource
from medre.core.evidence.handoff_resolution import (
    HANDOFF_NOT_SUBMITTED_DETAIL,
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    UncertainHandoffResolution,
    UncertainNativeHandoffResolver,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.storage.backend import (
    DeferredHandoffFinalization,
    DeliveryOutboxItem,
    OutboxDispatchSnapshot,
    OutboxRouteRetryBounds,
    StorageError,
    TerminalOutboxFinalization,
)


class RetryReceiptFields(TypedDict):
    """Retry-policy fields for receipt construction.

    Returned by :meth:`DeliveryLifecycleService.extract_retry_fields` so
    ``**``-unpacking at :func:`build_delivery_receipt` call sites maps
    each key to its matching keyword parameter.
    """

    retry_max_attempts: int | None
    retry_backoff_base: float | None
    retry_max_delay: float | None
    retry_jitter: bool | None


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
_NOT_SUBMITTED_DISPATCH_ERROR = (
    "reserved retry dispatch was reclaimed after retained evidence proved the "
    "grant fence never authorized a native submission; the attempt identity is "
    "consumed"
)
_UNCERTAIN_HANDOFF_ERROR_PREFIX = (
    "reserved retry dispatch was reclaimed with an unresolved native handoff; "
    "the attempt identity is consumed and the uncertainty stays explicit"
)


class _ObservationGuard(TypedDict, total=False):
    expected_observation_count: int
    expected_receipt_count: int
    expected_checkpoint_revision: int
    expected_observation_scope_revision: int


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

    async def list_receipts_for_outbox(
        self,
        outbox_id: str,
    ) -> list[DeliveryReceipt]:
        """List immutable receipt history for one exact outbox ID."""
        ...

    async def finalize_deferred_handoff(
        self,
        command: DeferredHandoffFinalization,
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

    async def get_outbox_dispatch_context(
        self,
        outbox_id: str,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
    ) -> OutboxDispatchSnapshot | None:
        """Read the frozen dispatch context under the exact outbox identity."""
        ...

    async def get_outbox_route_retry_bounds(
        self,
        outbox_id: str,
    ) -> OutboxRouteRetryBounds | None:
        """Read the persisted admission-time retry bounds for one generation.

        Returns ``None`` for generations admitted without resolved route
        bounds.  The bounds are content-free facts, not send authority.
        """
        ...

    async def release_outbox_claim(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        release_status: str = "pending",
    ) -> None:
        """Release only an owned, unreserved claim under atomic fencing."""
        ...

    async def defer_unsubmitted_outbox_attempt(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        attempt_number: int,
        next_attempt_at: str,
    ) -> bool:
        """Release a reservation that the live owner has not exposed to a provider.

        The caller MUST know that no native submission or provider callback has
        been authorized for this attempt. Never use this for crash recovery.
        A mismatched owner, generation or existing attempt evidence refuses
        release. Prior finalized attempt and receipt evidence are unchanged.
        """
        ...

    async def reserve_outbox_attempt(
        self,
        outbox_id: str,
        worker_id: str,
        from_attempt: int,
        *,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
    ) -> int | None:
        """Reserve the next attempt for the current claim owner.

        The successful reservation preserves replay origin, records ``live``
        from attempt zero, and otherwise records ``retry``, as provenance
        for callbacks that can beat receipt append. An expected observation count is an atomic guard on the
        exact finalized ``from_attempt`` (zero observations is valid).
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
        *,
        clear_receipt_id: bool = False,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
    ) -> bool:
        """Commit a sent outcome if the supplied attempt and owner still match.

        ``clear_receipt_id`` removes any stale receipt pointer on the row in
        the same transaction as the settlement; it is used by settlements that
        commit without a receipt.

        Return ``False`` when the guarded transition did not commit.
        """
        ...

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
        expected_worker_id: str | None = None,
        *,
        expected_receipt_count: int | None = None,
    ) -> bool:
        """Commit queue acceptance against exact attempt, owner and receipt set.

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
        *,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
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
        *,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
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
        *,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
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
        "queued",
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
    def extract_retry_fields(plan: DeliveryPlan) -> RetryReceiptFields:
        """Extract retry policy fields for receipt construction.

        Parameters
        ----------
        plan:
            The delivery plan whose retry policy (if any) is extracted.

        Returns
        -------
        RetryReceiptFields
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
        Retry-policy provenance is inherited from the causative receipt.
        Rendering evidence remains on its immutable sent/queued receipt and is
        reachable through the parent chain rather than duplicated onto a
        terminal lifecycle receipt.
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
            capability_level=previous_receipt.capability_level,
            capability_field=previous_receipt.capability_field,
            capability_reason=previous_receipt.capability_reason,
            delivery_strategy=previous_receipt.delivery_strategy,
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
            # Shared plan sanitizer: dead-letter evidence stays persistable
            # even when a malformed plan carries unknown capability values.
            **plan_capability_receipt_fields(plan),
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
        capability_level: str | None = None,
        capability_field: str | None = None,
        capability_reason: str | None = None,
        delivery_strategy: str | None = None,
        source: DeliverySource = "live",
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
            capability_level=capability_level,
            capability_field=capability_field,
            capability_reason=capability_reason,
            delivery_strategy=delivery_strategy,
            source=source,
            replay_run_id=replay_run_id,
        )
        await storage.append_receipt(receipt)
        return receipt

    # -- Post-handoff observations ------------------------------------------

    async def record_post_handoff_observation(
        self,
        storage: DeliveryLifecycleStorage,
        feedback: PostHandoffObservation,
        now: datetime,
    ) -> bool:
        """Persist append-only transport evidence for one exact attempt.

        The immutable attempt envelope is the sole correlation authority.
        Observations never reopen or rewrite receipt/outbox lifecycle state.
        """
        provenance = feedback.attempt_provenance
        outbox = await storage.get_outbox_item(provenance.outbox_id)
        if outbox is None:
            self._log.warning(
                "Rejecting post-handoff observation for missing outbox row: "
                "outbox_id=%s",
                provenance.outbox_id,
            )
            return False

        mismatch = delivery_attempt_provenance_mismatch(provenance, outbox)
        if mismatch is not None:
            self._log.warning(
                "Rejecting post-handoff observation with contradictory attempt "
                "provenance: outbox_id=%s %s",
                provenance.outbox_id,
                mismatch,
            )
            return False
        eligible_uncertain_backoff = (
            outbox.status == "retry_wait"
            and outbox.active_attempt is None
            and outbox.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL
        )
        if outbox.status not in {"in_progress", "queued", "sent"} and not (
            eligible_uncertain_backoff
        ):
            self._log.warning(
                "Rejecting post-handoff observation for ineligible outbox state: "
                "outbox_id=%s status=%s attempt=%s state=%s",
                provenance.outbox_id,
                outbox.status,
                provenance.attempt_number,
                feedback.state,
            )
            return False

        try:
            receipts = await storage.list_receipts_for_outbox(provenance.outbox_id)
        except Exception:
            self._log.exception(
                "Failed to list attempt receipt history for post-handoff "
                "observation: outbox_id=%s",
                provenance.outbox_id,
            )
            return False
        for receipt in receipts_for_attempt(provenance, receipts):
            receipt_mismatch = delivery_attempt_receipt_provenance_mismatch(
                provenance, receipt
            )
            if receipt_mismatch is not None:
                self._log.warning(
                    "Rejecting post-handoff observation with contradictory "
                    "immutable receipt provenance: outbox_id=%s attempt=%d %s",
                    provenance.outbox_id,
                    provenance.attempt_number,
                    receipt_mismatch,
                )
                return False

        identity = "\x1f".join(
            (
                provenance.outbox_id,
                str(provenance.attempt_number),
                provenance.target_adapter,
                feedback.native_message_id or "",
                feedback.state,
                feedback.confirmation_level,
            )
        )
        observation = DeliveryObservation(
            observation_id="obs-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            native_channel_id=feedback.native_channel_id,
            outbox_id=provenance.outbox_id,
            attempt_number=provenance.attempt_number,
            adapter_message_id=feedback.native_message_id,
            state=feedback.state,
            confirmation_level=feedback.confirmation_level,
            error=feedback.error,
            metadata=dict(feedback.metadata),
            observed_at=now,
        )
        return await storage.append_delivery_observation(observation)

    # -- Atomic queued->sent finalization ------------------------------------

    async def finalize_deferred_handoff(
        self,
        storage: DeliveryLifecycleStorage,
        feedback: DeferredHandoffCompleted,
        now: datetime,
    ) -> bool:
        """Atomically finalize a previously deferred transport hand-off.

        Provenance identifies the exact durable generation. Immutable attempt
        evidence, when already present, must agree with that envelope. A native
        reference is committed only when the transport supplied a message ID.
        """
        provenance = feedback.attempt_provenance
        handoff = feedback.handoff
        outbox = await storage.get_outbox_item(provenance.outbox_id)
        if outbox is None:
            self._log.warning(
                "Deferred hand-off completion rejected: outbox_id=%s not found",
                provenance.outbox_id,
            )
            return False

        mismatch = delivery_attempt_provenance_mismatch(provenance, outbox)
        if mismatch is not None:
            self._log.warning(
                "Deferred hand-off completion rejected: contradictory attempt "
                "provenance for outbox_id=%s: %s",
                provenance.outbox_id,
                mismatch,
            )
            return False
        if outbox.status not in {"queued", "in_progress"}:
            self._log.warning(
                "Deferred hand-off completion rejected: outbox_id=%s status=%s",
                provenance.outbox_id,
                outbox.status,
            )
            return False

        try:
            existing = await storage.list_receipts_for_outbox(provenance.outbox_id)
        except Exception:
            self._log.exception(
                "Failed to list receipt history for deferred hand-off completion: "
                "outbox_id=%s",
                provenance.outbox_id,
            )
            return False

        attempt_receipts = receipts_for_attempt(provenance, existing)
        for candidate in attempt_receipts:
            receipt_mismatch = delivery_attempt_receipt_provenance_mismatch(
                provenance, candidate
            )
            if receipt_mismatch is not None:
                self._log.warning(
                    "Deferred hand-off completion rejected: immutable receipt "
                    "provenance contradicts feedback for outbox_id=%s attempt=%d: %s",
                    provenance.outbox_id,
                    provenance.attempt_number,
                    receipt_mismatch,
                )
                return False

        queued_matches = queued_receipts_for_attempt(provenance, attempt_receipts)
        queued_receipt = (
            max(
                queued_matches,
                key=lambda receipt: (
                    receipt.sequence or 0,
                    receipt.created_at.isoformat(),
                    receipt.receipt_id,
                ),
            )
            if queued_matches
            else None
        )
        if queued_receipt is not None and not _is_valid_queued_to_sent_transition(
            queued_receipt.status
        ):
            self._log.warning(
                "Deferred hand-off parent receipt %s has status=%s; "
                "cannot transition to sent",
                queued_receipt.receipt_id,
                queued_receipt.status,
            )
            return False
        if queued_receipt is None:
            if outbox.status == "queued":
                self._log.warning(
                    "Deferred hand-off completion rejected: queued outbox has no "
                    "matching immutable queued attempt evidence: outbox_id=%s "
                    "attempt=%d",
                    provenance.outbox_id,
                    provenance.attempt_number,
                )
                return False
            self._log.debug(
                "Deferred hand-off completion arrived before queued attempt "
                "evidence; finalizing from immutable attempt provenance: "
                "outbox_id=%s attempt=%d",
                provenance.outbox_id,
                provenance.attempt_number,
            )

        # Receipt capability fallback: durable inline metadata is the
        # historical source when no queued attempt evidence exists yet.
        # Post-change rows keep those frozen decision facts in the owned work
        # snapshot; a failed snapshot read leaves the fallback empty rather
        # than blocking persistence of an outcome that already happened.
        capability_fallback = await resolve_capability_fallback(
            storage, outbox, self._log
        )

        supplemental = build_delivery_receipt(
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            route_id=(
                queued_receipt.route_id
                if queued_receipt is not None
                else outbox.route_id
            ),
            status="sent",
            adapter_message_id=handoff.native_message_id,
            created_at=now,
            attempt_number=provenance.attempt_number,
            parent_receipt_id=(
                queued_receipt.receipt_id
                if queued_receipt is not None
                else outbox.receipt_id
            ),
            source=provenance.source,
            replay_run_id=provenance.replay_run_id,
            retry_max_attempts=(
                queued_receipt.retry_max_attempts
                if queued_receipt is not None
                else None
            ),
            retry_backoff_base=(
                queued_receipt.retry_backoff_base
                if queued_receipt is not None
                else None
            ),
            retry_max_delay=(
                queued_receipt.retry_max_delay if queued_receipt is not None else None
            ),
            retry_jitter=(
                queued_receipt.retry_jitter if queued_receipt is not None else None
            ),
            **capability_receipt_fields(
                receipt=queued_receipt,
                metadata=capability_fallback,
            ),
            rendering_evidence=(
                queued_receipt.rendering_evidence
                if queued_receipt is not None
                else None
            ),
            outbox_id=provenance.outbox_id,
            confirmation_level=handoff.confirmation_level,
        )
        native_ref = None
        if handoff.native_message_id is not None:
            native_ref = NativeMessageRef(
                id=f"nref-outbound-{uuid.uuid4()}",
                event_id=provenance.event_id,
                adapter=provenance.target_adapter,
                native_channel_id=handoff.native_channel_id,
                native_message_id=handoff.native_message_id,
                native_thread_id=handoff.native_thread_id,
                native_relation_id=handoff.native_relation_id,
                direction="outbound",
                metadata=dict(handoff.metadata),
                created_at=now,
            )

        committed = await storage.finalize_deferred_handoff(
            DeferredHandoffFinalization(
                receipt=supplemental,
                native_ref=native_ref,
            )
        )
        if not committed:
            self._log.warning(
                "Deferred hand-off completion lost its outbox guard: "
                "outbox_id=%s event_id=%s adapter=%s attempt=%d",
                provenance.outbox_id,
                provenance.event_id,
                provenance.target_adapter,
                provenance.attempt_number,
            )
        return committed

    # -- Retry-worker outbox transitions -----------------------------------

    @staticmethod
    def _receipt_provenance_conflicts(
        item: DeliveryOutboxItem,
        attempt_number: int,
        receipts: list[DeliveryReceipt],
    ) -> bool:
        """Whether any receipt contradicts the claimed delivery identity.

        Receipts with contradictory event, plan, adapter, destination,
        origin, or replay-run provenance can never settle an attempt. The
        check covers the whole receipt set whose count fences a settlement,
        not only the accepted receipt the settlement would name.
        """
        identity = DeliveryIdentity(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel,
        )
        return any(
            receipt.outbox_id != item.outbox_id
            or receipt.attempt_number != attempt_number
            or delivery_identity(receipt) != identity
            or (receipt.replay_run_id or None) != (item.replay_run_id or None)
            or not dispatch_source_matches_attempt(
                item.dispatch_source or "live", receipt.source, attempt_number
            )
            for receipt in receipts
        )

    async def reconcile_content_denied_handoff(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        handoff_resolver: UncertainNativeHandoffResolver,
        *,
        attempt_receipts: list[DeliveryReceipt],
    ) -> RetryAttemptFinalization | UncertainHandoffResolution | None:
        """Reconcile already-authorized acceptance without restoring send authority.

        Called when content has expired and dispatch context is gone. The
        absence of the old uncertainty label does not erase exact-attempt
        evidence. Positive native evidence settles only without a conflicting
        receipt, using the existing observation/receipt/checkpoint CAS.

        *attempt_receipts* is the exact-attempt receipt set the caller
        reconciled; its length fences the settlement write and every
        receipt's provenance is validated before positive evidence can
        settle. A receipt contradicting the claimed delivery identity
        blocks the settlement: the caller leaves the claim for
        conservative recovery.
        """
        attempt = item.active_attempt or item.attempt_number
        if attempt < 1:
            return None
        resolution = await handoff_resolver.resolve(item, attempt)
        if resolution.observation_count is None:
            return resolution
        if resolution.verdict == "delivered_with_evidence":
            if self._receipt_provenance_conflicts(item, attempt, attempt_receipts):
                # A receipt contradicting the claimed delivery identity —
                # event, plan, adapter, destination, origin, or replay run —
                # can never settle this attempt. Positive native evidence
                # does not commit over contradictory evidence.
                return resolution
            # Positive exact-attempt evidence wins over historical failed
            # receipts. A sent/queued receipt is settled from the receipt
            # itself before this path; either way the counter is fenced.
            return await self._settle_resolved_handoff(
                storage,
                item,
                attempt,
                resolution,
                expected_receipt_count=len(attempt_receipts),
            )
        return resolution

    async def abandon_retry_outbox(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        *,
        error_summary: str,
        expected_observation_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
        expected_receipt_count: int | None = None,
    ) -> None:
        """Persist terminal abandonment only against unchanged attempt evidence."""
        if expected_observation_count is not None and expected_receipt_count is None:
            # An observation-only guard historically also required zero
            # receipts. Preserve that implicit acceptance fence for callers
            # that do not supply the observed receipt count directly.
            expected_receipt_count = 0
        evidence_guard: _ObservationGuard = {}
        if expected_observation_count is not None:
            evidence_guard["expected_observation_count"] = expected_observation_count
        if expected_receipt_count is not None:
            evidence_guard["expected_receipt_count"] = expected_receipt_count
        if expected_checkpoint_revision is not None:
            evidence_guard["expected_checkpoint_revision"] = (
                expected_checkpoint_revision
            )
        if expected_observation_scope_revision is not None:
            evidence_guard["expected_observation_scope_revision"] = (
                expected_observation_scope_revision
            )
        committed = await storage.mark_outbox_abandoned(
            item.outbox_id,
            error_summary=error_summary,
            attempt_number=(
                item.active_attempt
                if item.active_attempt is not None
                else (item.attempt_number if evidence_guard else None)
            ),
            expected_worker_id=item.worker_id,
            **evidence_guard,
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
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
    ) -> RetryAttemptFinalization:
        """Commit the outbox state implied by one retry attempt's evidence.

        When the four ``expected_*`` evidence guards are supplied, the
        terminal ``dead_lettered`` write carries them: it commits only if the
        exact attempt still shows the evidence revision the caller read. The
        retry-wait and non-terminal paths stay unfenced so a stale guard
        never blocks a self-healing reschedule.
        """
        attempt_number = self._resolve_retry_attempt(item, attempt_number)

        if evidence is not None and evidence.status in {"queued", "sent", "suppressed"}:
            accepted = await self.finalize_retry_success(storage, item, evidence)
            # A queued receipt is adapter-local acceptance only: it is the
            # third observability outcome, never delivery success. The row
            # rests in the durable confirmation-pending disposition and no
            # success/failure counter counts it.
            outcome: Literal[
                "accepted",
                "queued",
                "suppressed",
            ] = (
                "suppressed"
                if not accepted
                else ("queued" if evidence.status == "queued" else "accepted")
            )
            return RetryAttemptFinalization(
                outcome=outcome,
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
                        expected_observation_count=expected_observation_count,
                        expected_receipt_count=expected_receipt_count,
                        expected_checkpoint_revision=expected_checkpoint_revision,
                        expected_observation_scope_revision=(
                            expected_observation_scope_revision
                        ),
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
        *,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
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
        evidence_guard: _ObservationGuard = {}
        if expected_observation_count is not None:
            evidence_guard["expected_observation_count"] = expected_observation_count
        if expected_receipt_count is not None:
            evidence_guard["expected_receipt_count"] = expected_receipt_count
        if expected_checkpoint_revision is not None:
            evidence_guard["expected_checkpoint_revision"] = (
                expected_checkpoint_revision
            )
        if expected_observation_scope_revision is not None:
            evidence_guard["expected_observation_scope_revision"] = (
                expected_observation_scope_revision
            )
        return await storage.reserve_outbox_attempt(
            item.outbox_id,
            item.worker_id,
            item.attempt_number,
            **evidence_guard,
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

    async def list_attempt_receipts(
        self,
        storage: DeliveryLifecycleStorage,
        outbox_id: str,
        attempt_number: int,
    ) -> list[DeliveryReceipt]:
        """List the immutable receipt history for one exact outbox attempt.

        The exact-attempt view backs evidence-fenced terminal decisions:
        receipts from any other attempt never inflate the observed count.
        """
        receipts = await storage.list_receipts_for_outbox(outbox_id)
        return [
            receipt for receipt in receipts if receipt.attempt_number == attempt_number
        ]

    async def reconcile_retry_claim(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        now: datetime | None = None,
        handoff_resolver: UncertainNativeHandoffResolver | None = None,
        expected_observation_count: int | None = None,
        expected_receipt_count: int | None = None,
        expected_checkpoint_revision: int | None = None,
        expected_observation_scope_revision: int | None = None,
    ) -> RetryAttemptFinalization | None:
        """Repair a claimed outbox row from already-persisted attempt evidence.

        This preflight closes the partial-persistence window where target
        delivery appended receipt evidence but the corresponding outbox
        transition failed.  Two cases exist:

        When the four ``expected_*`` evidence guards are supplied, a receipt
        finalization on a reserved attempt arms its terminal write with them:
        the write commits only if the exact attempt still shows the evidence
        revision the caller observed. The guards change nothing else.

        * The claimed row carries a durable attempt reservation
          (``active_attempt``): evidence for exactly that attempt means a
          prior worker dispatched it and died before finalization — commit
          the missing transition and return it; the caller MUST NOT invoke
          the transport again.  A reservation without evidence is ambiguous:
          the prior process may have died before transport invocation, or the
          transport may still have accepted the send while receipt persistence
          was lost.  Before consuming such an identity, an optional
          *handoff_resolver* runs the bounded uncertain-native-handoff
          remediation pass (see :mod:`medre.core.evidence.handoff_resolution`):
          evidence that the whole plan was handed off — or that a provider
          accepted the message — settles the obligation ``sent`` through the
          existing guarded transition without fabricating a receipt; evidence
          that the grant fence never committed keeps the normal retry
          semantics under an evidence-backed diagnostic; anything else stays
          unresolved and is consumed conservatively with the explicit
          ``uncertain_native_handoff`` operator-visible detail for the bounded
          pre-dispatch evidence recheck (:meth:`recheck_last_uncertain_handoff`) that
          runs before a newer attempt dispatches, even when no evidence was
          visible on the first read. Without a resolver, the identity is consumed as
          a failed attempt exactly as before and the row moves to
          ``retry_wait`` (or ``dead_lettered`` when the attempt budget is
          exhausted).  A later dispatch MUST use a strictly newer attempt
          number; retry recovery never reuses a reserved dispatch identity.
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
                    expected_observation_count=expected_observation_count,
                    expected_receipt_count=expected_receipt_count,
                    expected_checkpoint_revision=expected_checkpoint_revision,
                    expected_observation_scope_revision=(
                        expected_observation_scope_revision
                    ),
                )
            attempt_number = item.active_attempt
            executor = RetryExecutor(retry_policy)
            error_summary = _AMBIGUOUS_DISPATCH_ERROR
            failure_detail = _AMBIGUOUS_DISPATCH_FAILURE_DETAIL
            evidence_guard: _ObservationGuard = {}

            if handoff_resolver is not None:
                resolution = await handoff_resolver.resolve(item, attempt_number)
                if resolution.observation_count is None:
                    raise StorageError(
                        "Cannot reconcile handoff without an evidence revision"
                    )
                evidence_guard["expected_receipt_count"] = 0
                if resolution.checkpoint_revision is not None:
                    evidence_guard["expected_checkpoint_revision"] = (
                        resolution.checkpoint_revision
                    )
                if resolution.verdict == "delivered_with_evidence":
                    return await self._settle_resolved_handoff(
                        storage, item, attempt_number, resolution
                    )
                if resolution.observation_count is not None:
                    evidence_guard["expected_observation_count"] = (
                        resolution.observation_count
                    )
                if resolution.observation_scope_revision is not None:
                    evidence_guard["expected_observation_scope_revision"] = (
                        resolution.observation_scope_revision
                    )
                if resolution.verdict == "definitively_not_submitted":
                    failure_detail = HANDOFF_NOT_SUBMITTED_DETAIL
                    error_summary = _NOT_SUBMITTED_DISPATCH_ERROR
                else:
                    failure_detail = UNCERTAIN_NATIVE_HANDOFF_DETAIL
                    error_summary = (
                        f"{_UNCERTAIN_HANDOFF_ERROR_PREFIX} "
                        f"({resolution.summary()})"
                    )

            if executor.is_exhausted(attempt_number):
                committed = await storage.mark_outbox_dead_lettered(
                    item.outbox_id,
                    failure_kind="retry_exhausted",
                    failure_kind_detail=failure_detail,
                    error_summary=error_summary,
                    attempt_number=attempt_number,
                    expected_worker_id=item.worker_id,
                    **evidence_guard,
                )
                self._require_retry_commit(
                    committed,
                    item,
                    transition="dead_lettered",
                    attempt_number=attempt_number,
                )
                self._log.warning(
                    "Reclaimed retry reservation %d for outbox %s was "
                    "dead-lettered after exhausting the retry budget (%s)",
                    attempt_number,
                    item.outbox_id,
                    failure_detail,
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
                failure_kind_detail=failure_detail,
                error_summary=error_summary,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
                **evidence_guard,
            )
            self._require_retry_commit(
                committed,
                item,
                transition="retry_wait",
                attempt_number=attempt_number,
            )
            self._log.warning(
                "Reclaimed retry reservation %d for outbox %s was "
                "consumed before any later dispatch (%s)",
                attempt_number,
                item.outbox_id,
                failure_detail,
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

    async def settle_committed_acceptance(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        *,
        attempt_number: int,
        receipts: list[DeliveryReceipt],
    ) -> RetryAttemptFinalization:
        """Reconcile a committed receipt without promoting local queue admission.

        Content denial can follow a ``queued``/``sent`` receipt whose outbox
        transition never committed. Only ``sent`` proves completion; a
        ``queued`` receipt commits the row to the durable, content-free
        confirmation-pending disposition — never claimable, never
        redispatched — with deferred completion outstanding until evidence
        settles it or the operator resolves it. Every receipt's provenance
        is validated against the claimed delivery identity before any
        settlement commits.
        """
        if self._receipt_provenance_conflicts(item, attempt_number, receipts):
            raise StorageError(
                "Committed receipt provenance contradicts the claimed delivery attempt"
            )
        # Local queue admission is not a completed native handoff. A queued
        # receipt cannot close the outbox as sent or suppress its deferred
        # completion callback. Prefer a completed receipt if both exist.
        accepted = next((r for r in receipts if r.status == "sent"), None)
        if accepted is None:
            accepted = next((r for r in receipts if r.status == "queued"), None)
        if accepted is None:
            raise StorageError(
                "Settling a committed acceptance requires a sent or queued receipt"
            )
        outcome: Literal["accepted", "queued"]
        if accepted.status == "queued":
            committed = await storage.mark_outbox_queued(
                item.outbox_id,
                receipt_id=accepted.receipt_id,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
                expected_receipt_count=len(receipts),
            )
            outcome = "queued"
        else:
            committed = await storage.mark_outbox_sent(
                item.outbox_id,
                receipt_id=accepted.receipt_id,
                attempt_number=attempt_number,
                expected_worker_id=item.worker_id,
                expected_receipt_count=len(receipts),
            )
            outcome = "accepted"
        self._require_retry_commit(
            committed,
            item,
            transition=accepted.status,
            attempt_number=attempt_number,
        )
        return RetryAttemptFinalization(
            outcome=outcome,
            receipt_id=accepted.receipt_id,
            failure_kind=accepted.failure_kind,
            attempt_number=attempt_number,
        )

    async def _settle_resolved_handoff(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        attempt_number: int,
        resolution: UncertainHandoffResolution,
        *,
        expected_receipt_count: int = 0,
    ) -> RetryAttemptFinalization:
        """Commit a delivered-with-evidence settlement from the remediation pass.

        The retained handoff or observation evidence already proves the whole
        plan reached its transport boundary, so the existing guarded ``sent``
        transition settles the obligation. No receipt is fabricated: the
        checkpoint and observation rows remain the evidence, and the settled
        row carries no receipt pointer — any stale pointer left on the row by
        an earlier attempt is cleared atomically with the settlement write.
        Historical failed receipts are counted in the supply fence but do not
        override positive native evidence. The guarded CAS refuses a stolen
        or already-settled row, so re-running resolution after a lost commit
        converges instead of double-committing.
        """
        if resolution.observation_count is None:
            raise StorageError("Cannot settle a handoff without an evidence revision")
        committed = await storage.mark_outbox_sent(
            item.outbox_id,
            attempt_number=attempt_number,
            expected_worker_id=item.worker_id,
            clear_receipt_id=True,
            expected_observation_count=resolution.observation_count,
            expected_receipt_count=expected_receipt_count,
            expected_checkpoint_revision=resolution.checkpoint_revision,
            expected_observation_scope_revision=resolution.observation_scope_revision,
        )
        self._require_retry_commit(
            committed,
            item,
            transition="sent",
            attempt_number=attempt_number,
        )
        self._log.info(
            "Uncertain native handoff resolved by retained evidence for outbox "
            "%s attempt %d: %s; settling the obligation as sent without a "
            "fabricated receipt",
            item.outbox_id,
            attempt_number,
            ",".join(resolution.reasons),
        )
        return RetryAttemptFinalization(
            outcome="accepted",
            receipt_id=None,
            failure_kind=None,
            attempt_number=attempt_number,
        )

    async def recheck_last_uncertain_handoff(
        self,
        storage: DeliveryLifecycleStorage,
        item: DeliveryOutboxItem,
        retry_policy: RetryPolicy,
        *,
        handoff_resolver: UncertainNativeHandoffResolver,
        now: datetime | None = None,
    ) -> RetryAttemptFinalization | UncertainHandoffResolution | None:
        """Bounded pre-dispatch evidence recheck of the last ambiguous attempt.

        Runs immediately before a reclaimed row dispatches a NEW attempt when
        the row's last finalized attempt carries the explicit
        ``uncertain_native_handoff`` detail. The read-only verdict is re-run
        on that attempt's retained evidence, so newly readable evidence is
        reconsidered before redispatch:

        * ``delivered_with_evidence`` settles the obligation ``sent`` through
          the existing guarded transition (no receipt is fabricated, and any
          stale receipt pointer is cleared with the settlement);
        * ``definitively_not_submitted`` records the evidence-backed
          determination and returns the row to ``retry_wait`` without
          dispatching — normal retry dispatch resumes on a later poll;
        * ``unresolved`` returns the read-only resolution, including its
          observation count when known, to fence the next reservation.
        ``None`` means the row was not eligible for a recheck.

        This is a single recheck of the last receipt-less ambiguous attempt —
        never a durable hold, never a loop over older attempts, and never a
        transport invocation. Evidence from an older attempt can never settle
        this attempt: the resolver correlates only the exact attempt named by
        the row.
        """
        if item.active_attempt is not None or item.attempt_number < 1:
            return None
        if item.failure_kind_detail != UNCERTAIN_NATIVE_HANDOFF_DETAIL:
            return None
        attempt_number = item.attempt_number
        resolution = await handoff_resolver.resolve(item, attempt_number)
        if resolution.observation_count is None:
            return resolution
        if resolution.verdict == "unresolved":
            return resolution
        if resolution.verdict == "delivered_with_evidence":
            return await self._settle_resolved_handoff(
                storage, item, attempt_number, resolution
            )
        next_attempt_at = (now or datetime.now(timezone.utc)) + RetryExecutor(
            retry_policy
        ).compute_backoff(attempt_number)
        committed = await storage.mark_outbox_retry_wait(
            item.outbox_id,
            next_attempt_at=next_attempt_at.isoformat(),
            failure_kind=_AMBIGUOUS_DISPATCH_FAILURE_KIND,
            failure_kind_detail=HANDOFF_NOT_SUBMITTED_DETAIL,
            error_summary=_NOT_SUBMITTED_DISPATCH_ERROR,
            attempt_number=attempt_number,
            expected_worker_id=item.worker_id,
            expected_observation_count=resolution.observation_count,
            expected_receipt_count=0,
            expected_checkpoint_revision=resolution.checkpoint_revision,
        )
        self._require_retry_commit(
            committed,
            item,
            transition="retry_wait",
            attempt_number=attempt_number,
        )
        self._log.info(
            "Pre-dispatch evidence recheck resolved outbox %s attempt %d to a "
            "proven non-submission (%s); recording the determination instead "
            "of dispatching",
            item.outbox_id,
            attempt_number,
            ",".join(resolution.reasons),
        )
        return RetryAttemptFinalization(
            outcome="retry_wait",
            receipt_id=None,
            failure_kind=_AMBIGUOUS_DISPATCH_FAILURE_KIND,
            attempt_number=attempt_number,
            next_retry_at=next_attempt_at,
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

    async def defer_unsubmitted_attempt(
        self,
        storage: DeliveryLifecycleStorage,
        outbox_id: str,
        *,
        worker_id: str,
        attempt_number: int,
        next_attempt_at: str,
    ) -> bool:
        """Only a live owner before provider invocation may release a reservation."""
        return await storage.defer_unsubmitted_outbox_attempt(
            outbox_id,
            worker_id=worker_id,
            attempt_number=attempt_number,
            next_attempt_at=next_attempt_at,
        )

    async def release_unstarted_retry_claim(
        self, storage: DeliveryLifecycleStorage, item: DeliveryOutboxItem
    ) -> None:
        """Return unscheduled work to the outbox without fabricating an attempt.

        An existing active reservation is potentially a native handoff, even
        when a new dispatcher reclaimed its lease but never sent anything.
        Such claims are not releasable here and must follow reconciliation.
        """
        if item.active_attempt is not None:
            return
        if not item.worker_id:
            raise ValueError("unstarted claim release requires a worker owner")
        await storage.release_outbox_claim(
            item.outbox_id,
            item.worker_id,
            release_status="pending" if item.attempt_number == 0 else "retry_wait",
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
        min_delay: timedelta | None = None,
    ) -> datetime:
        """Defer owned work without consuming a native attempt.

        Operational refusals can impose a polling floor independent of the
        transport retry backoff. The returned time is authoritative only after
        the guarded outbox transition commits.
        """
        if min_delay is not None and min_delay < timedelta(0):
            raise ValueError("min_delay must be non-negative")
        backoff = RetryExecutor(retry_policy).compute_backoff(attempt_number)
        if min_delay is not None:
            backoff = max(backoff, min_delay)
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
        (which abandons the row). A queued receipt commits the durable,
        content-free confirmation-pending disposition: the row is never
        claimable and never redispatched. Raise
        :class:`RetryAttemptCommitRejected` if the guarded transition
        fails, or :class:`ValueError` for another receipt status.
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

        Deferred adapters can report terminal feedback immediately after
        returning a ``queued`` receipt.  That feedback may consume the live
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

        # Derive failure classification from the persisted attempt receipt
        # when the boundary's evidence carries no typed value (envelope-less
        # records). Normal target execution supplies the typed value on
        # DeliveryExecutionEvidence.
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
