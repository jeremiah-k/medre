"""Retry delivery-plan reconstruction from persisted frozen work context.

When the RetryWorker re-attempts a delivery it must rebuild a minimal
:class:`~medre.core.planning.delivery_plan.DeliveryPlan` and
:class:`~medre.core.routing.models.Route` from the frozen dispatch context
persisted for the outbox generation and (when available) the previous
delivery receipt.

**Frozen work snapshots.**  Every outbox generation created by
``OutboxManager.create_for_delivery`` persists its frozen dispatch context in
``delivery_outbox_snapshots`` (``staged`` shape: the complete
``OutboxDispatchContext``; ``direct`` shape: the reduced route-decision and
destination context).  The storage read is bound to the exact outbox
identity; reconstruction consumes that typed read and keeps every identity
check. Historical rows created before the snapshot table keep their inline
``delivery_outbox.metadata`` bytes as the frozen context, and reconstruction
from those inline bytes is unchanged. A post-change row without a snapshot
fails closed; reconstruction never falls back to current router
configuration.

**Route-decision metadata recovery.**  The frozen context persists
route-decision fields (capability_level, delivery_strategy, capability_field,
capability_reason, deadline).  The reconstruction helper reads them back so
retry delivery matches the original live delivery decision rather than
defaulting to ``capability_level=None`` (which silently becomes ``"native"``)
and ``strategy="direct"``.

Runtime-staged obligations retain a complete ``OutboxDispatchContext``. Its
route, strategy chain, policy and deadline are restored exactly and checked
against the outbox identity. Direct/replay deliveries retain only the
reduced context; for that representation the following fields are not
persisted and cannot be recovered:

* ``fallback_chain`` — always ``[]``.
* ``Route.source`` — always a minimal/dummy source.

The helper in this module centralises that reconstruction so the
RetryWorker does not duplicate planning logic and so the semantics are
documented in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, get_args

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
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshot

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


def _destination_from_context(meta: Mapping[str, Any]) -> RouteDestination | None:
    """Read the retained native address scope from a frozen context mapping."""
    if "destination_kind" not in meta:
        return None
    return RouteDestination(
        kind=meta["destination_kind"],
        destination_hash=meta.get("destination_hash"),
        destination_name=meta.get("destination_name"),
        metadata=meta.get("destination_metadata", {}),
    )


def _destination_from_outbox(item: DeliveryOutboxItem) -> RouteDestination | None:
    """Read the retained native address scope independently of its plan snapshot."""
    if item.metadata is None:
        return None
    return _destination_from_context(item.metadata)


def capability_fallback_from_snapshot(
    snapshot: OutboxDispatchSnapshot,
) -> dict[str, Any]:
    """Compose the receipt capability-fallback mapping from a frozen snapshot.

    ``capability_receipt_fields`` reads capability decision facts from
    durable outbox metadata when no prior receipt exists.  Post-change rows
    keep those facts in the frozen work snapshot instead of inline metadata,
    so this helper restores the same reduced key set from the snapshot's
    frozen document.
    """
    if snapshot.kind == "direct":
        return dict(snapshot.context)
    plan = snapshot.context.get("plan")
    if not isinstance(plan, dict):
        return {}
    strategy = plan.get("primary_strategy")
    return {
        "capability_level": plan.get("capability_level"),
        "delivery_strategy": (
            strategy.get("method") if isinstance(strategy, dict) else None
        ),
        "capability_field": plan.get("capability_field"),
        "capability_reason": plan.get("capability_reason"),
    }


class OutboxSnapshotReader(Protocol):
    """Narrow capability-context read surface shared by lifecycle and storage."""

    async def get_outbox_dispatch_context(
        self,
        outbox_id: str,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
    ) -> OutboxDispatchSnapshot | None: ...


async def resolve_capability_fallback(
    storage: OutboxSnapshotReader,
    item: DeliveryOutboxItem,
    logger: logging.Logger,
) -> Mapping[str, Any] | None:
    """Recover receipt capability facts from historical or owned dispatch context.

    A previously completed transport outcome must still be persisted when a
    snapshot read fails. The warning includes the underlying failure so
    operators can distinguish corrupt context from temporary database I/O.
    Reconstruction of unstarted work, unlike evidence recording here, stays
    fail-closed and treats transient read errors separately.
    """
    if item.metadata:
        return item.metadata
    try:
        snapshot = await storage.get_outbox_dispatch_context(
            item.outbox_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
        )
    except Exception:
        logger.warning(
            "Could not read frozen dispatch context for outbox_id=%s; "
            "committed evidence will lack capability fallback fields",
            item.outbox_id,
            exc_info=True,
        )
        return None
    return capability_fallback_from_snapshot(snapshot) if snapshot else None


def _require_frozen_fields(saved: Any, decoded: Any) -> None:
    """Reject fields a typed conversion would silently drop or default.

    Compare shape rather than values: datetime normalization and opaque
    destination/strategy dictionaries retain their existing semantics.
    """
    if is_dataclass(decoded):
        decoded = {
            field.name: getattr(decoded, field.name) for field in fields(decoded)
        }
    elif isinstance(decoded, msgspec.Struct):
        decoded = msgspec.structs.asdict(decoded)
    if isinstance(saved, dict):
        if not isinstance(decoded, dict) or saved.keys() != decoded.keys():
            raise ValueError("stored dispatch context has changed document fields")
        for key, value in saved.items():
            _require_frozen_fields(value, decoded[key])
    elif isinstance(saved, (list, tuple)):
        if isinstance(decoded, (set, frozenset)):
            return  # Typed set elements have already been validated by msgspec.
        if not isinstance(decoded, (list, tuple)) or len(saved) != len(decoded):
            raise ValueError("stored dispatch context has changed document fields")
        for saved_value, decoded_value in zip(saved, decoded, strict=True):
            _require_frozen_fields(saved_value, decoded_value)


def _reconstruct_from_staged_snapshot(
    item: DeliveryOutboxItem,
    snapshot: OutboxDispatchSnapshot,
) -> ReconstructedRetryPlan:
    """Restore the accepted route, plan, and routing from a staged snapshot.

    Snapshot failure is terminal to reconstruction: an unknown or changed
    document shape raises instead of proceeding, and every identity check the
    inline path performs is checked against the outbox row. The retained
    destination is the plan's own target; the snapshot's identity binding to
    the row is enforced by the storage read.
    """
    context = msgspec.convert(snapshot.context, type=OutboxDispatchContext)
    _require_frozen_fields(snapshot.context, context)
    plan = context.plan
    if plan.deadline is not None and plan.deadline.utcoffset() is None:
        raise ValueError("stored dispatch context has a timezone-naive deadline")
    if (
        plan.event_id != item.event_id
        or plan.plan_id != item.delivery_plan_id
        or context.route.id != item.route_id
        or (plan.route_id or "") != (item.route_id or "")
        or plan.target.adapter != item.target_adapter
        or (plan.target.channel or None) != (item.target_channel or None)
        or plan.target not in context.route.targets
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


def reconstruct_retry_delivery_plan(
    *,
    item: DeliveryOutboxItem,
    previous_receipt: DeliveryReceipt | None,
    default_max_attempts: int,
    dispatch_snapshot: OutboxDispatchSnapshot | None = None,
) -> ReconstructedRetryPlan:
    """Restore staged planning, or reconstruct a direct/replay delivery plan.

    The frozen work snapshot returned by
    :meth:`~medre.core.storage.backend.StorageBackend.get_outbox_dispatch_context`
    takes precedence when present; the reconstruction keeps every identity
    check and never consults current router configuration. Snapshot failure
    is terminal to reconstruction; it never falls through to a guessed route
    or retry policy. Historical rows without a snapshot reconstruct from
    their inline metadata bytes exactly as before.

    Parameters
    ----------
    item:
        The outbox item being retried.  Provides target adapter/channel,
        route/plan/event IDs, and — for historical rows — the inline
        route-decision and destination metadata persisted at outbox creation
        time.
    previous_receipt:
        The most recent delivery receipt for this target, or ``None`` if
        no previous receipt exists (first retry of a newly-created item).
        Used to restore the retry policy parameters.
    default_max_attempts:
        Fallback ``max_attempts`` when no previous receipt is available
        or the receipt's ``retry_max_attempts`` is ``None``.  Typically
        the worker's configured default.
    dispatch_snapshot:
        The composed frozen dispatch context read under the exact outbox
        identity, or ``None`` for historical rows whose frozen context is
        their inline metadata.  A row with neither a snapshot nor inline
        metadata fails closed.

    Returns
    -------
    ReconstructedRetryPlan
        A frozen bundle containing the reconstructed route, plan, and
        resolved retry policy.

    Reconstruction semantics
    ------------------------
    * **Target adapter/channel**: taken directly from ``item``.
    * **Destination**: reconstructed from the frozen context keys
      ``destination_kind``, ``destination_hash``, ``destination_name``,
      ``destination_metadata`` — from the snapshot for rows created with a
      snapshot, from the inline metadata bytes for historical rows; staged
      snapshots carry the destination inside the retained plan.
    * **Route ID**: ``item.route_id or ""`` for the :class:`Route`,
      ``item.route_id or None`` for the plan (plan's ``route_id`` is
      optional).
    * **Plan ID**: ``item.delivery_plan_id or ""``.
    * **Event ID**: ``item.event_id``.
    * **Primary strategy**: recovered from the required
      ``delivery_strategy`` value of the frozen context.
    * **Capability level**: recovered from the required
      ``capability_level`` key; the value may be ``None``.
    * **Capability field/reason**: recovered from the required
      ``capability_field`` and ``capability_reason`` keys; each value may be
      ``None``.
    * **Deadline**: recovered from the required ``deadline`` value, which
      may be ``None`` or a timezone-aware ISO 8601 string.
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
    _inline_metadata = item.metadata or {}
    if dispatch_snapshot is not None and _inline_metadata:
        raise ValueError(
            f"Outbox item {item.outbox_id} carries both a frozen work "
            "snapshot and inline dispatch metadata"
        )

    if dispatch_snapshot is not None and dispatch_snapshot.kind == "staged":
        return _reconstruct_from_staged_snapshot(item, dispatch_snapshot)

    if not dispatch_snapshot and "dispatch_context" in _inline_metadata:
        context = msgspec.convert(
            _inline_metadata["dispatch_context"], type=OutboxDispatchContext
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

    # -- Route-decision context recovery -----------------------------------
    # These keys are frozen work state.  Retry execution must reproduce the
    # original planning decision; malformed durable state is rejected instead
    # of being guessed into a different strategy.
    if dispatch_snapshot is not None:
        _meta = dispatch_snapshot.context
    elif _inline_metadata:
        _meta = _inline_metadata
    else:
        raise ValueError(
            f"Retry outbox item {item.outbox_id} has no frozen dispatch "
            "context; reconstruction fails closed instead of re-planning"
        )

    # -- Destination, target and route -------------------------------------
    # The frozen destination opaque keys come from the snapshot for rows
    # created with one, and from the historical inline bytes for rows created
    # before the snapshot table.
    dest = _destination_from_context(_meta)

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

    if dispatch_snapshot is not None:
        destination_keys = {
            "destination_kind",
            "destination_hash",
            "destination_name",
            "destination_metadata",
        }
        if _meta.keys() - required_keys - destination_keys:
            raise ValueError("stored direct dispatch context has unknown fields")
        present_destination = _meta.keys() & destination_keys
        if present_destination and present_destination != destination_keys:
            raise ValueError(
                "stored direct dispatch context has incomplete destination"
            )
        if present_destination:
            # Dataclass construction alone does not validate annotations.
            dest = msgspec.convert(
                {
                    "kind": _meta["destination_kind"],
                    "destination_hash": _meta["destination_hash"],
                    "destination_name": _meta["destination_name"],
                    "metadata": _meta["destination_metadata"],
                },
                type=RouteDestination,
            )

    if (dest.destination_hash if dest is not None else None) != item.target_address:
        raise ValueError("stored dispatch context disagrees with outbox destination")

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
