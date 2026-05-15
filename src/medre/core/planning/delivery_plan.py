"""Delivery plan and strategy models for the medre.

This module defines the data structures that describe *how* an event
should be delivered to a target:

* :class:`DeliveryStrategy` – the delivery method and its parameters.
* :class:`RetryPolicy` – retry/backoff configuration for failed deliveries.
* :class:`DeliveryPlan` – a complete delivery specification for one
  event-target pair, including the primary strategy and a fallback chain.
* :class:`DeliveryFailureKind` – taxonomy of delivery failure categories.
* :class:`RetryExecutor` – receipt-level retry state transitions.
* :class:`DeliveryOutcome` – per-target delivery result with failure
  classification.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal

from medre.adapters.base import AdapterSendError
from medre.core.events.canonical import DeliveryReceipt

if TYPE_CHECKING:
    from medre.core.routing.models import RouteTarget


# ---------------------------------------------------------------------------
# Delivery strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryStrategy:
    """A single delivery method and its tuning parameters.

    Attributes
    ----------
    method:
        The delivery approach – ``"direct"``, ``"propagated"``,
        ``"opportunistic"``, or ``"paper"`` (store-and-forward).
    max_retries:
        Maximum number of retry attempts before marking the delivery
        as permanently failed.
    timeout_seconds:
        Per-attempt timeout in seconds.
    """

    method: str
    max_retries: int = 3
    timeout_seconds: float = 30.0


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential-backoff retry configuration.

    Attributes
    ----------
    max_attempts:
        Maximum total delivery attempts (including the initial attempt).
    backoff_base:
        Base delay in seconds for the exponential backoff formula
        ``delay = backoff_base * 2 ** attempt``.
    max_delay_seconds:
        Upper bound for the computed backoff delay.
    jitter:
        Whether to add random jitter to the backoff delay to avoid
        thundering-herd effects.
    """

    max_attempts: int = 5
    backoff_base: float = 2.0
    max_delay_seconds: float = 60.0
    jitter: bool = True


# ---------------------------------------------------------------------------
# Delivery plan
# ---------------------------------------------------------------------------


@dataclass
class DeliveryPlan:
    """A complete delivery specification for one event-target pair.

    A delivery plan is produced by the planning pipeline after the
    router has matched an event to a route.  It captures the primary
    delivery strategy, an optional fallback chain, retry policy, and
    an optional deadline.

    Attributes
    ----------
    plan_id:
        Unique identifier for this delivery plan.
    event_id:
        The canonical event ID being delivered.
    target:
        The resolved route target this plan delivers to.
    primary_strategy:
        The first strategy to attempt.
    fallback_chain:
        Ordered list of fallback strategies to try if the primary
        strategy fails.  Evaluated in order until one succeeds or the
        chain is exhausted.
    retry_policy:
        Retry/backoff policy.  ``None`` means no retries.
    deadline:
        Absolute deadline after which the delivery should be abandoned.
        ``None`` means no deadline.
    """

    plan_id: str
    event_id: str
    target: RouteTarget
    primary_strategy: DeliveryStrategy
    fallback_chain: list[DeliveryStrategy] = field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    deadline: datetime | None = None


# ---------------------------------------------------------------------------
# Delivery failure taxonomy
# ---------------------------------------------------------------------------


class DeliveryFailureKind(Enum):
    """Taxonomy of delivery failure categories.

    Each member captures *where* in the pipeline the failure occurred
    and *whether* it is retryable.  The classification drives retry
    decisions, dead-letter transitions, and diagnostic grouping.

    Members
    -------
    PLANNER_FAILURE:
        Error during routing or planning (e.g. router misconfiguration).
        Always permanent — no retry.
    RENDERER_FAILURE:
        Error during rendering (e.g. no renderer registered for event
        kind).  Always permanent — the rendering layer is deterministic.
    ADAPTER_TRANSIENT:
        Transient adapter error (timeout, connection reset, network
        unreachable).  Retryable subject to :class:`RetryPolicy`.
    ADAPTER_PERMANENT:
        Permanent adapter error (malformed payload, business-logic
        rejection).  Not retryable.
    ADAPTER_MISSING:
        The target adapter ID is not registered in the pipeline config
        (no adapter instance exists for that ID).  Always permanent.
    TARGET_NOT_FOUND:
        Reserved.  Not currently emitted by any adapter.  Conditions
        where a channel, address, or destination is not found are
        currently classified as :attr:`ADAPTER_PERMANENT` unless a
        future adapter-specific path deliberately maps them here.
    DEADLINE_EXCEEDED:
        The delivery plan's ``deadline`` has passed.  Not retryable.
    CAPACITY_REJECTION:
        Delivery was rejected because the capacity controller's
        semaphore is exhausted (all in-flight slots occupied).  Not
        retryable at this call site — the caller should back-pressure.
    SHUTDOWN_REJECTION:
        Delivery was attempted while the pipeline is shutting down
        (capacity controller no longer accepting work).  Not retryable.
    """

    PLANNER_FAILURE = "planner_failure"
    RENDERER_FAILURE = "renderer_failure"
    ADAPTER_TRANSIENT = "adapter_transient"
    ADAPTER_PERMANENT = "adapter_permanent"
    ADAPTER_MISSING = "adapter_missing"
    TARGET_NOT_FOUND = "target_not_found"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CAPACITY_REJECTION = "capacity_rejection"
    SHUTDOWN_REJECTION = "shutdown_rejection"

    @property
    def is_retryable(self) -> bool:
        """Return ``True`` if this failure kind is retryable."""
        return self is DeliveryFailureKind.ADAPTER_TRANSIENT


