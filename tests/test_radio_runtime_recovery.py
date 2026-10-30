"""Configured retry ownership continues only proven-unsent native radio work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.contracts.adapter import AdapterSendError
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


@dataclass
class RadioRenderer:
    """An owned native renderer with controllable configuration changes."""

    name: str = "runtime-radio"
    budget: int = 40
    planned: bool = True

    def can_render(self, event, ctx):
        return True

    async def render(self, event, ctx):
        wire = plan_radio_text(
            event.payload["text"], prefix="[North] ", max_text_bytes=self.budget
        )
        return RenderingResult(
            event.event_id,
            "radio",
            "0",
            {"text": wire.parts[0].wire_text, "channel_index": 0},
            radio_plan=wire if self.planned else None,
        )


class RadioAdapter:
    """Record only actual native calls after core's one-use durable grant."""

    supports_radio_multipart = True

    def __init__(self, platform):
        self.platform = platform
        self.native = []
        self.failure = "before"
        self.retry_hint = 0

    async def deliver(self, packet):
        if not self.native and self.failure == "first":
            raise AdapterSendError("radio unavailable before first grant")
        if len(self.native) == 1 and self.failure == "before":
            raise AdapterSendError(
                "radio disconnected before grant", retry_after_seconds=self.retry_hint
            )
        await packet.before_native_send()
        self.native.append(packet.payload["text"])
        if len(self.native) == 2 and self.failure == "during":
            raise AdapterSendError("native acceptance is unknown")
        return AdapterHandoffResult(
            native_message_id=(
                None if self.platform == "meshcore" else str(len(self.native))
            ),
            native_channel_id="0",
            confirmation_level="local_transport",
        )


@pytest.fixture
async def radio(tmp_path):
    """Real SQLite, routing, rendering and pipeline coordination share one owner."""
    storage = SQLiteStorage(db_path=str(tmp_path / "runtime-radio.db"))
    await storage.initialize()
    try:
        yield storage
    finally:
        await storage.close()


async def setup(storage, platform="meshtastic", *, retry=True, attempts=3):
    """Create a configured text route without starting any transport or SDK."""
    event = make_event(payload={"text": "café α " * 15})
    await storage.append(event)
    target = RouteTarget(adapter="radio", channel="0")
    route = Route(
        id="radio-route",
        source=RouteSource(
            adapter="source", channel=None, event_kinds=("message.created",)
        ),
        targets=[target],
    )
    policy = (
        RetryPolicy(max_attempts=attempts, backoff_base=0, jitter=False)
        if retry
        else None
    )
    plan = DeliveryPlan(
        plan_id="radio-plan",
        event_id=event.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=policy,
    )
    renderer, adapter = RadioRenderer(), RadioAdapter(platform)
    rendering = RenderingPipeline()
    rendering.register(renderer)
    config = make_pipeline_config_for_pipeline(
        storage, Router(routes=[route]), {"radio": adapter}
    )
    config.rendering_pipeline = rendering
    runner = PipelineRunner(config)
    return event, route, plan, renderer, adapter, runner


async def retry_once(storage, runner):
    """Drive one scheduler cycle deterministically, without polling or sleeps."""
    worker = RetryWorker(storage, runner, None, enabled=True)
    await worker._process_due(datetime.now(timezone.utc) + timedelta(seconds=10))
    return worker


@pytest.mark.parametrize("platform", ["meshtastic", "meshcore"])
async def test_transient_between_parts_resumes_only_unsent_tail(radio, platform):
    """Native acceptance survives a retry without repeating the accepted prefix."""
    event, route, plan, renderer, adapter, runner = await setup(radio, platform)
    first = await runner.deliver_to_targets(event, [(route, plan)])
    rows = await radio.list_outbox_items_for_event(event.event_id)
    assert first[0].status == "transient_failure"
    assert len(rows) == 1 and rows[0].status == "retry_wait"
    before = await radio.get_radio_attempt(rows[0].outbox_id, 1)
    assert before.progress.parts[0].state == "accepted"
    assert all(part.state == "unsent" for part in before.progress.parts[1:])
    adapter.failure = None
    worker = await retry_once(radio, runner)
    assert worker.state.succeeded == 1
    assert adapter.native == [part.wire_text for part in before.progress.plan.parts]
    row = await radio.get_outbox_item(rows[0].outbox_id)
    assert row.status == "sent" and row.attempt_number == 2
    assert await radio.get_radio_attempt(row.outbox_id, 1) == before
    after = await radio.get_radio_attempt(row.outbox_id, 2)
    assert after.progress.handoff_complete
    assert json.loads(after.progress.parts[0].handoff_id)["origin"] == {
        "outbox_id": row.outbox_id,
        "attempt_number": 1,
    }
    assert await radio.list_native_refs_for_event(event.event_id) == []


