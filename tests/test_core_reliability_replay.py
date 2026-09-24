"""Core reliability: replay rendering fidelity and durable suppression."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.delivery_coordinator import _DeliveryContext
from medre.core.events import DeliveryReceipt
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


async def test_re_render_uses_persisted_live_rendering_context(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = make_event(event_id="core-reliability-render-context")
    await temp_storage.append(event)
    evidence = json.dumps(
        {
            "schema_version": "1",
            "renderer": "text",
            "delivery_strategy": "fallback_text",
            # Receipt target identity is authoritative even when historical
            # presentation evidence contains stale adapter metadata.
            "target_adapter": "stale-evidence-adapter",
            "target_platform": "meshcore",
            "target_channel": "7",
            "max_text_chars": 123,
            "max_text_bytes": 321,
            "capability_level": "fallback",
            "capability_policy": None,
            "fallback_applied": "thread_fallback_text",
            "truncated": False,
            "rendered_text_chars": 5,
            "rendered_text_bytes": 5,
            "original_text_chars": 5,
            "original_text_bytes": 5,
            "conversation_id": None,
            "root_event_id": None,
            "relation_evidence": [],
            "source_origin_label": "Ops Radio",
        }
    )
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-render-context",
            event_id=event.event_id,
            delivery_plan_id="plan-render-context",
            target_adapter="dest",
            target_channel="7",
            route_id="route-render-context",
            status="sent",
            rendering_evidence=evidence,
        )
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(), adapters={}
        )
    )
    render = AsyncMock(return_value="rendered")
    monkeypatch.setattr(runner._rendering_pipeline, "render", render)
    enrich = AsyncMock(return_value=event)
    monkeypatch.setattr(runner, "_enrich_relations_for_target", enrich)

    assert await runner.render_replay_event(event) == ["rendered"]
    enrich.assert_awaited_once_with(event, "dest", "7")
    render.assert_awaited_once_with(
        event,
        "dest",
        "7",
        target_platform="meshcore",
        max_text_chars=123,
        max_text_bytes=321,
        delivery_strategy="fallback_text",
        capability_level="fallback",
        source_origin_label="Ops Radio",
    )


async def test_re_render_prefers_live_evidence_over_later_delivery_sources(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = make_event(event_id="render-context-source-precedence")
    await temp_storage.append(event)

    def evidence(label: str) -> str:
        return json.dumps(
            {
                "delivery_strategy": "direct",
                "capability_level": "native",
                "source_origin_label": label,
            }
        )

    for index, (source, label) in enumerate(
        [("live", "Live"), ("retry", "Retry"), ("replay", "Replay")]
    ):
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id=f"rcpt-source-{index}",
                event_id=event.event_id,
                delivery_plan_id="plan-source",
                target_adapter="dest",
                target_channel="room",
                route_id="route-source",
                status="sent",
                source=source,
                rendering_evidence=evidence(label),
            )
        )

    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(), adapters={}
        )
    )
    render = AsyncMock(return_value="rendered")
    monkeypatch.setattr(runner._rendering_pipeline, "render", render)
    monkeypatch.setattr(
        runner, "_enrich_relations_for_target", AsyncMock(return_value=event)
    )

    assert await runner.render_replay_event(event) == ["rendered"]
    assert render.await_args.kwargs["source_origin_label"] == "Live"


@pytest.mark.parametrize("accepted_status", ["queued", "sent"])
@pytest.mark.parametrize(
    ("stored_channel", "target_channel"),
    [("room", "room"), ("", None)],
)
async def test_same_replay_run_suppresses_already_accepted_target(
    temp_storage: SQLiteStorage,
    accepted_status: str,
    stored_channel: str,
    target_channel: str | None,
) -> None:
    event = make_event(
        event_id="core-reliability-replay-suppress", source_adapter="src"
    )
    await temp_storage.append(event)
    route = Route(
        id="route-replay",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[RouteTarget(adapter="dest", channel=target_channel)],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay",
        event_id=event.event_id,
        target=route.targets[0],
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-replay-accepted",
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter="dest",
            target_channel=stored_channel,
            route_id=route.id,
            status=accepted_status,  # type: ignore[arg-type]
            source="replay",
            replay_run_id="run-42",
        )
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={
                "dest": FakePresentationAdapter(adapter_id="dest", channel="room")
            },
        )
    )

    outcomes = await runner._deliver_to_targets_fan_out(
        event, [(route, plan)], source="replay", replay_run_id="run-42"
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "skipped"
    assert outcomes[0].failure_kind is not None
    assert outcomes[0].failure_kind.value == "replay_duplicate_suppressed"
    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert [receipt.receipt_id for receipt in receipts] == ["rcpt-replay-accepted"]
    assert receipts[0].status == accepted_status
    # Same-run idempotency is an execution skip, not a new lifecycle fact.
    # Persisting an outbox-less suppression receipt here would incorrectly
    # outrank the already-accepted delivery in current authority.
    assert outcomes[0].receipt is None
    current = await temp_storage.delivery_status(
        DeliveryIdentity(event.event_id, plan.plan_id, "dest", target_channel)
    )
    assert current is not None
    assert current.receipt_id == "rcpt-replay-accepted"
    assert current.status == accepted_status


async def test_same_replay_run_durable_claim_skips_before_capacity(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = make_event(event_id="core-reliability-replay-claim", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-replay-claim",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-claim",
        event_id=event.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={
                "dest": FakePresentationAdapter(adapter_id="dest", channel="room")
            },
        )
    )
    claim = await runner._outbox_manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id="run-claimed",
    )
    assert claim.replay_duplicate is False

    acquire = AsyncMock(side_effect=AssertionError("capacity must not be consulted"))
    monkeypatch.setattr(
        runner._delivery_coordinator, "_acquire_capacity_or_reject", acquire
    )

    outcomes = await runner._deliver_to_targets_fan_out(
        event, [(route, plan)], source="replay", replay_run_id="run-claimed"
    )

    assert outcomes[0].status == "skipped"
    assert outcomes[0].failure_kind is not None
    assert outcomes[0].failure_kind.value == "replay_duplicate_suppressed"
    assert outcomes[0].failure_kind_detail == "replay_run_claimed:in_progress"
    acquire.assert_not_awaited()
    assert await temp_storage.list_receipts_for_event(event.event_id) == []


async def test_replay_origin_retry_is_not_treated_as_replay_duplicate(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(event_id="core-reliability-retry-origin", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-retry-origin",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-retry-origin",
        event_id=event.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(routes=[route]), adapters={}
        )
    )
    await runner._outbox_manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id="run-retry-origin",
    )
    ctx = _DeliveryContext(
        event=event,
        route=route,
        plan=plan,
        source="retry",
        replay_run_id="run-retry-origin",
        cached_get_fn=None,
        cached_list_fn=None,
        started_at=time.monotonic(),
    )

    assert (
        await runner._delivery_coordinator._replay_duplicate_outcome(ctx, None) is None
    )


async def test_stale_same_run_receipt_does_not_override_current_authority(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(event_id="core-reliability-replay-stale", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-replay-stale",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-stale",
        event_id=event.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    outbox_id = "obox-replay-stale-authority"
    stale = DeliveryReceipt(
        receipt_id="rcpt-replay-stale-same-run",
        event_id=event.event_id,
        delivery_plan_id=plan.plan_id,
        target_adapter="dest",
        target_channel="room",
        route_id=route.id,
        status="sent",
        source="replay",
        replay_run_id="run-42",
        outbox_id=outbox_id,
    )
    current = DeliveryReceipt(
        receipt_id="rcpt-replay-current-other-run",
        event_id=event.event_id,
        delivery_plan_id=plan.plan_id,
        target_adapter="dest",
        target_channel="room",
        route_id=route.id,
        status="sent",
        source="replay",
        replay_run_id="run-other",
        outbox_id=outbox_id,
    )
    await temp_storage.append_receipt(stale)
    await temp_storage.append_receipt(current)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event.event_id,
            route_id=route.id,
            delivery_plan_id=plan.plan_id,
            target_adapter="dest",
            target_channel="room",
            attempt_number=1,
            status="in_progress",
            worker_id="seed-worker",
        )
    )
    assert await temp_storage.mark_outbox_sent(
        outbox_id,
        receipt_id=current.receipt_id,
        attempt_number=1,
        expected_worker_id="seed-worker",
    )

    adapter = FakePresentationAdapter(adapter_id="dest", channel="room")
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={"dest": adapter},
        )
    )
    outcomes = await runner._deliver_to_targets_fan_out(
        event, [(route, plan)], source="replay", replay_run_id="run-42"
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "success"
    assert len(adapter.delivered_payloads) == 1
    authority = await temp_storage.delivery_status(
        DeliveryIdentity(event.event_id, plan.plan_id, "dest", "room")
    )
    assert authority is not None
    assert authority.replay_run_id == "run-42"
    assert authority.receipt_id not in {stale.receipt_id, current.receipt_id}
    assert authority.parent_receipt_id == current.receipt_id


async def test_same_replay_run_does_not_treat_suppression_as_dispatch_claim(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(
        event_id="core-reliability-replay-suppressed", source_adapter="src"
    )
    await temp_storage.append(event)
    route = Route(
        id="route-replay-suppressed",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[RouteTarget(adapter="dest", channel="room")],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-suppressed",
        event_id=event.event_id,
        target=route.targets[0],
        primary_strategy=DeliveryStrategy(method="skip"),
    )
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-replay-suppressed",
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter="dest",
            target_channel="room",
            route_id=route.id,
            status="suppressed",
            failure_kind="capability_suppressed",
            source="replay",
            replay_run_id="run-suppressed",
        )
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={
                "dest": FakePresentationAdapter(adapter_id="dest", channel="room")
            },
        )
    )

    outcomes = await runner._deliver_to_targets_fan_out(
        event, [(route, plan)], source="replay", replay_run_id="run-suppressed"
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "skipped"
    assert outcomes[0].failure_kind is not None
    assert outcomes[0].failure_kind.value != "replay_duplicate_suppressed"
    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert len(receipts) == 2
    assert all(receipt.status == "suppressed" for receipt in receipts)
    assert all(receipt.replay_run_id == "run-suppressed" for receipt in receipts)
    assert (
        await temp_storage.list_outbox_items_for_delivery(
            DeliveryIdentity(event.event_id, plan.plan_id, "dest", "room")
        )
        == []
    )


@pytest.mark.parametrize("malformed_capability", [[], {}])
async def test_re_render_defaults_malformed_capability_evidence_to_native(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    malformed_capability: object,
) -> None:
    event = make_event(event_id="render-context-malformed-capability")
    await temp_storage.append(event)
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-malformed-capability",
            event_id=event.event_id,
            delivery_plan_id="plan-malformed-capability",
            target_adapter="dest",
            target_channel="room",
            route_id="route-malformed-capability",
            status="sent",
            source="live",
            rendering_evidence=json.dumps(
                {
                    "delivery_strategy": "direct",
                    "capability_level": malformed_capability,
                }
            ),
        )
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=Router(), adapters={}
        )
    )
    render = AsyncMock(return_value="rendered")
    monkeypatch.setattr(runner._rendering_pipeline, "render", render)
    monkeypatch.setattr(
        runner, "_enrich_relations_for_target", AsyncMock(return_value=event)
    )

    assert await runner.render_replay_event(event) == ["rendered"]
    assert render.await_args.kwargs["capability_level"] == "native"
