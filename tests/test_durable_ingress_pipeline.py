"""Coverage for durable-ingress boundaries in the core pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgspec
import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events import CanonicalEvent, EventRelation, NativeMessageRef, NativeRef
from medre.core.ingress import DurableIngressDeferredError
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


def _native_ref(native_id: str) -> NativeRef:
    return NativeRef(
        adapter="matrix-main",
        native_channel_id="!room:example.org",
        native_message_id=native_id,
    )


def _runner(
    storage: SQLiteStorage, *, accounting: RuntimeAccounting | None = None
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(storage, Router([]))
    config.runtime_accounting = accounting
    return PipelineRunner(config)


def _stored_admitted_event(event_id: str) -> CanonicalEvent:
    """Build the canonical shape process_admitted_event actually consumes."""
    event = make_event(event_id=event_id)
    return msgspec.structs.replace(
        event,
        root_event_id=event_id,
        conversation_id=event_id,
    )


async def test_admit_ingress_tracks_created_duplicate_and_history(
    temp_storage: SQLiteStorage,
) -> None:
    accounting = RuntimeAccounting()
    runner = _runner(temp_storage, accounting=accounting)

    first = make_event(event_id="evt-first", source_native_ref=_native_ref("$same"))
    created = await runner.admit_ingress(first, "live")
    duplicate = await runner.admit_ingress(
        make_event(event_id="evt-redecoded", source_native_ref=_native_ref("$same")),
        "recovered",
    )
    history = await runner.admit_ingress(
        make_event(event_id="evt-history", source_native_ref=_native_ref("$history")),
        "history",
    )

    assert created.created is True
    assert created.duplicate is False
    assert duplicate.created is False
    assert duplicate.duplicate is True
    assert duplicate.event_id == first.event_id
    assert history.work_status == "suppressed_history"
    assert accounting.snapshot()["inbound_accepted"] == 2
    assert accounting.snapshot()["loop_prevented"] == 1
    assert (
        await temp_storage.resolve_native_ref(
            "matrix-main", "!room:example.org", "$same"
        )
        == first.event_id
    )


async def test_admit_ingress_without_native_ref_creates_pending_work(
    temp_storage: SQLiteStorage,
) -> None:
    runner = _runner(temp_storage)
    event = make_event(event_id="evt-no-native", source_native_ref=None)

    result = await runner.admit_ingress(event, "live")

    assert result.created is True
    assert result.work_status == "pending"
    assert PipelineRunner._build_inbound_native_ref(event) is None


def test_build_inbound_native_ref_rejects_empty_native_id() -> None:
    event = make_event(
        event_id="evt-empty-native",
        source_native_ref=NativeRef(
            adapter="matrix-main",
            native_channel_id="!room:example.org",
            native_message_id="",
        ),
    )

    assert PipelineRunner._build_inbound_native_ref(event) is None


async def test_process_admitted_event_rejects_missing_event(
    temp_storage: SQLiteStorage,
) -> None:
    runner = _runner(temp_storage)

    with pytest.raises(RuntimeError, match="admitted ingress event is missing"):
        await runner.process_admitted_event("evt-missing")


async def test_process_admitted_event_suppresses_reaction_to_reaction(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-reaction")
    await temp_storage.append(event)
    is_reaction = AsyncMock(return_value=True)
    route_event = AsyncMock()
    monkeypatch.setattr(runner, "_is_reaction_to_reaction", is_reaction)
    monkeypatch.setattr(runner, "route_event", route_event)

    assert await runner.process_admitted_event(event.event_id) == []
    is_reaction.assert_awaited_once_with(event)
    route_event.assert_not_awaited()


async def test_process_admitted_event_handles_no_routes(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-no-routes")
    await temp_storage.append(event)
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    route_event = AsyncMock(return_value=(event, []))
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "route_event", route_event)
    monkeypatch.setattr(runner, "deliver_to_targets", deliver)

    assert await runner.process_admitted_event(event.event_id) == []
    route_event.assert_awaited_once_with(event)
    deliver.assert_not_awaited()


async def test_process_admitted_event_refreshes_late_native_relation_in_memory(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Late native targets affect delivery copies without rewriting evidence."""
    runner = _runner(temp_storage)
    native_target = NativeRef(
        adapter="matrix-main",
        native_channel_id="!room:example.org",
        native_message_id="$late-parent",
    )
    relation = EventRelation(
        relation_type="reply",
        target_event_id=None,
        target_native_ref=native_target,
        key=None,
        fallback_text=None,
    )
    child = msgspec.structs.replace(
        _stored_admitted_event("evt-late-child"),
        relations=(relation,),
    )
    parent = _stored_admitted_event("evt-late-parent")
    await temp_storage.append(child)
    await temp_storage.append(parent)
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="nref-late-parent",
            event_id=parent.event_id,
            adapter=native_target.adapter,
            native_channel_id=native_target.native_channel_id,
            native_message_id=native_target.native_message_id,
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
    )
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    route_event = AsyncMock(side_effect=lambda event: (event, []))
    monkeypatch.setattr(runner, "route_event", route_event)

    assert await runner.process_admitted_event(child.event_id) == []

    delivered_event = route_event.await_args.args[0]
    assert delivered_event.root_event_id == parent.event_id
    assert delivered_event.conversation_id == parent.event_id
    assert delivered_event.relations[0].target_event_id == parent.event_id

    stored_child = await temp_storage.get(child.event_id)
    assert stored_child is not None
    assert stored_child.root_event_id == child.event_id
    assert stored_child.relations[0].target_event_id is None