async def test_reopened_storage_resumes_owned_unfinished_tail(radio):
    """A fresh retry owner uses durable progress after the prior process exits."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    await radio.close()
    await radio.initialize()
    adapter.failure = None
    worker = await retry_once(radio, runner)
    assert worker.state.succeeded == 1
    assert (await radio.get_outbox_item(row.outbox_id)).status == "sent"
    assert len(adapter.native) == len(
        (await radio.get_radio_attempt(row.outbox_id, 1)).progress.parts
    )


@pytest.mark.parametrize("change", ["budget", "single_packet"])
async def test_changed_rendering_never_bypasses_retained_radio_progress(radio, change):
    """A retry cannot fall through to ordinary sending after multipart settings change."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    await runner.deliver_to_targets(event, [(route, plan)])
    if change == "budget":
        renderer.budget = 50
    else:
        renderer.planned = False
    adapter.failure = None
    await retry_once(radio, runner)
    assert len(adapter.native) == 1
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "dead_lettered"


async def test_unknown_native_submission_does_not_enter_automatic_retry(radio):
    """Retry policy cannot reinterpret a consumed native grant as unsent work."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    adapter.failure = "during"
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "dead_lettered"
    snapshot = await radio.get_radio_attempt(row.outbox_id, 1)
    assert [part.state for part in snapshot.progress.parts[:3]] == [
        "accepted",
        "unknown",
        "unsent",
    ]
    adapter.failure = None
    await retry_once(radio, runner)
    assert len(adapter.native) == 2


@pytest.mark.parametrize("retry,attempts", [(False, 3), (True, 1)])
async def test_retry_opt_in_and_budget_remain_authoritative(radio, retry, attempts):
    """Eligible unsent work does not override absent policy or an exhausted budget."""
    event, route, plan, renderer, adapter, runner = await setup(
        radio, retry=retry, attempts=attempts
    )
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "dead_lettered"
    adapter.failure = None
    await retry_once(radio, runner)
    assert len(adapter.native) == 1


async def test_outage_before_first_packet_uses_existing_full_retry(radio):
    """A wholly unsent plan retains ordinary bounded retry behavior."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    adapter.failure = "first"
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "retry_wait"
    before = await radio.get_radio_attempt(row.outbox_id, 1)
    assert all(part.state == "unsent" for part in before.progress.parts)
    adapter.failure = None
    worker = await retry_once(radio, runner)
    assert worker.state.succeeded == 1
    assert adapter.native == [part.wire_text for part in before.progress.plan.parts]


