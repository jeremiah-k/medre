"""Radio dispatch grants, retained handoffs and parent completion under real storage."""

from __future__ import annotations

import asyncio
import json

from datetime import datetime, timedelta, timezone

import pytest

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.radio_delivery import deliver_radio_plan
from medre.core.events import DeliveryAttemptProvenance
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


@pytest.fixture
async def storage(tmp_path):
    """File-backed storage exercises the real owned checkpoint transactions."""
    store = SQLiteStorage(db_path=str(tmp_path / "radio.db"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def wire_plan():
    """A genuine prefixed multibyte plan with multiple native packets."""
    return plan_radio_text("café α " * 12, prefix="[North] ", max_text_bytes=40)


async def rendering(storage):
    """Create an authoritative owned attempt and a renderer-selected wire plan."""
    from datetime import datetime, timezone

    item = make_outbox_item(status="pending", attempt_number=0)
    item = await create_outbox_item_with_parent(storage, item)
    claimed = await storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "radio-owner"
    )
    assert any(row.outbox_id == item.outbox_id for row in claimed)
    assert await storage.reserve_outbox_attempt(item.outbox_id, "radio-owner", 0) == 1
    claim = DeliveryAttemptProvenance(
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=1,
        source="live",
    )
    plan = wire_plan()
    return RenderingResult(
        event_id=item.event_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        payload={"text": plan.parts[0].wire_text, "channel_index": 0},
        attempt_provenance=claim,
        radio_plan=plan,
    )


async def test_all_parts_commit_before_whole_handoff_and_do_not_invent_a_native_id(
    storage,
):
    """Local channel acceptance retains explicit null IDs and separate strength."""
    result = await rendering(storage)
    sent, old_grants = [], []

    async def deliver(packet):
        if old_grants:
            with pytest.raises(AdapterPermanentError, match="reused"):
                await old_grants[0]()
        await packet.before_native_send()
        old_grants.append(packet.before_native_send)
        snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
        assert snapshot["parts"][len(sent)]["state"] == "submitting"
        assert all(p["state"] == "unsent" for p in snapshot["parts"][len(sent) + 1 :])
        sent.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_channel_id="0", confirmation_level="local_transport"
        )

    handoff = await deliver_radio_plan(storage, result, "radio-owner", deliver)
    assert sent == [p.wire_text for p in result.radio_plan.parts]
    assert handoff.native_message_id is None
    assert handoff.native_channel_id is None
    assert handoff.confirmation_level == "local_transport"
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    for index, part in enumerate(snapshot["parts"], 1):
        reference = json.loads(part["handoff_id"])
        assert reference == {
            "ordinal": index,
            "native_message_id": None,
            "native_channel_id": "0",
            "confirmation_level": "local_transport",
        }
    assert (await storage.get_outbox_item(result.outbox_id)).status == "in_progress"
    assert await storage.list_receipts_for_event(result.event_id) == []


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_uncertain_second_packet_keeps_accepted_prefix_and_blocks_replay(
    storage, failure
):
    """Partial acceptance never completes a parent or authorizes whole-plan resend."""
    result = await rendering(storage)
    calls = 0

    async def deliver(packet):
        nonlocal calls
        await packet.before_native_send()
        calls += 1
        if calls == 2:
            raise failure()
        return AdapterHandoffResult(
            native_message_id="first", confirmation_level="local_transport"
        )

    expected = (
        asyncio.CancelledError
        if failure is asyncio.CancelledError
        else AdapterPermanentError
    )
    with pytest.raises(expected):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert [p["state"] for p in snapshot["parts"][:3]] == [
        "accepted",
        "unknown",
        "unsent",
    ]
    with pytest.raises(AdapterPermanentError, match="already bound"):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    assert calls == 2


async def test_claim_loss_while_waiting_for_send_owner_prevents_native_invocation(
    storage,
):
    """The grant checks current ownership after the adapter's pacing boundary."""
    result = await rendering(storage)
    calls = []

    async def deliver(packet):
        storage._db.execute("UPDATE delivery_outbox SET worker_id='new-owner'")
        storage._db.commit()
        await packet.before_native_send()
        calls.append(packet.payload)
        return AdapterHandoffResult()

    with pytest.raises(AdapterPermanentError, match="native start denied"):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    assert calls == []
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert all(p["state"] == "unsent" for p in snapshot["parts"])


async def test_disconnected_before_first_grant_retains_transient_failure(storage):
    """A proven no-call outage may use existing retry policy without unknown evidence."""
    result = await rendering(storage)

    async def unavailable(packet):
        raise AdapterSendError("not connected", transient=True)

    with pytest.raises(AdapterSendError):
        await deliver_radio_plan(storage, result, "radio-owner", unavailable)
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert all(p["state"] == "unsent" for p in snapshot["parts"])