async def test_process_admitted_event_delivers_routed_targets(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-routed")
    await temp_storage.append(event)
    delivery = object()
    outcome = DeliveryOutcome(
        event_id=event.event_id,
        target_adapter="target",
        target_channel=None,
        route_id="route-1",
        delivery_plan_id="plan-1",
        status="success",
    )
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        runner, "route_event", AsyncMock(return_value=(event, [delivery]))
    )
    deliver = AsyncMock(return_value=[outcome])
    monkeypatch.setattr(runner, "deliver_to_targets", deliver)

    assert await runner.process_admitted_event(event.event_id) == [outcome]
    deliver.assert_awaited_once_with(event, [delivery])


@pytest.mark.parametrize(
    "failure_kind,status,error,failure_kind_detail,expected_reason",
    [
        (
            DeliveryFailureKind.CAPACITY_REJECTION,
            "permanent_failure",
            "delivery_capacity_exceeded",
            None,
            "capacity_rejection",
        ),
        (
            DeliveryFailureKind.SHUTDOWN_REJECTION,
            "permanent_failure",
            "delivery_rejected_shutdown",
            None,
            "shutdown_rejection",
        ),
        (
            DeliveryFailureKind.OUTBOX_NOT_OWNED,
            "skipped",
            "outbox persistence failed before ownership transferred",
            "outbox_creation_failed",
            "outbox_creation_failed",
        ),
    ],
)
async def test_process_admitted_event_defers_when_delivery_responsibility_not_transferred(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: DeliveryFailureKind,
    status: str,
    error: str,
    failure_kind_detail: str | None,
    expected_reason: str,
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-deferred")
    await temp_storage.append(event)
    delivery = object()
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        runner, "route_event", AsyncMock(return_value=(event, [delivery]))
    )
    outcome = DeliveryOutcome(
        event_id=event.event_id,
        target_adapter="target",
        target_channel=None,
        route_id="route-1",
        delivery_plan_id="plan-1",
        status=status,
        failure_kind=failure_kind,
        error=error,
        failure_kind_detail=failure_kind_detail,
    )
    monkeypatch.setattr(runner, "deliver_to_targets", AsyncMock(return_value=[outcome]))

    with pytest.raises(DurableIngressDeferredError, match="evt-deferred") as exc_info:
        await runner.process_admitted_event(event.event_id)

    assert exc_info.value.reasons == (expected_reason,)


async def test_process_admitted_event_accepts_terminal_existing_outbox_skip(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-terminal-outbox")
    await temp_storage.append(event)
    delivery = object()
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        runner, "route_event", AsyncMock(return_value=(event, [delivery]))
    )
    outcome = DeliveryOutcome(
        event_id=event.event_id,
        target_adapter="target",
        target_channel=None,
        route_id="route-1",
        delivery_plan_id="plan-1",
        status="skipped",
        failure_kind=DeliveryFailureKind.OUTBOX_NOT_OWNED,
        error=(
            "outbox row not owned: terminal; prior outbox_creation_failed text "
            "is diagnostic only"
        ),
        failure_kind_detail="terminal:sent",
    )
    monkeypatch.setattr(runner, "deliver_to_targets", AsyncMock(return_value=[outcome]))

    assert await runner.process_admitted_event(event.event_id) == [outcome]


async def test_partial_deferral_does_not_redeliver_successful_target(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(temp_storage)
    event = _stored_admitted_event("evt-partial-deferral")
    await temp_storage.append(event)
    route = Route(
        id="route-partial-deferral",
        source=RouteSource(
            adapter="src", channel=None, event_kinds=("message.created",)
        ),
        targets=[
            RouteTarget(adapter="target-a", channel="a"),
            RouteTarget(adapter="target-b", channel="b"),
        ],
    )
    deliveries = [
        (
            route,
            DeliveryPlan(
                plan_id=f"plan:{event.event_id}:{target.adapter}",
                event_id=event.event_id,
                target=target,
                primary_strategy=DeliveryStrategy(method="direct"),
                route_id=route.id,
            ),
        )
        for target in route.targets
    ]
    capacity = SimpleNamespace(
        delivery_limit=1,
        accepting_work=True,
        acquire_delivery=AsyncMock(side_effect=[True, False, True, True]),
        release_delivery=AsyncMock(),
    )
    runner._capacity_controller = capacity
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        runner, "route_event", AsyncMock(return_value=(event, deliveries))
    )
    delivered_plan_ids: list[str] = []

    async def deliver(_event, matched_route, plan, **kwargs):
        delivered_plan_ids.append(plan.plan_id)
        receipt = build_delivery_receipt(
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=plan.target.adapter or "",
            target_channel=plan.target.channel,
            route_id=matched_route.id,
            status="sent",
            outbox_id=kwargs["outbox_id"],
        )
        await temp_storage.append_receipt(receipt)
        return receipt

    monkeypatch.setattr(runner, "deliver_to_target", deliver)

    with pytest.raises(DurableIngressDeferredError) as exc_info:
        await runner.process_admitted_event(event.event_id)
    assert exc_info.value.reasons == ("capacity_rejection",)

    outcomes = await runner.process_admitted_event(event.event_id)

    assert delivered_plan_ids == [deliveries[0][1].plan_id, deliveries[1][1].plan_id]
    assert [outcome.status for outcome in outcomes] == ["skipped", "success"]
    assert outcomes[0].failure_kind is DeliveryFailureKind.OUTBOX_NOT_OWNED
    assert outcomes[0].failure_kind_detail == "terminal:sent"
