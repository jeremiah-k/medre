"""Retry delivery-plan reconstruction from persisted outbox/receipt data.

When the RetryWorker re-attempts a delivery it must rebuild a minimal
:class:`~medre.core.planning.delivery_plan.DeliveryPlan` and
:class:`~medre.core.routing.models.Route` from the information persisted in
the outbox item and (when available) the previous delivery receipt.

**Route-decision metadata recovery.**  The outbox ``metadata`` dict
persists route-decision fields (capability_level, delivery_strategy,
capability_field, capability_reason, deadline) alongside destination
metadata.  The reconstruction helper reads these back so retry delivery
matches the original live delivery decision rather than defaulting to
``capability_level=None`` (which silently becomes ``"native"``) and
``strategy="direct"``.

Fields that are *not* persisted and cannot be recovered:

* ``fallback_chain`` — always ``[]``.
* ``Route.source`` — always a minimal/dummy source.

The helper in this module centralises that reconstruction so the
RetryWorker does not duplicate planning logic and so the semantics are
documented in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import get_args

from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    DeliveryStrategyMethod,
    RetryPolicy,
    delivery_target_identity,
)
from medre.core.routing.models import Route, RouteDestination, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem

logger = logging.getLogger(__name__)

#: Valid capability-level values matching
#: :data:`~medre.core.rendering.renderer.CapabilityLevel`.
_VALID_CAPABILITY_LEVELS: frozenset[str] = frozenset(
    {"native", "fallback", "unsupported"}
)


@dataclass(frozen=True)
class ReconstructedRetryPlan:
    """The reconstructed delivery context for a retry attempt.

    Attributes
    ----------
    route:
        Minimal :class:`Route` rebuilt from the outbox item.
    plan:
        Minimal :class:`DeliveryPlan` rebuilt from the outbox item and
        previous receipt.
    retry_policy:
        The resolved :class:`RetryPolicy` used for the plan (also returned
        separately so the caller can use it for backoff scheduling without
        reaching back into the plan).
    """

    route: Route
    plan: DeliveryPlan
    retry_policy: RetryPolicy


def reconstruct_retry_delivery_plan(
    *,
    item: DeliveryOutboxItem,
    previous_receipt: DeliveryReceipt | None,
    default_max_attempts: int,
) -> ReconstructedRetryPlan:
    """Reconstruct a minimal delivery plan and route for a retry attempt.

    Parameters
    ----------
    item:
        The outbox item being retried.  Provides target adapter/channel,
        destination metadata, route/plan/event IDs, and route-decision
        metadata persisted at outbox creation time.
    previous_receipt:
        The most recent delivery receipt for this target, or ``None`` if
        no previous receipt exists (first retry of a newly-created item).
        Used to restore the retry policy parameters.
    default_max_attempts:
        Fallback ``max_attempts`` when no previous receipt is available
        or the receipt's ``retry_max_attempts`` is ``None``.  Typically
        the worker's configured default.

    Returns
    -------
    ReconstructedRetryPlan
        A frozen bundle containing the reconstructed route, plan, and
        resolved retry policy.

    Reconstruction semantics
    ------------------------
    * **Target adapter/channel**: taken directly from ``item``.
    * **Destination**: reconstructed from ``item.metadata`` keys
      ``destination_kind``, ``destination_hash``, ``destination_name``,
      ``destination_metadata`` — the same keys the pipeline persisted at
      outbox creation time.
    * **Route ID**: ``item.route_id or ""`` for the :class:`Route`,
      ``item.route_id or None`` for the plan (plan's ``route_id`` is
      optional).
    * **Plan ID**: ``item.delivery_plan_id or ""``.
    * **Event ID**: ``item.event_id``.
    * **Primary strategy**: recovered from ``item.metadata["delivery_strategy"]``
      when present; falls back to ``"direct"`` for outbox rows with missing
      or corrupt prerelease metadata that predate route-decision persistence.
    * **Capability level**: recovered from ``item.metadata["capability_level"]``
      when present; falls back to ``None`` for rows with missing or corrupt
      prerelease metadata.
    * **Capability field/reason**: recovered from ``item.metadata`` keys
      ``capability_field`` and ``capability_reason`` when present.
    * **Deadline**: recovered from ``item.metadata["deadline"]`` (ISO 8601
      string) when present; falls back to ``None``.
    * **Retry policy**: restored from the previous receipt's
      ``retry_max_attempts``, ``retry_backoff_base``,
      ``retry_max_delay``, and ``retry_jitter`` fields.  Falls back to
      defaults when the receipt is ``None`` or individual fields are
      ``None``.
    * **Target identity**: recomputed via :func:`delivery_target_identity`
      from the reconstructed target.
    * **Route source**: a minimal/dummy source — the original source is
      not persisted.

    Intentionally omitted (not persisted, cannot be recovered):

    * ``fallback_chain`` — always ``[]``.
    * ``Route.source`` — always ``RouteSource(adapter=None, ...)``.
    """
    # -- Retry policy from previous receipt with fallback defaults --------
    max_attempts = default_max_attempts
    backoff_base = 2.0
    max_delay = 60.0
    jitter = False

    if previous_receipt is not None:
        max_attempts = (
            previous_receipt.retry_max_attempts
            if previous_receipt.retry_max_attempts is not None
            else default_max_attempts
        )
        backoff_base = (
            previous_receipt.retry_backoff_base
            if previous_receipt.retry_backoff_base is not None
            else 2.0
        )
        max_delay = (
            previous_receipt.retry_max_delay
            if previous_receipt.retry_max_delay is not None
            else 60.0
        )
        jitter = (
            previous_receipt.retry_jitter
            if previous_receipt.retry_jitter is not None
            else False
        )

    retry_policy = RetryPolicy(
        max_attempts=max_attempts,
        backoff_base=backoff_base,
        max_delay_seconds=max_delay,
        jitter=jitter,
    )

    # -- Destination from item metadata -----------------------------------
    dest: RouteDestination | None = None
    if item.metadata and "destination_kind" in item.metadata:
        dest = RouteDestination(
            kind=item.metadata["destination_kind"],
            destination_hash=item.metadata.get("destination_hash"),
            destination_name=item.metadata.get("destination_name"),
            metadata=item.metadata.get("destination_metadata", {}),
        )

    # -- Target and route -------------------------------------------------
    target = RouteTarget(
        adapter=item.target_adapter,
        channel=item.target_channel,
        destination=dest,
    )

    route = Route(
        id=item.route_id or "",
        source=RouteSource(adapter=None, event_kinds=(), channel=None),
        targets=[target],
    )

    # -- Route-decision metadata recovery ----------------------------------
    # Recover capability/strategy/deadline from the outbox metadata dict.
    # Outbox rows with missing or corrupt prerelease metadata that predate
    # route-decision persistence will not have these keys; graceful
    # degradation uses DeliveryPlan defaults.
    _meta = item.metadata or {}
    _capability_level: str | None = _meta.get("capability_level")

    # Validate _capability_level against the closed vocabulary.
    if (
        _capability_level is not None
        and _capability_level not in _VALID_CAPABILITY_LEVELS
    ):
        logger.warning(
            "Invalid capability_level %r in outbox metadata for item %s; "
            "falling back to None",
            _capability_level,
            item.outbox_id,
        )
        _capability_level = None

    _delivery_strategy_raw: str | None = _meta.get("delivery_strategy")
    _capability_field: str | None = _meta.get("capability_field")
    _capability_reason: str | None = _meta.get("capability_reason")
    _deadline_raw: str | None = _meta.get("deadline")

    # Validate delivery_strategy against the closed vocabulary.
    _strategy_method: DeliveryStrategyMethod = "direct"
    if _delivery_strategy_raw is not None:
        # Accept the raw string if it matches a known DeliveryStrategyMethod value.
        if _delivery_strategy_raw in get_args(DeliveryStrategyMethod):
            _strategy_method = _delivery_strategy_raw  # type: ignore[assignment]

    _deadline: datetime | None = None
    if _deadline_raw is not None:
        try:
            _parsed = datetime.fromisoformat(_deadline_raw)
            # Reject timezone-naive datetimes — comparison with
            # datetime.now(tz=timezone.utc) would raise TypeError.
            if _parsed.tzinfo is not None:
                _deadline = _parsed
        except (ValueError, TypeError):
            pass  # Graceful degradation: deadline stays None.

    # -- Delivery plan ----------------------------------------------------
    plan = DeliveryPlan(
        plan_id=item.delivery_plan_id or "",
        event_id=item.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method=_strategy_method),
        retry_policy=retry_policy,
        route_id=item.route_id or None,
        target_identity=delivery_target_identity(target),
        capability_level=_capability_level,
        capability_field=_capability_field,
        capability_reason=_capability_reason,
        deadline=_deadline,
    )

    return ReconstructedRetryPlan(
        route=route,
        plan=plan,
        retry_policy=retry_policy,
    )