@pytest.mark.parametrize(
    "bad_handoff", [None, AdapterHandoffResult(disposition="deferred")]
)
async def test_invalid_or_deferred_handoff_cannot_complete_radio_plan(
    storage, bad_handoff
):
    """A synchronous participant cannot substitute local queue acceptance."""
    result = await rendering(storage)

    async def deliver(packet):
        await packet.before_native_send()
        return bad_handoff

    with pytest.raises(AdapterPermanentError):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert snapshot["parts"][0]["state"] == "unknown"
    assert snapshot["parts"][1]["state"] == "unsent"


async def test_failed_evidence_commit_does_not_resend_accepted_native_work(
    storage, caplog
):
    """An accepted-before-commit crash window remains submitting and fail-closed."""
    result = await rendering(storage)
    native_calls = []

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload["text"])
        storage._db.executescript("""
            CREATE TRIGGER reject_radio_evidence BEFORE UPDATE ON radio_attempt_progress
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END;
        """)
        return AdapterHandoffResult(native_message_id="accepted-before-commit")

    with pytest.raises(AdapterPermanentError, match="requires inspection"):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert snapshot["parts"][0]["state"] == "submitting"
    assert len(native_calls) == 1
    record = next(
        record
        for record in caplog.records
        if "uncertainty checkpoint failed" in record.message
    )
    assert result.outbox_id in record.message
    assert "attempt=1 part=1" in record.message
    assert record.exc_info is not None


async def test_pipeline_owns_plan_completion_and_preserves_worker_fencing(storage):
    """Full coordination supplies the actual worker and finalizes only after all parts."""
    event = make_event(payload={"text": "long radio message"})
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
    )
    calls = []

    async def untrusted_renderer_hook():
        pytest.fail("renderer-supplied native hook escaped core replacement")

    class Renderer:
        name = "radio-test"

        def can_render(self, event, ctx):
            return True

        async def render(self, event, ctx):
            return RenderingResult(
                event.event_id,
                "radio",
                "0",
                {"text": "first"},
                radio_plan=wire_plan(),
                before_native_send=untrusted_renderer_hook,
            )

    class Adapter:
        platform = "radio-test"
        supports_radio_multipart = True

        async def deliver(self, packet):
            await packet.before_native_send()
            row = await storage.get_outbox_item(packet.outbox_id)
            assert row.status == "in_progress"
            assert row.worker_id is not None
            calls.append(packet.payload["text"])
            return AdapterHandoffResult(
                native_message_id=f"id:{len(calls)}",
                confirmation_level="local_transport",
            )

    rendering_pipeline = RenderingPipeline()
    rendering_pipeline.register(Renderer())
    config = make_pipeline_config_for_pipeline(
        storage, Router(routes=[route]), {"radio": Adapter()}
    )
    config.rendering_pipeline = rendering_pipeline
    runner = PipelineRunner(config)
    outcomes = await runner.deliver_to_targets(event, [(route, plan)])
    assert outcomes[0].status == "success"
    assert calls == [part.wire_text for part in wire_plan().parts]
    rows = await storage.list_outbox_items_for_event(event.event_id)
    assert len(rows) == 1 and rows[0].status == "sent"
    assert await storage.list_native_refs_for_event(event.event_id) == []


async def test_aggregate_confirmation_never_exceeds_weakest_packet(storage):
    """Per-packet local evidence cannot be promoted by a stronger neighbor."""
    result = await rendering(storage)
    calls = 0

    async def deliver(packet):
        nonlocal calls
        await packet.before_native_send()
        calls += 1
        return AdapterHandoffResult(
            native_message_id=str(calls),
            confirmation_level="unknown" if calls == 2 else "local_transport",
        )

    handoff = await deliver_radio_plan(storage, result, "radio-owner", deliver)
    assert handoff.confirmation_level == "unknown"
    assert handoff.native_message_id is None


