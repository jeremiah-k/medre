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

Runtime-staged obligations retain a complete ``OutboxDispatchContext``. Its
route, strategy chain, policy and deadline are restored exactly and checked
against the outbox identity. Direct/replay deliveries can instead carry only
route-decision metadata; for that representation the following fields are not
persisted and cannot be recovered:

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

import msgspec

from medre.core.events.canonical import DeliveryReceipt
from medre.core.events.metadata import RoutingMetadata
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


class OutboxDispatchContext(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Accepted route, plan and bounded routing attribution for a native dispatch.

    Routing metadata is retained without duplicating message content. It is
    restored on a delivery copy, independently of mutable router configuration.
    """

    route: Route
    plan: DeliveryPlan
    routing: RoutingMetadata


@dataclass(frozen=True)
class ReconstructedRetryPlan:
    """The reconstructed delivery context for a retry attempt.

    Attributes
    ----------
    route:
        Accepted route snapshot, or route rebuilt from direct/replay metadata.
    plan:
        Accepted plan snapshot, or plan rebuilt from the outbox and receipt.
    retry_policy:
        The resolved :class:`RetryPolicy` used for the plan (also returned
        separately so the caller can use it for backoff scheduling without
        reaching back into the plan).
    routing:
        Frozen routing attribution for staged obligations; absent for direct
        deliveries that retain only reduced route-decision metadata.
    """

    route: Route
    plan: DeliveryPlan
    retry_policy: RetryPolicy
    routing: RoutingMetadata | None = None


def _destination_from_outbox(item: DeliveryOutboxItem) -> RouteDestination | None:
    """Read the retained native address scope independently of its plan snapshot."""
    if item.metadata is None or "destination_kind" not in item.metadata:
        return None
    return RouteDestination(
        kind=item.metadata["destination_kind"],
        destination_hash=item.metadata.get("destination_hash"),
        destination_name=item.metadata.get("destination_name"),
        metadata=item.metadata.get("destination_metadata", {}),
    )


def reconstruct_retry_delivery_plan(
    *,
    item: DeliveryOutboxItem,
    previous_receipt: DeliveryReceipt | None,
    default_max_attempts: int,
) -> ReconstructedRetryPlan:
    """Restore staged planning, or reconstruct a direct/replay delivery plan.

    A complete staged snapshot takes precedence over the reduced representation
    described below. Snapshot failure is terminal to reconstruction; it never
    falls through to a guessed route or retry policy.

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
    if item.metadata is not None and "dispatch_context" in item.metadata:
        context = msgspec.convert(
            item.metadata["dispatch_context"], type=OutboxDispatchContext
        )
        plan = context.plan
        if (
            plan.event_id != item.event_id
            or plan.plan_id != item.delivery_plan_id
            or context.route.id != item.route_id
            or (plan.route_id or "") != (item.route_id or "")
            or plan.target.adapter != item.target_adapter
            or (plan.target.channel or None) != (item.target_channel or None)
            or plan.target not in context.route.targets
            or plan.target.destination != _destination_from_outbox(item)
            or (
                plan.target.destination.destination_hash
                if plan.target.destination
                else None
            )
            != item.target_address
            or plan.target_identity != delivery_target_identity(plan.target)
            or context.route.id not in context.routing.matched_routes
            or len(context.routing.route_trace) > 16
        ):
            raise ValueError("stored dispatch context disagrees with outbox identity")
        return ReconstructedRetryPlan(
            route=context.route,
            plan=plan,
            retry_policy=plan.retry_policy or RetryPolicy(max_attempts=1, jitter=False),
            routing=context.routing,
        )

    if item.attempt_number == 0:
        raise ValueError("never-dispatched work requires a complete dispatch context")

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
    dest = _destination_from_outbox(item)

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
        raise ValueError(f"Retry outbox item {item.outbox_id} is missing metadata")
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
    if _capability_level_raw is not None and not isinstance(_capability_level_raw, str):
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
