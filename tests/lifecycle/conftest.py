"""Shared fixtures and helpers for DeliveryLifecycleService tests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.routing.models import RouteTarget


def _make_lifecycle() -> DeliveryLifecycleService:
    return DeliveryLifecycleService(
        logger=logging.getLogger("test.delivery_lifecycle"),
    )


def _make_plan(
    plan_id: str = "plan-001",
    adapter_id: str = "test_adapter",
    retry_policy: RetryPolicy | None = None,
) -> DeliveryPlan:
    target = RouteTarget(adapter=adapter_id, channel=None)
    return DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=retry_policy,
    )


def _make_receipt(
    receipt_id: str = "rcpt-001",
    status: Literal[
        "queued", "sent", "failed", "dead_lettered", "suppressed"
    ] = "failed",
    attempt_number: int = 1,
    event_id: str = "evt-001",
    adapter: str = "test_adapter",
    channel: str | None = None,
    plan_id: str = "plan-001",
    route_id: str = "route-001",
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
    source: str = "live",
    replay_run_id: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status=status,
        error=None,
        failure_kind=failure_kind,
        created_at=datetime.now(tz=timezone.utc),
        attempt_number=attempt_number,
        next_retry_at=next_retry_at,
        source=source,
        replay_run_id=replay_run_id,
    )
