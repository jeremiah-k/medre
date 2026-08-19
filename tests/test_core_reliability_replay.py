"""Core reliability: replay rendering fidelity and durable suppression."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import DeliveryReceipt
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
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
            storage=temp_storage, router=Router(routes=[route]), adapters={}
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
    assert receipts[-1].failure_kind == "replay_duplicate_suppressed"
    assert receipts[-1].replay_run_id == "run-42"


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