# ---------------------------------------------------------------------------
# Retry executor
# ---------------------------------------------------------------------------


class RetryExecutor:
    """Stateless helper for retry/backoff decisions and receipt construction.

    :class:`RetryExecutor` encapsulates the logic for computing backoff
    delays, detecting retry exhaustion, and building the appropriate
    receipt for a retry attempt or dead-letter transition.

    Phase 1 implements a background retry scheduler via RetryWorker (opt-in).
    When RetryWorker is not enabled, retry is synchronous / receipt-level only:
    the pipeline records the failure receipt with ``next_retry_at`` populated,
    and a future scheduler (or manual replay) re-invokes ``deliver_to_target``
    using the plan and the latest receipt's ``attempt_number``.

    Parameters
    ----------
    policy:
        The retry policy governing backoff and max attempts.
    """

    def __init__(self, policy: RetryPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> RetryPolicy:
        """The retry policy used by this executor."""
        return self._policy

    def compute_backoff(self, attempt_number: int) -> timedelta:
        """Compute the backoff delay after *attempt_number* (1-indexed).

        The formula is::

            delay = min(backoff_base * 2 ** (attempt_number - 1),
                        max_delay_seconds)

        When ``jitter`` is enabled a deterministic value derived from a
        SHA-256 hash of the policy fields and attempt number is used,
        keeping the result in ``[delay * 0.5, delay]``.  This avoids
        thundering-herd effects while remaining fully reproducible.

        Parameters
        ----------
        attempt_number:
            The attempt that just failed (1 = first attempt).

        Returns
        -------
        timedelta
            Delay until the next retry attempt.
        """
        raw = self._policy.backoff_base * (2 ** (attempt_number - 1))
        capped = min(raw, self._policy.max_delay_seconds)
        if self._policy.jitter and capped > 0:
            seed = (
                f"{self._policy.backoff_base}:"
                f"{self._policy.max_delay_seconds}:"
                f"{self._policy.max_attempts}:"
                f"{attempt_number}"
            ).encode()
            digest = hashlib.sha256(seed).digest()
            fraction = int.from_bytes(digest[:8], "big") / (1 << 64)
            capped = capped - fraction * capped * 0.5
        return timedelta(seconds=capped)

    def is_exhausted(self, attempt_number: int) -> bool:
        """Return ``True`` if *attempt_number* has reached or exceeded
        the maximum allowed attempts.

        Parameters
        ----------
        attempt_number:
            The attempt that just failed (1-indexed).
        """
        return attempt_number >= self._policy.max_attempts

    def next_attempt_number(self, previous_attempt: int) -> int:
        """Return the attempt number for the next retry.

        Parameters
        ----------
        previous_attempt:
            The attempt number of the just-failed attempt.
        """
        return previous_attempt + 1

    def build_retry_receipt(
        self,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        previous_receipt_id: str | None,
        attempt_number: int,
        error: str,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        """Build a ``failed`` receipt for a retryable transient failure.

        The receipt carries ``next_retry_at`` so a future scheduler can
        decide when to re-attempt delivery.

        Parameters
        ----------
        event_id:
            The canonical event being delivered.
        delivery_plan_id:
            ID of the delivery plan.
        target_adapter:
            Name of the target adapter.
        previous_receipt_id:
            Receipt ID of the preceding attempt (receipt lineage).
        attempt_number:
            The 1-indexed attempt number for this receipt.
        error:
            Human-readable error description.
        source:
            Origin of delivery: ``"live"`` or ``"replay"``.
        replay_run_id:
            When ``source="replay"``, the replay run identifier.

        Returns
        -------
        DeliveryReceipt
            A receipt with ``status="failed"`` and ``next_retry_at``
            populated.
        """
        now = datetime.now(tz=timezone.utc)
        backoff = self.compute_backoff(attempt_number)
        return DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            status="failed",
            error=error,
            next_retry_at=now + backoff,
            created_at=now,
            attempt_number=attempt_number,
            parent_receipt_id=previous_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
        )

    def build_dead_letter_receipt(
        self,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        previous_receipt_id: str | None,
        attempt_number: int,
        error: str,
        source: str = "live",
        replay_run_id: str | None = None,
        target_channel: str | None = None,
    ) -> DeliveryReceipt:
        """Build a ``dead_lettered`` receipt after all retries are
        exhausted.

        Parameters
        ----------
        event_id:
            The canonical event being delivered.
        delivery_plan_id:
            ID of the delivery plan.
        target_adapter:
            Name of the target adapter.
        previous_receipt_id:
            Receipt ID of the preceding attempt (receipt lineage).
        attempt_number:
            The 1-indexed attempt number for this terminal receipt.
        error:
            Human-readable error description.
        source:
            Origin of delivery: ``"live"``, ``"retry"``, or ``"replay"``.
        replay_run_id:
            When ``source="replay"``, the replay run identifier.
        target_channel:
            Channel on the target adapter, if applicable.

        Returns
        -------
        DeliveryReceipt
            A receipt with ``status="dead_lettered"`` and no
            ``next_retry_at``.
        """
        now = datetime.now(tz=timezone.utc)
        return DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            status="dead_lettered",
            error=error,
            next_retry_at=None,
            created_at=now,
            attempt_number=attempt_number,
            parent_receipt_id=previous_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
            retry_max_attempts=self._policy.max_attempts,
            retry_backoff_base=self._policy.backoff_base,
            retry_max_delay=self._policy.max_delay_seconds,
            retry_jitter=self._policy.jitter,
        )

    @staticmethod
    def classify_failure(
        error: Exception,
        *,
        adapter_registered: bool = True,
        renderer_failed: bool = False,
        planner_failed: bool = False,
        deadline: datetime | None = None,
    ) -> DeliveryFailureKind:
        """Classify an exception into a :class:`DeliveryFailureKind`.

        This is a static convenience method that inspects the exception
        type and contextual flags to produce the correct failure kind.

        Parameters
        ----------
        error:
            The exception that caused the failure.
        adapter_registered:
            Whether the target adapter was found in the pipeline config.
        renderer_failed:
            Whether the failure occurred during rendering.
        planner_failed:
            Whether the failure occurred during planning.
        deadline:
            The delivery plan deadline, if any.

        Returns
        -------
        DeliveryFailureKind
        """
        if planner_failed:
            return DeliveryFailureKind.PLANNER_FAILURE
        if renderer_failed:
            return DeliveryFailureKind.RENDERER_FAILURE
        if not adapter_registered:
            return DeliveryFailureKind.ADAPTER_MISSING
        if deadline is not None and datetime.now(tz=timezone.utc) > deadline:
            return DeliveryFailureKind.DEADLINE_EXCEEDED
        # AdapterSendError carries an explicit transient flag — trust it.
        if isinstance(error, AdapterSendError):
            return (
                DeliveryFailureKind.ADAPTER_TRANSIENT
                if error.transient
                else DeliveryFailureKind.ADAPTER_PERMANENT
            )
        transient_types = (
            TimeoutError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            OSError,
        )
        if isinstance(error, transient_types):
            return DeliveryFailureKind.ADAPTER_TRANSIENT
        return DeliveryFailureKind.ADAPTER_PERMANENT


