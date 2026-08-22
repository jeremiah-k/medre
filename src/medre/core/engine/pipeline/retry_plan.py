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
    * **Primary strategy**: recovered from the required
      ``item.metadata["delivery_strategy"]`` value.
    * **Capability level**: recovered from the required
      ``item.metadata["capability_level"]`` key; the value may be ``None``.
    * **Capability field/reason**: recovered from the required metadata keys
      ``capability_field`` and ``capability_reason``; each value may be ``None``.
    * **Deadline**: recovered from the required ``item.metadata["deadline"]``
      value, which may be ``None`` or a timezone-aware ISO 8601 string.
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
    # These keys are part of the current outbox contract.  Retry execution
    # must reproduce the original planning decision; malformed durable state
    # is rejected instead of being guessed into a different strategy.
    if item.metadata is None:
        raise ValueError(
            f"Retry outbox item {item.outbox_id} is missing metadata"
        )
    _meta = item.metadata
    required_keys = {
        "capability_level",
        "delivery_strategy",
        "capability_field",
        "capability_reason",
        "deadline",
    }
    missing_keys = sorted(required_keys - _meta.keys())
    if missing_keys:
        raise ValueError(
            f"Retry outbox item {item.outbox_id} is missing route-decision "
            f"metadata keys: {', '.join(missing_keys)}"
        )

    _capability_level_raw = _meta["capability_level"]
    if _capability_level_raw is not None and not isinstance(
        _capability_level_raw, str
    ):
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has non-string "
            f"capability_level={_capability_level_raw!r}"
        )
    if (
        _capability_level_raw is not None
        and _capability_level_raw not in _VALID_CAPABILITY_LEVELS
    ):
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has invalid "
            f"capability_level={_capability_level_raw!r}"
        )
    _capability_level: str | None = _capability_level_raw

    _delivery_strategy_raw = _meta["delivery_strategy"]
    if not isinstance(_delivery_strategy_raw, str) or (
        _delivery_strategy_raw not in get_args(DeliveryStrategyMethod)
    ):
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has invalid "
            f"delivery_strategy={_delivery_strategy_raw!r}"
        )
    _strategy_method: DeliveryStrategyMethod = _delivery_strategy_raw  # type: ignore[assignment]

    _capability_field = _meta["capability_field"]
    if _capability_field is not None and not isinstance(_capability_field, str):
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has invalid "
            f"capability_field={_capability_field!r}"
        )
    _capability_reason = _meta["capability_reason"]
    if _capability_reason is not None and not isinstance(_capability_reason, str):
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has invalid "
            f"capability_reason={_capability_reason!r}"
        )

    _deadline_raw = _meta["deadline"]
    _deadline: datetime | None = None
    if _deadline_raw is not None:
        if not isinstance(_deadline_raw, str):
            raise ValueError(
                f"Retry outbox item {item.outbox_id} has non-string "
                f"deadline={_deadline_raw!r}"
            )
        try:
            _deadline = datetime.fromisoformat(_deadline_raw)
        except ValueError as exc:
            raise ValueError(
                f"Retry outbox item {item.outbox_id} has invalid "
                f"deadline={_deadline_raw!r}"
            ) from exc
        if _deadline.tzinfo is None:
            raise ValueError(
                f"Retry outbox item {item.outbox_id} has timezone-naive "
                f"deadline={_deadline_raw!r}"
            )

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
