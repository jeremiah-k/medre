"""Resource-ownership regressions for per-target delivery coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import msgspec
import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.engine.pipeline.outbox_manager import OutboxContext, OutboxManager
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
from medre.core.events import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.capacity import CapacityController
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


@dataclass(frozen=True)
class _Limits:
    max_inflight_deliveries: int = 1
    max_inflight_replay_events: int = 1
    delivery_acquire_timeout_seconds: float = 1.0


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


class _ReceiptStorage:
    def __init__(self, receipt: DeliveryReceipt) -> None:
        self.receipt = receipt

    async def list_receipts_for_delivery(self, identity) -> list[DeliveryReceipt]:
        from medre.core.delivery_authority import delivery_identity

        assert identity == delivery_identity(self.receipt)
        return [self.receipt]


def _runner_with_receipt_result(
    candidate: DeliveryReceipt,
    persisted: DeliveryReceipt,
    *,
    failure_kind: DeliveryFailureKind | None,
) -> tuple[PipelineRunner, list[DeliveryReceipt | None]]:
    runner = _runner(cast(StorageBackend, _ReceiptStorage(persisted)))
    finalized_receipts: list[DeliveryReceipt | None] = []

    async def _create_outbox(*args: object, **kwargs: object) -> OutboxContext:
        return OutboxContext(
            outbox_id="obox-sequence",
            created=True,
            pipeline_worker="pipeline:test",
            skip_reason=None,
        )

    async def _deliver(*args: object, **kwargs: object) -> DeliveryExecutionEvidence:
        evidence = DeliveryExecutionEvidence(
            attempt_receipt=candidate,
            failure_kind=failure_kind,
            error="simulated send failure" if failure_kind is not None else None,
        )
        if failure_kind is not None:
            raise _AdapterDeliveryError(
                "target",
                "simulated send failure",
                evidence=evidence,
            )
        return evidence

    async def _finalize(*args: object, **kwargs: object) -> None:
        evidence = cast(DeliveryExecutionEvidence, args[1])
        finalized_receipts.append(evidence.current_receipt)

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_execution_to_target = _deliver  # type: ignore[assignment]
    return runner, finalized_receipts


async def test_named_replay_duplicate_serializes_before_capacity_admission(
    temp_storage: StorageBackend,
) -> None:
    """Exact local replay duplicates cannot manufacture capacity suppression."""
    runner = _runner(temp_storage)
    capacity = CapacityController(_Limits(delivery_acquire_timeout_seconds=0.02))
    runner.set_capacity_controller(capacity)
    event = make_event(event_id="coordinator-event", source_adapter="source")
    await temp_storage.append(event)

    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    create_calls = 0
    deliver_calls = 0

    async def _create_outbox(*args: object, **kwargs: object) -> OutboxContext:
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            return OutboxContext(
                outbox_id="obox-local-replay-gate",
                created=True,
                pipeline_worker="pipeline:first",
                skip_reason=None,
                attempt_number=1,
            )
        return OutboxContext(
            outbox_id="obox-local-replay-gate",
            created=False,
            pipeline_worker="pipeline:duplicate",
            skip_reason="replay_run_claimed:in_progress",
            attempt_number=1,
            replay_duplicate=True,
        )

    async def _deliver(event, route, plan, **kwargs):
        nonlocal deliver_calls
        deliver_calls += 1
        delivery_started.set()
        await release_delivery.wait()
        return DeliveryExecutionEvidence(
            attempt_receipt=build_delivery_receipt(
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=plan.target.adapter or "",
                target_channel=plan.target.channel,
                route_id=route.id,
                status="sent",
                source="replay",
                replay_run_id="run-local-gate",
                attempt_number=kwargs["reserved_attempt_number"],
                outbox_id=kwargs["outbox_id"],
            )
        )

    async def _finalize(*args: object, **kwargs: object) -> None:
        return None

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_execution_to_target = _deliver  # type: ignore[assignment]

    first = asyncio.create_task(
        runner.deliver_to_targets(
            event,
            [(_route(), _plan())],
            source="replay",
            replay_run_id="run-local-gate",
        )
    )
    await delivery_started.wait()
    duplicate = asyncio.create_task(
        runner.deliver_to_targets(
            event,
            [(_route(), _plan())],
            source="replay",
            replay_run_id="run-local-gate",
        )
    )

    # Longer than the capacity-acquire timeout: without exact-run local
    # serialization the duplicate would finish by persisting capacity
    # suppression.  The gate keeps it pending until the winner releases.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(duplicate), timeout=0.05)
    release_delivery.set()
    first_outcomes, duplicate_outcomes = await asyncio.gather(first, duplicate)

    assert first_outcomes[0].status == "success"
    assert duplicate_outcomes[0].status == "skipped"
    assert (
        duplicate_outcomes[0].failure_kind
        == DeliveryFailureKind.REPLAY_DUPLICATE_SUPPRESSED
    )
    assert create_calls == 2
    assert deliver_calls == 1
    assert runner._delivery_rejection_count == 0
    identity = DeliveryIdentity(event.event_id, _plan().plan_id, "target", None)
    assert await temp_storage.list_receipts_for_delivery(identity) == []


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
        return DeliveryExecutionEvidence(
            attempt_receipt=build_delivery_receipt(
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=plan.target.adapter or "",
                target_channel=plan.target.channel,
                route_id=route.id,
                status="sent",
                outbox_id=kwargs["outbox_id"],
            )
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
    runner.deliver_execution_to_target = _deliver  # type: ignore[assignment]
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
        return DeliveryExecutionEvidence(
            attempt_receipt=build_delivery_receipt(
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=plan.target.adapter or "",
                target_channel=plan.target.channel,
                route_id=route.id,
                status="sent",
                outbox_id=kwargs["outbox_id"],
            )
        )

    async def _finalize(*args: object, **kwargs: object) -> None:
        return None

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_execution_to_target = _deliver  # type: ignore[assignment]

    event = make_event(event_id="coordinator-no-capacity", source_adapter="source")
    outcomes = await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert outcomes[0].status == "success"
    assert runner._inflight_deliveries == {}


async def test_failed_outcome_uses_storage_assigned_receipt_sequence() -> None:
    """Failed outcomes expose the persisted receipt, not its pre-insert value."""
    candidate = build_delivery_receipt(
        event_id="coordinator-event",
        delivery_plan_id="coordinator-plan",
        target_adapter="target",
        target_channel=None,
        route_id="coordinator-route",
        status="failed",
        failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
        sequence=0,
    )
    persisted = msgspec.structs.replace(candidate, sequence=1)

    runner, finalized_receipts = _runner_with_receipt_result(
        candidate,
        persisted,
        failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT,
    )

    event = make_event(event_id="coordinator-event", source_adapter="source")
    outcomes = await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert outcomes[0].receipt == persisted
    assert finalized_receipts == [persisted]


async def test_success_outcome_uses_storage_assigned_receipt_sequence() -> None:
    """Successful outcomes expose the same exact row as durable evidence."""
    candidate = build_delivery_receipt(
        event_id="coordinator-event",
        delivery_plan_id="coordinator-plan",
        target_adapter="target",
        target_channel=None,
        route_id="coordinator-route",
        status="sent",
        sequence=0,
    )
    persisted = msgspec.structs.replace(candidate, sequence=1)

    runner, finalized_receipts = _runner_with_receipt_result(
        candidate,
        persisted,
        failure_kind=None,
    )

    event = make_event(event_id="coordinator-event", source_adapter="source")
    outcomes = await runner.deliver_to_targets(event, [(_route(), _plan())])

    assert outcomes[0].status == "success"
    assert outcomes[0].receipt == persisted
    assert finalized_receipts == [persisted]


async def test_capacity_release_targets_the_acquiring_controller_after_rewiring(
    temp_storage: StorageBackend,
) -> None:
    """A mid-delivery controller swap cannot strand the acquiring slot."""
    runner = _runner(temp_storage)
    acquirer = CapacityController(_Limits())
    replacement = CapacityController(_Limits())
    runner.set_capacity_controller(acquirer)

    async def _create_outbox(*args: object, **kwargs: object) -> OutboxContext:
        return OutboxContext(
            outbox_id="obox-swap",
            created=True,
            pipeline_worker="pipeline:test",
            skip_reason=None,
        )

    entered_delivery = asyncio.Event()
    finish_delivery = asyncio.Event()

    async def _deliver(*args: object, **kwargs: object) -> object:
        entered_delivery.set()
        await finish_delivery.wait()
        raise RuntimeError("failure after controller rewiring")

    async def _finalize(*args: object, **kwargs: object) -> None:
        return None

    runner._outbox_manager.create_for_delivery = _create_outbox  # type: ignore[assignment]
    runner._outbox_manager.start_lease_renewal = lambda _ctx: None  # type: ignore[assignment]
    runner._outbox_manager.finalize_outcome = _finalize  # type: ignore[assignment]
    runner.deliver_execution_to_target = _deliver  # type: ignore[assignment]

    event = make_event(event_id="coordinator-swap", source_adapter="source")
    task = asyncio.create_task(runner.deliver_to_targets(event, [(_route(), _plan())]))
    await asyncio.wait_for(entered_delivery.wait(), timeout=5)
    runner.set_capacity_controller(replacement)
    finish_delivery.set()
    outcomes = await task

    assert outcomes[0].status == "permanent_failure"
    assert acquirer.delivery_current == 0
    assert replacement.delivery_current == 0
    assert runner._inflight_deliveries == {}