async def test_repeated_pregrant_outage_exhausts_existing_attempt_budget(radio):
    """A resumable tail cannot create unlimited retry generations."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    await runner.deliver_to_targets(event, [(route, plan)])
    await retry_once(radio, runner)
    await retry_once(radio, runner)
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "dead_lettered" and row.attempt_number == 3
    assert len(adapter.native) == 1
    adapter.failure = None
    await retry_once(radio, runner)
    assert len(adapter.native) == 1


async def test_reserved_generation_without_radio_work_does_not_erase_prefix(radio):
    """Reservation recovery can consume a generation before selecting retained native facts."""
    from unittest.mock import AsyncMock

    event, route, plan, renderer, adapter, runner = await setup(radio)
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    real_delivery = runner.deliver_to_target

    class ProcessExit(BaseException):
        """Fault injection leaves a reserved attempt without its delivery receipt."""

    runner.deliver_to_target = AsyncMock(side_effect=ProcessExit())
    try:
        with pytest.raises(ProcessExit):
            await retry_once(radio, runner)
    finally:
        runner.deliver_to_target = real_delivery
    interrupted = await radio.get_outbox_item(row.outbox_id)
    assert interrupted.active_attempt == 2
    assert await radio.get_radio_attempt(row.outbox_id, 2) is None
    await radio.close()
    await radio.initialize()
    adapter.failure = None
    worker = RetryWorker(radio, runner, None, enabled=True)
    # Reclaim the interrupted reservation after its lease expires. The first
    # cycle reconciles it; the next reserves a distinct native attempt.
    await worker._process_due(datetime.now(timezone.utc) + timedelta(seconds=120))
    assert len(adapter.native) == 1
    await worker._process_due(datetime.now(timezone.utc) + timedelta(seconds=130))
    completed = await radio.get_outbox_item(row.outbox_id)
    assert completed.status == "sent" and completed.attempt_number == 3
    assert await radio.get_radio_attempt(row.outbox_id, 2) is None
    saved = await radio.get_radio_attempt(row.outbox_id, 3)
    assert saved.progress.handoff_complete
    assert (
        json.loads(saved.progress.parts[0].handoff_id)["origin"]["attempt_number"] == 1
    )
    assert len(adapter.native) == len(saved.progress.parts)


@pytest.mark.parametrize("state", ["submitting", "unknown"])
async def test_reclaimed_native_uncertainty_stays_held_without_fresh_send(radio, state):
    """An unfinalized native grant is never reclassified as an unsent tail."""
    from dataclasses import replace

    from medre.core.engine.pipeline.radio_delivery import _handoff_reference
    from medre.core.events.delivery import DeliveryAttemptProvenance
    from medre.core.storage.radio_handoff import plain_channel_payload_fingerprint
    from tests.helpers.retry_plan import ROUTE_DECISION_METADATA
    from tests.helpers.storage_outbox import (
        create_outbox_item_with_parent,
        make_outbox_item,
    )

    event, route, plan, renderer, adapter, runner = await setup(radio)
    item = replace(
        make_outbox_item(
            status="in_progress",
            event_id=event.event_id,
            target_adapter="radio",
            target_channel="0",
            delivery_plan_id=plan.plan_id,
        ),
        worker_id="pipeline:interrupted-owner",
        lease_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        metadata=dict(ROUTE_DECISION_METADATA),
    )
    item = await create_outbox_item_with_parent(radio, item)
    claim = DeliveryAttemptProvenance(
        event.event_id, plan.plan_id, "radio", "0", item.outbox_id, 1, "live"
    )
    result = await renderer.render(event, None)
    checkpoint = await radio.bind_radio_attempt(
        claim, item.worker_id, result.radio_plan
    )
    checkpoint = await radio.begin_radio_part(checkpoint, item.worker_id, 1)
    checkpoint = await radio.observe_radio_part(
        checkpoint,
        item.worker_id,
        1,
        handoff_id=_handoff_reference(
            AdapterHandoffResult(
                native_channel_id="0", confirmation_level="local_transport"
            ),
            1,
            plain_channel_payload_fingerprint(0),
        ),
    )
    checkpoint = await radio.begin_radio_part(checkpoint, item.worker_id, 2)
    if state == "unknown":
        checkpoint = await radio.observe_radio_part(checkpoint, item.worker_id, 2)
    # A crashed worker becomes reclaimable only after its real lease expires.
    await radio._write(
        "UPDATE delivery_outbox SET lease_until=? WHERE outbox_id=?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            item.outbox_id,
        ),
    )
    await radio.close()
    await radio.initialize()
    adapter.failure = None
    await retry_once(radio, runner)
    assert adapter.native == []
    assert (await radio.get_outbox_item(item.outbox_id)).status == "dead_lettered"
    assert await radio.get_radio_attempt(item.outbox_id, 1) == checkpoint


async def test_tail_retry_preserves_transport_backoff_hint(radio):
    """Recoverable partial work respects the same transport retry minimum as ordinary work."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    adapter.retry_hint = 5
    started = datetime.now(timezone.utc)
    await runner.deliver_to_targets(event, [(route, plan)])
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert datetime.fromisoformat(row.next_attempt_at) >= started + timedelta(seconds=5)
    adapter.failure = None
    worker = RetryWorker(radio, runner, None, enabled=True)
    await worker._process_due(datetime.now(timezone.utc))
    assert len(adapter.native) == 1
    await retry_once(radio, runner)
    assert (await radio.get_outbox_item(row.outbox_id)).status == "sent"


async def test_claim_loss_after_tail_binding_blocks_native_call(radio, monkeypatch):
    """A validated predecessor does not bypass the independently held dispatch worker."""
    event, route, plan, renderer, adapter, runner = await setup(radio)
    await runner.deliver_to_targets(event, [(route, plan)])
    original = radio.resume_radio_attempt

    async def steal_claim(*args, **kwargs):
        saved = await original(*args, **kwargs)
        assert saved is not None
        radio._db.execute("UPDATE delivery_outbox SET worker_id='another-owner'")
        radio._db.commit()
        return saved

    monkeypatch.setattr(radio, "resume_radio_attempt", steal_claim)
    adapter.failure = None
    await retry_once(radio, runner)
    assert len(adapter.native) == 1
    row = (await radio.list_outbox_items_for_event(event.event_id))[0]
    assert row.status == "in_progress" and row.worker_id == "another-owner"
