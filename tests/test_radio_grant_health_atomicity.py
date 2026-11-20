# medre-test-tier: compatibility
"""Per-packet-start-grant health atomicity for multipart radio plans.

Health admission precedes radio plan execution (pre-plan), so for a
multipart burst the health picture could go stale mid-burst: every
remaining packet grant issued against a link the runtime already knows is
degraded. These tests pin the grant-level contract:

* each packet-start grant re-admits against the current blocking-health
  observation as part of the same grant unit; a degraded observation
  refuses the grant with no native call and no uncertainty record;
* a mid-burst degradation stops new grants while the accepted prefix and
  wholly unsent tail stay authoritative (transient retry eligibility, not
  inspection-required);
* recovery resumes per the existing continuation rules: a newer attempt
  sends only the unsent tail, re-admitted per grant.

The pre-plan deadline fence per grant and claim/authority fencing per
grant were already atomic and remain covered by their existing suites.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from typing import Any

import msgspec
import pytest

from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.adapter_health import AdapterHealthGate
from medre.core.engine.pipeline.radio_delivery import deliver_radio_plan
from medre.core.events import DeliveryAttemptProvenance
from medre.core.events.metadata import EventMetadata, RoutingMetadata
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)

# ---------------------------------------------------------------------------
# Unit harness: grant-level semantics under real storage
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    """File-backed storage exercises the real owned checkpoint transactions."""
    store = SQLiteStorage(db_path=str(tmp_path / "radio-grant-health.db"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def wire_plan():
    return plan_radio_text("café α " * 12, prefix="[North] ", max_text_bytes=40)


async def bound_rendering(storage) -> RenderingResult:
    """Create an authoritative owned attempt with a renderer-selected wire plan."""
    item = make_outbox_item(status="pending", attempt_number=0, target_channel="0")
    item = await create_outbox_item_with_parent(storage, item)
    claimed = await storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "radio-owner"
    )
    assert any(row.outbox_id == item.outbox_id for row in claimed)
    assert await storage.reserve_outbox_attempt(item.outbox_id, "radio-owner", 0) == 1
    plan = wire_plan()
    return RenderingResult(
        event_id=item.event_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        payload={"text": plan.parts[0].wire_text, "channel_index": 0},
        attempt_provenance=DeliveryAttemptProvenance(
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
            outbox_id=item.outbox_id,
            attempt_number=1,
            source="live",
        ),
        radio_plan=plan,
    )


async def test_degraded_first_grant_refuses_without_native_call(storage):
    """A degraded observation refuses the grant; the whole plan stays unsent."""
    result = await bound_rendering(storage)
    native_calls: list[str] = []

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        return AdapterHandoffResult(native_message_id="x")

    with pytest.raises(AdapterSendError, match="radio packet grant refused"):
        await deliver_radio_plan(
            storage,
            result,
            "radio-owner",
            deliver,
            health_probe=lambda: "degraded",
        )
    assert native_calls == []
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert all(part["state"] == "unsent" for part in snapshot["parts"])


async def test_mid_burst_degradation_stops_new_grants_and_keeps_progress(storage):
    """Granted prefix stays accepted; the refused tail stays wholly unsent."""
    result = await bound_rendering(storage)
    native_calls: list[str] = []

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        return AdapterHandoffResult(native_message_id=str(len(native_calls)))

    with pytest.raises(AdapterSendError, match="radio packet grant refused"):
        await deliver_radio_plan(
            storage,
            result,
            "radio-owner",
            deliver,
            health_probe=lambda: "degraded" if len(native_calls) >= 2 else None,
        )

    # Two packets were granted and accepted before the degradation; the
    # refusal hit the third grant and no further native call happened.
    assert len(native_calls) == 2
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    states = [part["state"] for part in snapshot["parts"]]
    assert states[:2] == ["accepted", "accepted"]
    assert all(state == "unsent" for state in states[2:])

    # No uncertainty exists, so the retained plan stays continuation-eligible
    # rather than inspection-required.
    attempt = await storage.get_radio_attempt(result.outbox_id, 1)
    assert attempt is not None
    assert all(
        part.state not in {"submitting", "unknown"} for part in attempt.progress.parts
    )


async def test_degradation_during_grant_write_applies_to_next_grant(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await bound_rendering(storage)
    begin = storage.begin_radio_part
    grant_pending = asyncio.Event()
    resume_grant = asyncio.Event()
    health: str | None = None
    native_calls: list[str] = []

    async def begin_after_health_sample(
        expected: StoredRadioAttempt,
        worker_id: str | None,
        ordinal: int,
        *,
        deadline: datetime | None = None,
    ) -> StoredRadioAttempt | None:
        grant_pending.set()
        await resume_grant.wait()
        return await begin(expected, worker_id, ordinal, deadline=deadline)

    async def deliver(packet: RenderingResult) -> AdapterHandoffResult:
        assert packet.before_native_send is not None
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        return AdapterHandoffResult(native_message_id="first")

    monkeypatch.setattr(storage, "begin_radio_part", begin_after_health_sample)
    burst = asyncio.create_task(
        deliver_radio_plan(
            storage, result, "radio-owner", deliver, health_probe=lambda: health
        )
    )
    try:
        await asyncio.wait_for(grant_pending.wait(), 5)
        assert native_calls == []
        health = "degraded"
        resume_grant.set()
        with pytest.raises(AdapterSendError, match="radio packet grant refused"):
            await asyncio.wait_for(burst, 5)
    finally:
        if not burst.done():
            burst.cancel()
            await asyncio.gather(burst, return_exceptions=True)

    assert len(native_calls) == 1
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    states = [part["state"] for part in snapshot["parts"]]
    assert states[0] == "accepted"
    assert states[1:] and all(state == "unsent" for state in states[1:])


async def test_healthy_probe_preserves_burst_and_admits_every_grant(storage):
    """A healthy observation admits every grant exactly once per packet."""
    result = await bound_rendering(storage)
    native_calls: list[str] = []
    admissions = {"count": 0}

    def probe() -> str | None:
        admissions["count"] += 1
        return None

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_message_id=str(len(native_calls)),
            confirmation_level="local_transport",
        )

    handoff = await deliver_radio_plan(
        storage, result, "radio-owner", deliver, health_probe=probe
    )
    assert handoff.confirmation_level == "local_transport"
    assert native_calls == [part.wire_text for part in result.radio_plan.parts]
    assert admissions["count"] == len(result.radio_plan.parts)


@pytest.mark.parametrize("degrade_again", [False, True])
async def test_refused_tail_resumes_under_fresh_grants_after_recovery(
    storage: SQLiteStorage, degrade_again: bool
) -> None:
    """Recovery sends only the unsent tail; the accepted prefix is never resent."""
    result = await bound_rendering(storage)
    native_calls: list[str] = []

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        return AdapterHandoffResult(native_message_id=str(len(native_calls)))

    with pytest.raises(AdapterSendError, match="radio packet grant refused"):
        await deliver_radio_plan(
            storage,
            result,
            "radio-owner",
            deliver,
            health_probe=lambda: "degraded" if len(native_calls) >= 2 else None,
        )
    assert len(native_calls) == 2
    predecessor = await storage.get_radio_attempt(result.outbox_id, 1)
    assert predecessor is not None

    # The owning retry rule reserves a strictly newer attempt through the
    # production claim transfer; storage revalidates the retained history
    # before the continuation binds.
    claim = result.attempt_provenance
    now = datetime.now(timezone.utc).isoformat()
    assert await storage.mark_outbox_retry_wait(
        claim.outbox_id, now, attempt_number=claim.attempt_number
    )
    claimed = await storage.claim_due_outbox_items(now, "next-owner")
    assert len(claimed) == 1
    assert (
        await storage.reserve_outbox_attempt(
            claim.outbox_id, "next-owner", claim.attempt_number
        )
        == claim.attempt_number + 1
    )
    continuation = dataclass_replace(
        claim,
        attempt_number=claim.attempt_number + 1,
        source="retry",
    )

    tail_calls: list[str] = []
    tail_admissions = 0

    def tail_health() -> str | None:
        nonlocal tail_admissions
        tail_admissions += 1
        return "degraded" if degrade_again and tail_admissions == 2 else None

    async def resumed_deliver(packet):
        await packet.before_native_send()
        tail_calls.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_message_id=f"tail-{len(tail_calls)}",
            confirmation_level="local_transport",
        )

    resumed = dataclass_replace(
        result,
        attempt_number=claim.attempt_number + 1,
        attempt_provenance=continuation,
    )
    execution = deliver_radio_plan(
        storage,
        resumed,
        "next-owner",
        resumed_deliver,
        resume_from=predecessor,
        health_probe=tail_health,
    )
    if degrade_again:
        with pytest.raises(AdapterSendError, match="radio packet grant refused"):
            await execution
    else:
        await execution
    tail = [part.wire_text for part in predecessor.progress.plan.parts[2:]]
    assert tail_calls == (tail[:1] if degrade_again else tail)
    assert tail_admissions == (2 if degrade_again else len(tail))
    after = await storage.get_radio_attempt(result.outbox_id, 2)
    assert after is not None
    assert after.progress.handoff_complete is not degrade_again
    assert [part.state for part in after.progress.parts] == (
        ["accepted"] * 3 + ["unsent"] * (len(tail) - 1)
        if degrade_again
        else ["accepted"] * (len(tail) + 2)
    )


# ---------------------------------------------------------------------------
# Integration harness: end-to-end admission through the delivery pipeline
# ---------------------------------------------------------------------------


class RadioRenderer:
    """An owned native renderer producing a genuine multipart wire plan."""

    name = "runtime-radio"

    def can_render(self, event: Any, ctx: Any) -> bool:
        return True

    async def render(self, event: Any, ctx: Any) -> RenderingResult:
        wire = plan_radio_text(
            event.payload["text"], prefix="[North] ", max_text_bytes=40
        )
        return RenderingResult(
            event.event_id,
            "radio",
            "0",
            {"text": wire.parts[0].wire_text, "channel_index": 0},
            radio_plan=wire,
        )


class RadioAdapter:
    """Records only actual native calls after core's one-use durable grant."""

    supports_radio_multipart = True

    def __init__(self) -> None:
        self.native: list[str] = []

    async def deliver(self, packet: Any) -> Any:
        await packet.before_native_send()
        self.native.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_message_id=str(len(self.native)),
            native_channel_id="0",
            confirmation_level="local_transport",
        )


