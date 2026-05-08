"""Delivery plan and strategy models for the medre.

This module defines the data structures that describe *how* an event
should be delivered to a target:

* :class:`DeliveryStrategy` – the delivery method and its parameters.
* :class:`RetryPolicy` – retry/backoff configuration for failed deliveries.
* :class:`DeliveryPlan` – a complete delivery specification for one
  event-target pair, including the primary strategy and a fallback chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

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
