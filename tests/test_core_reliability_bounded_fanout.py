"""Core reliability: bounded fan-out task creation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.outbox_manager import OutboxContext
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.engine.pipeline.runner import _bounded_ordered_map
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


async def test_deliver_all_creates_only_delivery_limit_workers(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = make_event(event_id="core-reliability-bounded-fanout")
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(), adapters={}
        )
    )
    runner._capacity_controller = SimpleNamespace(delivery_limit=2)
    route = Route(
        id="route-bounded",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[],
    )
    deliveries = [
        (
            route,
            DeliveryPlan(
                plan_id=f"plan-{index}",
                event_id=event.event_id,
                target=RouteTarget(adapter=f"dest-{index}"),
                primary_strategy=DeliveryStrategy(method="direct"),
            ),
        )
        for index in range(5)
    ]
    entered_two = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0
    started: list[str] = []

    async def deliver(_event, _route, plan):
        nonlocal active, max_active
        started.append(plan.plan_id)
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            entered_two.set()
        await release.wait()
        active -= 1
        return build_delivery_receipt(
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=plan.target.adapter or "",
            target_channel=plan.target.channel,
            route_id=route.id,
            status="sent",
        )

    monkeypatch.setattr(runner, "deliver_to_target", deliver)
    task = asyncio.create_task(runner._deliver_all(event, deliveries))
    try:
        await asyncio.wait_for(entered_two.wait(), timeout=5)
        assert started == ["plan-0", "plan-1"]
        assert max_active == 2
    finally:
        release.set()
        results = await asyncio.wait_for(task, timeout=5)

    assert [result.delivery_plan_id for result in results if result is not None] == [
        f"plan-{index}" for index in range(5)
    ]
    assert max_active == 2


async def test_production_fanout_invokes_only_delivery_limit_acquires(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = make_event(event_id="core-reliability-production-bounded-fanout")
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(), adapters={}
        )
    )
    limits = SimpleNamespace(
        max_inflight_deliveries=2,
        max_inflight_replay_events=1,
        delivery_acquire_timeout_seconds=1.0,
    )
    capacity = CapacityController(limits)
    runner.set_capacity_controller(capacity)
    route = Route(
        id="route-production-bounded",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[],
    )
    deliveries = [
        (
            route,
            DeliveryPlan(
                plan_id=f"plan-production-{index}",
                event_id=event.event_id,
                target=RouteTarget(adapter="dest", channel=f"room-{index}"),
                primary_strategy=DeliveryStrategy(method="direct"),
            ),
        )
        for index in range(5)
    ]
    acquire_calls = 0
    original_acquire = capacity.acquire_delivery

    async def acquire_delivery() -> bool:
        nonlocal acquire_calls
        acquire_calls += 1
        return await original_acquire()

    async def create_for_delivery(_event, _route, plan, _target, _adapter, **_kwargs):
        return OutboxContext(
            outbox_id=f"obox-{plan.plan_id}",
            created=True,
            pipeline_worker="test-worker",
            skip_reason=None,
        )

    async def finalize_outcome(*_args, **_kwargs) -> None:
        return None

    entered_two = asyncio.Event()
    release = asyncio.Event()
    started: list[str] = []

    async def deliver(_event, _route, plan, **kwargs):
        started.append(plan.plan_id)
        if len(started) == 2:
            entered_two.set()
        await release.wait()
        return build_delivery_receipt(
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=plan.target.adapter or "",
            target_channel=plan.target.channel,
            route_id=route.id,
            status="sent",
            outbox_id=kwargs["outbox_id"],
        )

    monkeypatch.setattr(capacity, "acquire_delivery", acquire_delivery)
    monkeypatch.setattr(
        runner._outbox_manager, "create_for_delivery", create_for_delivery
    )
    monkeypatch.setattr(
        runner._outbox_manager, "start_lease_renewal", lambda _ctx: None
    )
    monkeypatch.setattr(runner._outbox_manager, "finalize_outcome", finalize_outcome)
    monkeypatch.setattr(runner, "deliver_to_target", deliver)

    task = asyncio.create_task(runner._deliver_to_targets_fan_out(event, deliveries))
    try:
        await asyncio.wait_for(entered_two.wait(), timeout=5)
        assert acquire_calls == 2
        assert started == ["plan-production-0", "plan-production-1"]
    finally:
        release.set()
        outcomes = await asyncio.wait_for(task, timeout=5)
    assert acquire_calls == 5
    assert [outcome.delivery_plan_id for outcome in outcomes] == [
        f"plan-production-{index}" for index in range(5)
    ]


async def test_bounded_worker_pool_finishes_siblings_and_reraises_original() -> None:
    sibling_entered = asyncio.Event()
    completed: set[int] = set()

    async def handle(item: int) -> int:
        if item == 0:
            await asyncio.wait_for(sibling_entered.wait(), timeout=1)
            raise RuntimeError("worker failed")
        sibling_entered.set()
        await asyncio.sleep(0)
        completed.add(item)
        return item

    with pytest.raises(RuntimeError, match="worker failed"):
        await _bounded_ordered_map([0, 1, 2], handle, worker_limit=2)

    assert completed == {1, 2}


async def test_bounded_worker_pool_reraises_direct_handler_cancellation() -> None:
    async def handle(_item: int) -> int:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _bounded_ordered_map([0], handle, worker_limit=1)


async def test_bounded_worker_pool_propagates_caller_cancellation() -> None:
    both_started = asyncio.Event()
    never = asyncio.Event()
    started = 0
    cancelled: set[int] = set()

    async def handle(item: int) -> int:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.add(item)
            raise
        return item

    task = asyncio.create_task(_bounded_ordered_map([0, 1], handle, worker_limit=2))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == {0, 1}
