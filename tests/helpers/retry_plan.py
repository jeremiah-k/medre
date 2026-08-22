"""Shared factories for retry-plan reconstruction tests."""

from __future__ import annotations

from medre.core.storage.backend import DeliveryOutboxItem

ROUTE_DECISION_METADATA: dict[str, object] = {
    "capability_level": None,
    "delivery_strategy": "direct",
    "capability_field": None,
    "capability_reason": None,
    "deadline": None,
}


def make_retry_outbox(
    *,
    target_adapter: str = "matrix",
    target_channel: str | None = "#general",
    route_id: str = "route-1",
    delivery_plan_id: str = "plan-abc",
    event_id: str = "evt-001",
    metadata: dict[str, object] | None = None,
    include_route_metadata: bool = True,
) -> DeliveryOutboxItem:
    """Create an outbox row that satisfies the current retry-plan contract."""
    resolved_metadata: dict[str, object] | None
    if include_route_metadata:
        resolved_metadata = dict(ROUTE_DECISION_METADATA)
        if metadata is not None:
            resolved_metadata.update(metadata)
    else:
        resolved_metadata = metadata
    return DeliveryOutboxItem(
        outbox_id="ob-1",
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        metadata=resolved_metadata,
    )
