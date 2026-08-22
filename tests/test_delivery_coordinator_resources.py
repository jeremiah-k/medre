"""Resource-ownership regressions for per-target delivery coordination."""

from __future__ import annotations

import asyncio

import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.outbox_manager import OutboxContext, OutboxManager
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.capacity import CapacityController
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


class _Limits:
    max_inflight_deliveries = 1
    max_inflight_replay_events = 1
    delivery_acquire_timeout_seconds = 1.0


class _Adapter:
    @property
    def platform(self) -> str | None:
        return None

    async def deliver(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("delivery should be replaced by the test")


def _route() -> Route:
    return Route(
        id="coordinator-route",
        source=RouteSource(
            adapter="source",
            channel=None,
            event_kinds=("message.created",),
        ),
        targets=[RouteTarget(adapter="target")],
    )


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        plan_id="coordinator-plan",
        event_id="coordinator-event",
        target=RouteTarget(adapter="target"),
        primary_strategy=DeliveryStrategy(method="direct"),
    )


def _runner(storage: StorageBackend) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(
        storage=storage,
        router=Router(routes=[_route()]),
        adapters={"target": _Adapter()},
    )
    return PipelineRunner(config)


async def test_capacity_released_when_outbox_creation_is_cancelled(
    temp_storage: StorageBackend,
) -> None:
    """Cancellation after capacity acquisition cannot leak the slot."""
    runner = _runner(temp_storage)
    capacity = CapacityController(_Limits())
    runner.set_capacity_controller(capacity)

    async def _cancel_outbox(*args: object, **kwargs: object) -> OutboxContext:
        raise asyncio.CancelledError

    runner._outbox_manager.create_for_delivery = _cancel_outbox  # type: ignore[assignment]
    event = make_event(event_id="coordinator-cancel", source_adapter="source")

    with pytest.raises(asyncio.CancelledError):
        await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert capacity.delivery_current == 0
    assert runner._inflight_deliveries == {}


async def test_capacity_release_survives_outbox_finalization_failure(
    temp_storage: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable-finalization failure cannot strand runtime capacity or identity."""
    runner = _runner(temp_storage)
    capacity = CapacityController(_Limits())
    runner.set_capacity_controller(capacity)
    order: list[str] = []

    async def _create_outbox(*args: object, **kwargs: object) -> OutboxContext:
        return OutboxContext(
            outbox_id="obox-coordinator",
            created=True,
            pipeline_worker="pipeline:test",
            skip_reason=None,
        )

    async def _deliver(event, route, plan, **kwargs):
        order.append("deliver")
        assert runner._inflight_deliveries
        return build_delivery_receipt(
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=plan.target.adapter or "",
            target_channel=plan.target.channel,
            route_id=route.id,
            status="sent",
            outbox_id=kwargs["outbox_id"],
        )

    async def _cancel_renewal(_task: object) -> None:
        order.append("cancel_renewal")

    async def _finalize(*args: object, **kwargs: object) -> None:
        order.append("finalize")
        raise RuntimeError("injected finalization failure")

    real_release = capacity.release_delivery

    async def _release() -> None:
        order.append("release")
        await real_release()

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_to_target = _deliver  # type: ignore[assignment]
    monkeypatch.setattr(OutboxManager, "cancel_renewal", staticmethod(_cancel_renewal))
    monkeypatch.setattr(capacity, "release_delivery", _release)

    event = make_event(event_id="coordinator-finalize", source_adapter="source")
    with pytest.raises(RuntimeError, match="injected finalization failure"):
        await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert order == ["deliver", "cancel_renewal", "finalize", "release"]
    assert capacity.delivery_current == 0
    assert runner._inflight_deliveries == {}


async def test_delivery_without_capacity_controller_is_not_tracked_as_inflight(
    temp_storage: StorageBackend,
) -> None:
    """In-flight shutdown evidence remains tied to capacity-owned work."""
    runner = _runner(temp_storage)

    async def _create_outbox(*args: object, **kwargs: object) -> OutboxContext:
        return OutboxContext(
            outbox_id="obox-no-capacity",
            created=True,
            pipeline_worker="pipeline:test",
            skip_reason=None,
        )

    async def _deliver(event, route, plan, **kwargs):
        assert runner._inflight_deliveries == {}
        return build_delivery_receipt(
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=plan.target.adapter or "",
            target_channel=plan.target.channel,
            route_id=route.id,
            status="sent",
            outbox_id=kwargs["outbox_id"],
        )

    async def _finalize(*args: object, **kwargs: object) -> None:
        return None

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_to_target = _deliver  # type: ignore[assignment]

    event = make_event(event_id="coordinator-no-capacity", source_adapter="source")
    outcomes = await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert outcomes[0].status == "success"
    assert runner._inflight_deliveries == {}