# ---------------------------------------------------------------------------
# Delivery outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of a single target delivery attempt within the pipeline.

    Each delivery target produces an independent outcome so that
    per-target failures can be tracked, categorised, and surfaced
    to observability without affecting sibling targets.

    Attributes
    ----------
    event_id:
        The canonical event ID that was delivered.
    target_adapter:
        Name of the adapter the event was sent to.
    target_channel:
        Channel on the target adapter, if applicable.
    route_id:
        ID of the route that matched this delivery.
    delivery_plan_id:
        ID of the delivery plan governing this attempt.
    status:
        Categorised outcome:
        ``"success"`` – adapter accepted the event.
        ``"queued"`` – delivery was accepted asynchronously.
        ``"transient_failure"`` – a retryable error occurred.
        ``"permanent_failure"`` – an unrecoverable error occurred.
        ``"skipped"`` – delivery was intentionally skipped (e.g. no
        renderer).
    failure_kind:
        Fine-grained failure classification from the
        :class:`DeliveryFailureKind` taxonomy.  ``None`` on success.
    receipt:
        The recorded :class:`DeliveryReceipt`, if one was produced.
    error:
        Human-readable error description on failure; ``None`` on success.
    duration_ms:
        Wall-clock time spent on this delivery attempt in milliseconds.
    """

    event_id: str
    target_adapter: str
    target_channel: str | None
    route_id: str
    delivery_plan_id: str
    status: Literal[
        "success",
        "queued",
        "transient_failure",
        "permanent_failure",
        "skipped",
    ]
    failure_kind: DeliveryFailureKind | None = None
    receipt: DeliveryReceipt | None = None
    error: str | None = None
    duration_ms: float = 0.0
