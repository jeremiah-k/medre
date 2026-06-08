"""Pure helper for constructing :class:`DeliveryReceipt` instances.

This module provides a single function, :func:`build_delivery_receipt`, that
assembles a :class:`~medre.core.events.canonical.DeliveryReceipt` from explicit
caller-supplied fields.  It performs **no** lifecycle decisions, exception
classification, retry scheduling, or persistence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from medre.core.events.canonical import DeliveryReceipt

__all__ = ["build_delivery_receipt"]


def build_delivery_receipt(
    *,
    event_id: str,
    delivery_plan_id: str,
    target_adapter: str,
    target_channel: str | None,
    route_id: str,
    status: Literal["queued", "sent", "failed", "dead_lettered", "suppressed"],
    source: str = "live",
    replay_run_id: str | None = None,
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    error: str | None = None,
    failure_kind: str | None = None,
    adapter_message_id: str | None = None,
    next_retry_at: datetime | None = None,
    retry_max_attempts: int | None = None,
    retry_backoff_base: float | None = None,
    retry_max_delay: float | None = None,
    retry_jitter: bool | None = None,
    rendering_evidence: str | None = None,
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
        Current delivery status.
    source:
        Origin of this receipt (``"live"``, ``"retry"``, or ``"replay"``).
    replay_run_id:
        When ``source="replay"``, the run ID of the replay execution.
    attempt_number:
        1-indexed attempt number for this receipt.
    parent_receipt_id:
        Receipt ID of the preceding attempt in this delivery chain.
    error:
        Error message if the delivery failed.
    failure_kind:
        Categorisation of the failure, if any.
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
        error=error,
        failure_kind=failure_kind,
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
        created_at=created_at,
    )