async def test_deadline_expiring_during_pacing_prevents_native_submission(storage):
    """A long plan cannot keep sending after its delivery deadline."""
    from datetime import datetime, timezone

    result = await rendering(storage)
    native_calls = []

    async def deliver(packet):
        await packet.before_native_send()
        native_calls.append(packet.payload)
        return AdapterHandoffResult()

    with pytest.raises(AdapterPermanentError, match="deadline exceeded"):
        await deliver_radio_plan(
            storage,
            result,
            "radio-owner",
            deliver,
            deadline=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
    assert native_calls == []
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert all(part["state"] == "unsent" for part in snapshot["parts"])


async def test_lost_claim_during_uncertainty_checkpoint_is_correlated(storage, caplog):
    """A rejected observation is visible without erasing its blocking submission."""
    result = await rendering(storage)

    async def deliver(packet):
        await packet.before_native_send()
        storage._db.execute("UPDATE delivery_outbox SET worker_id='replacement'")
        storage._db.commit()
        raise TimeoutError("acceptance unknown")

    with pytest.raises(AdapterPermanentError):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    assert result.outbox_id in caplog.text
    assert "uncertainty checkpoint not committed" in caplog.text
    assert "attempt=1 part=1" in caplog.text
    snapshot = await storage.get_radio_attempt_snapshot(result.outbox_id, 1)
    assert snapshot["parts"][0]["state"] == "submitting"


async def test_content_release_between_packets_preserves_accepted_prefix(storage):
    from datetime import datetime, timezone

    result = await rendering(storage)
    calls = []

    async def deliver(packet):
        await packet.before_native_send()
        calls.append(packet.payload)
        if len(calls) == 1:
            await storage._write(
                "UPDATE event_content_dependencies SET released_at=? "
                "WHERE event_id=? AND owner_kind IN ('ingress','append')",
                (
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    result.event_id,
                ),
            )
        return AdapterHandoffResult()

    with pytest.raises(AdapterPermanentError):
        await deliver_radio_plan(storage, result, "radio-owner", deliver)
    checkpoint = await storage.get_radio_attempt(result.outbox_id, 1)
    assert len(calls) == 1
    assert checkpoint.progress.parts[0].state == "accepted"
    assert all(part.state == "unsent" for part in checkpoint.progress.parts[1:])


async def test_radio_inspection_does_not_select_wire_plan(storage):
    import sqlite3

    import msgspec

    result = await rendering(storage)
    saved = await storage.bind_radio_attempt(
        result.attempt_provenance, "radio-owner", result.radio_plan
    )
    assert saved is not None

    def authorize(action, table, column, database, source):
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ
            and (table, column) == ("radio_attempt_progress", "plan_json")
            else sqlite3.SQLITE_OK
        )

    await storage._run_in_thread(storage._require_db().set_authorizer, authorize)
    try:
        metadata = await storage.inspect_radio_attempt(result.outbox_id, 1)
        assert metadata.to_data()["content_withheld"] is True
        assert "wire_text" not in msgspec.json.encode(metadata.to_data()).decode()
        assert metadata.parts == saved.progress.parts
    finally:
        await storage._run_in_thread(storage._require_db().set_authorizer, None)


@pytest.mark.parametrize("expired", ["content", "deadline"])
async def test_expiry_during_radio_checkpoint_wait_cannot_grant_native_send(
    storage, monkeypatch, expired
):
    from datetime import datetime, timedelta, timezone

    import medre.core.engine.pipeline.radio_delivery as delivery_module
    import medre.core.storage.sqlite._radio as radio_module

    result = await rendering(storage)

    class Clock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(delivery_module, "datetime", Clock)
    monkeypatch.setattr(radio_module, "datetime", Clock)
    deadline = Clock.current + timedelta(seconds=10)
    original_begin = storage.begin_radio_part
    native_calls = 0

    async def queued_checkpoint(*args, **kwargs):
        if expired == "content":
            await storage._write(
                "UPDATE event_content_dependencies SET released_at=? WHERE event_id=?",
                (Clock.current.isoformat(), result.event_id),
            )
        else:
            Clock.current = deadline
        return await original_begin(*args, **kwargs)

    monkeypatch.setattr(storage, "begin_radio_part", queued_checkpoint)

    async def send(packet):
        nonlocal native_calls
        await packet.before_native_send()
        native_calls += 1
        return AdapterHandoffResult()

    with pytest.raises(AdapterPermanentError):
        await deliver_radio_plan(
            storage, result, "radio-owner", send, deadline=deadline
        )
    assert native_calls == 0
    saved = await storage.get_radio_attempt(result.outbox_id, 1)
    assert all(part.state == "unsent" for part in saved.progress.parts)


async def test_radio_start_compares_offset_lease_as_an_instant(storage):
    from datetime import datetime, timedelta, timezone

    result = await rendering(storage)
    lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).astimezone(
        timezone(timedelta(hours=-5))
    )
    await storage._write(
        "UPDATE delivery_outbox SET lease_until=? WHERE outbox_id=?",
        (lease.isoformat(), result.outbox_id),
    )
    bound = await storage.bind_radio_attempt(
        result.attempt_provenance, "radio-owner", result.radio_plan
    )
    assert bound is not None
    assert await storage.begin_radio_part(bound, "radio-owner", 1) is not None


async def test_native_grant_refuses_blank_worker_and_expired_deadline(
    storage, monkeypatch
):
    """The one-use native grant never starts without a worker or live deadline."""
    from medre.core.engine.pipeline import radio_delivery as module

    result = await rendering(storage)
    claim = result.attempt_provenance
    bound = await storage.bind_radio_attempt(claim, "radio-owner", result.radio_plan)
    assert bound is not None

    blank = module._Submission(
        storage=storage, checkpoint=bound, worker_id="", ordinal=1
    )
    with pytest.raises(AdapterPermanentError, match="requires a worker"):
        await blank.begin()

    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    ticking = iter(
        [
            deadline - timedelta(seconds=1),
            deadline + timedelta(seconds=1),
        ]
    )

    class _AdvancingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticking)

    grant = module._Submission(
        storage=storage,
        checkpoint=bound,
        worker_id="radio-owner",
        ordinal=1,
        deadline=deadline,
    )
    monkeypatch.setattr(module, "datetime", _AdvancingDatetime)
    with pytest.raises(
        AdapterPermanentError, match="deadline exceeded before native submission"
    ):
        await grant.begin()