def _flip_gate() -> AdapterHealthGate:
    """Gate whose observation flips to degraded at the fifth admission.

    A replay execution consults the gate once before allocation, twice for
    pre-render and pre-native health admission, then once per packet grant;
    the flip therefore sends the first packet and refuses the second
    packet's grant.
    """
    admissions = {"count": 0}

    def health(adapter_id: str) -> str | None:
        admissions["count"] += 1
        if admissions["count"] > 4:
            return "degraded"
        return "healthy"

    return AdapterHealthGate(health)


async def _setup(storage: SQLiteStorage, adapter: RadioAdapter) -> tuple:
    event = msgspec.structs.replace(
        make_event(payload={"text": "café α " * 15}),
        metadata=EventMetadata(routing=RoutingMetadata(matched_routes=("radio",))),
    )
    await storage.append(event)
    target = RouteTarget(adapter="radio", channel="0")
    route = Route(
        id="radio-route",
        source=RouteSource(
            adapter="source", channel=None, event_kinds=("message.created",)
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="radio-plan",
        event_id=event.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=RetryPolicy(max_attempts=3, backoff_base=0, jitter=False),
    )
    renderer = RadioRenderer()
    rendering = RenderingPipeline()
    rendering.register(renderer)
    config = make_pipeline_config_for_pipeline(
        storage, Router(routes=[route]), {"radio": adapter}
    )
    config.rendering_pipeline = rendering
    runner = PipelineRunner(config)
    return event, route, plan, runner


async def test_pipeline_refuses_grants_mid_burst_and_holds_for_retry(
    storage,
):
    """End-to-end wiring: a mid-burst degradation refuses grants and holds."""
    adapter = RadioAdapter()
    event, route, plan, runner = await _setup(storage, adapter)
    runner.set_adapter_health_gate(_flip_gate())

    outcomes = await runner.deliver_to_targets(
        event,
        [(route, plan)],
        source="replay",
        replay_run_id="run-radio-health",
    )

    # The burst stopped at the refused grant: one packet was granted and
    # sent, the rest were refused with no native call, and the refusal
    # stayed transient under the plan's retry policy.
    assert len(adapter.native) == 1
    assert outcomes[0].status == "transient_failure"
    rows = await storage.list_outbox_items_for_event(event.event_id)
    assert len(rows) == 1
    assert rows[0].status == "retry_wait"
    assert rows[0].replay_run_id == "run-radio-health"
    attempt = await storage.get_radio_attempt(rows[0].outbox_id, 1)
    assert attempt is not None
    states = [part.state for part in attempt.progress.parts]
    assert states[0] == "accepted"
    assert all(state == "unsent" for state in states[1:])

    failed = await storage.list_receipts_for_event(event.event_id)
    assert [receipt.status for receipt in failed] == ["failed"]
    assert "radio packet grant refused" in (failed[0].error or "")

    # The owning retry rule finds the retained attempt as its continuation
    # predecessor: the refused tail is exactly the resumable state the
    # existing continuation rules consume on recovery.
    predecessor = await storage.get_latest_radio_attempt(
        rows[0].outbox_id, before_attempt=2
    )
    assert predecessor == attempt


async def test_packet_grants_use_the_expired_obligation_horizon(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = RadioAdapter()
    event, route, plan, runner = await _setup(storage, adapter)
    now = 0.0
    health = "healthy"
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: now
    )
    gate = AdapterHealthGate(lambda _: health, max_horizon_seconds=10)
    runner.set_adapter_health_gate(gate)
    create = storage.create_outbox_item

    async def create_before_degradation(*args: Any, **kwargs: Any):
        nonlocal now, health
        row = await create(*args, **kwargs)
        health = "degraded"
        assert gate.blocking_health("radio", obligation_id=row.outbox_id) == "degraded"
        now = 10.0
        assert gate.blocking_health("radio", obligation_id="younger") == "degraded"
        assert gate.blocking_health("radio") == "degraded"
        return row

    monkeypatch.setattr(storage, "create_outbox_item", create_before_degradation)
    outcomes = await runner.deliver_to_targets(event, [(route, plan)], source="replay")
    assert outcomes[0].status == "success"
    assert len(adapter.native) > 1
