"""Pure helper for constructing :class:`DeliveryReceipt` instances.

This module assembles :class:`~medre.core.events.canonical.DeliveryReceipt`
instances from explicit caller-supplied fields and sanitizes structured
capability-decision fields for receipt construction.  It performs **no**
lifecycle decisions, exception classification, retry scheduling, or
persistence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol

from medre.core.events.canonical import (
    DELIVERY_CAPABILITY_LEVELS,
    DELIVERY_STRATEGY_METHODS,
    DeliveryConfirmationLevel,
    DeliveryReceipt,
    DeliveryReceiptKind,
    DeliverySource,
)

if TYPE_CHECKING:

    class _PlanStrategyView(Protocol):
        method: str

    class _PlanCapabilityView(Protocol):
        """Read-only structural view of the plan fields the sanitizer reads."""

        capability_level: str | None
        capability_field: str | None
        capability_reason: str | None
        primary_strategy: _PlanStrategyView


__all__ = [
    "build_delivery_receipt",
    "capability_receipt_fields",
    "plan_capability_receipt_fields",
]


def capability_receipt_fields(
    *,
    receipt: DeliveryReceipt | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    """Return safe structured capability fields for receipt lineage.

    A prior immutable receipt is authoritative when available.  Deferred
    callbacks can race the queued receipt append, so durable outbox metadata is
    the fallback source for that narrow case.  Malformed metadata is ignored
    rather than allowed to block persistence of a transport outcome that has
    already happened.
    """
    if receipt is not None:
        return {
            "capability_level": receipt.capability_level,
            "capability_field": receipt.capability_field,
            "capability_reason": receipt.capability_reason,
            "delivery_strategy": receipt.delivery_strategy,
        }

    values = metadata or {}
    # isinstance-first so unhashable or malformed metadata values are treated
    # as absent rather than raising during persistence of a decided outcome.
    raw_level = values.get("capability_level")
    level = (
        raw_level
        if isinstance(raw_level, str) and raw_level in DELIVERY_CAPABILITY_LEVELS
        else None
    )
    raw_strategy = values.get("delivery_strategy")
    strategy = (
        raw_strategy
        if isinstance(raw_strategy, str) and raw_strategy in DELIVERY_STRATEGY_METHODS
        else None
    )
    raw_field = values.get("capability_field")
    raw_reason = values.get("capability_reason")
    return {
        "capability_level": level,
        "capability_field": (
            raw_field if level is not None and isinstance(raw_field, str) else None
        ),
        "capability_reason": (
            raw_reason if level is not None and isinstance(raw_reason, str) else None
        ),
        "delivery_strategy": strategy,
    }


def plan_capability_receipt_fields(plan: _PlanCapabilityView) -> dict[str, str | None]:
    """Return a plan's structured capability decision, validated for receipts.

    Planner-failure evidence must remain persistable even when a malformed plan
    carries an unknown capability level or strategy; the plan's capability text
    is kept only when the level is valid and the text is a string.  Every
    receipt-construction site that has a plan MUST go through this sanitizer so
    suppression, dead-letter, and skip evidence can never raise at persistence
    time.
    """
    level = plan.capability_level
    valid_level = (
        level
        if isinstance(level, str) and level in DELIVERY_CAPABILITY_LEVELS
        else None
    )
    strategy = plan.primary_strategy.method
    valid_strategy = (
        strategy
        if isinstance(strategy, str) and strategy in DELIVERY_STRATEGY_METHODS
        else None
    )
    field_text = plan.capability_field
    reason_text = plan.capability_reason
    return {
        "capability_level": valid_level,
        "capability_field": (
            field_text
            if valid_level is not None and isinstance(field_text, str)
            else None
        ),
        "capability_reason": (
            reason_text
            if valid_level is not None and isinstance(reason_text, str)
            else None
        ),
        "delivery_strategy": valid_strategy,
    }


def build_delivery_receipt(
    *,
    event_id: str,
    delivery_plan_id: str,
    target_adapter: str,
    target_channel: str | None,
    route_id: str,
    status: Literal[
        "queued",
        "sent",
        "failed",
        "dead_lettered",
        "cancelled",
        "abandoned",
        "suppressed",
    ],
    receipt_kind: DeliveryReceiptKind | None = None,
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    error: str | None = None,
    failure_kind: str | None = None,
    capability_level: str | None = None,
    capability_field: str | None = None,
    capability_reason: str | None = None,
    delivery_strategy: str | None = None,
    adapter_message_id: str | None = None,
    next_retry_at: datetime | None = None,
    retry_max_attempts: int | None = None,
    retry_backoff_base: float | None = None,
    retry_max_delay: float | None = None,
    retry_jitter: bool | None = None,
    rendering_evidence: str | None = None,
    outbox_id: str | None = None,
    confirmation_level: DeliveryConfirmationLevel = "unknown",
    sequence: int = 0,
    receipt_id: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    """Construct a :class:`DeliveryReceipt` from explicit caller-owned fields.

    Parameters are passed straight through — this helper does **not**
    classify exceptions, compute retry schedules, persist, or mutate
    anything.  It only fills in defaults for ``receipt_id`` and
    ``created_at`` when the caller omits them.

    Parameters
    ----------
    event_id:
        Canonical event being delivered.
    delivery_plan_id:
        Identifier of the delivery plan this receipt belongs to.
    target_adapter:
        Name of the adapter the event is being delivered to.
    target_channel:
        Channel / conversation ID at the target adapter.
    route_id:
        Identifier of the route that triggered this delivery.
    status:
        Delivery evidence status.
    receipt_kind:
        Semantic evidence role. When omitted, :class:`DeliveryReceipt` derives
        it from ``status``.
    source:
        Origin of this receipt (``"live"``, ``"retry"``, or ``"replay"``).
    replay_run_id:
        Replay-origin run ID. It may accompany the initial ``source="replay"``
        dispatch or a later ``source="retry"`` attempt from that lineage.
    attempt_number:
        1-indexed attempt number for this receipt.
    parent_receipt_id:
        Receipt ID of the preceding attempt in this delivery chain.
    error:
        Error message if the delivery failed.
    failure_kind:
        Categorisation of the failure, if any.
    capability_level / capability_field / capability_reason / delivery_strategy:
        Structured capability decision evidence copied from delivery planning.
        Optional only for receipts built without a plan (e.g. retry lineage
        reconstruction from durable outbox metadata).
    adapter_message_id:
        Native message ID assigned by the target adapter.
    next_retry_at:
        Scheduled time for the next retry attempt.
    retry_max_attempts:
        Maximum number of retry attempts from retry policy.
    retry_backoff_base:
        Backoff base (seconds) from retry policy.
    retry_max_delay:
        Maximum delay cap (seconds) from retry policy.
    retry_jitter:
        Whether jitter is enabled in the retry policy.
    rendering_evidence:
        Evidence string from the rendering step.
    outbox_id:
        Internal correlation key linking this receipt to the durable
        outbox item tracking this delivery attempt.
    confirmation_level:
        Strongest delivery fact proven by this receipt.  This is independent
        of the receipt status and defaults to ``"unknown"``.
    sequence:
        Monotonically increasing sequence number within the plan.
    receipt_id:
        Unique identifier; auto-generated as ``"rcpt-{uuid}"`` when
        ``None``.
    created_at:
        Timestamp; defaults to ``datetime.now(tz=timezone.utc)`` when
        ``None``.

    Returns
    -------
    DeliveryReceipt
        A fully populated, immutable receipt instance.
    """
    if receipt_id is None:
        receipt_id = f"rcpt-{uuid.uuid4()}"
    if created_at is None:
        created_at = datetime.now(tz=timezone.utc)

    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        receipt_kind=receipt_kind,
        error=error,
        failure_kind=failure_kind,
        capability_level=capability_level,
        capability_field=capability_field,
        capability_reason=capability_reason,
        delivery_strategy=delivery_strategy,
        adapter_message_id=adapter_message_id,
        next_retry_at=next_retry_at,
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        source=source,
        replay_run_id=replay_run_id,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_base=retry_backoff_base,
        retry_max_delay=retry_max_delay,
        retry_jitter=retry_jitter,
        rendering_evidence=rendering_evidence,
        outbox_id=outbox_id,
        confirmation_level=confirmation_level,
        created_at=created_at,
    )
