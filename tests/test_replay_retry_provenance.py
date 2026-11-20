"""Replay-origin provenance survives the retry lane.

A replay generation dispatches retained content on the still-live source
admission hold: ``_outbox_content_authority`` fails closed for any row the
allocator did not mark ``dispatch_source="replay"``.  The guarded attempt
reservation must therefore preserve that origin marker when a replay-origin
generation enters the retry lane — rewriting it to ``retry`` dead-letters a
deliverable message with ``content_authority_denied`` on the recovery
redispatch.  A live-origin row must never gain replay authority: its retry
lane keeps ``dispatch_source="retry"`` and still requires its own adopted
content dependency, even while the event's admission hold is live.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import msgspec

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryReceipt
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_text import RadioTextPlan, plan_radio_text
from medre.core.routing import Router, RouteSource, RouteTarget
from medre.core.routing.models import Route
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.delivery_callbacks import make_post_handoff_observation
from tests.helpers.pipeline import make_pipeline_config_for_pipeline
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import create_outbox_item_with_parent

_EVENT = "evt-replay-retry-provenance"
_PLAN_ID = "plan-replay-retry"
_ROUTE_ID = "route-replay-retry"
_ADAPTER = "fake_presentation"
_CHANNEL = "ch-a"
_RUN_ID = "run-retry-provenance"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=120)


def _direct_snapshot() -> OutboxDispatchSnapshotWrite:
    """Freeze the reduced direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": None,
            "capability_field": None,
            "capability_reason": None,
            "deadline": None,
        },
    )


async def _admit_event_with_hold(
    store: SQLiteStorage,
    event_id: str = _EVENT,
    *,
    hold_minutes: int = 30,
) -> None:
    """Admit a canonical event whose source admission hold is still live."""
    await store.admit_ingress(
        msgspec.structs.replace(
            make_storage_event(event_id=event_id),
            payload={"body": "replayable body"},
        ),
        None,
        "live",
        content_deadline=_now() + timedelta(minutes=hold_minutes),
    )


async def _create_replay_generation(
    store: SQLiteStorage,
    *,
    event_id: str = _EVENT,
    outbox_id: str = "obox-replay-retry",
) -> DeliveryOutboxItem:
    """Allocate one replay generation the way the delivery coordinator does."""
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id=_ROUTE_ID,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=_CHANNEL,
        status="pending",
        dispatch_source="replay",
        replay_run_id=_RUN_ID,
    )
    created = await store.create_outbox_item(
        item,
        allocate_new_generation=True,
        dispatch_snapshot=_direct_snapshot(),
    )
    assert created.dispatch_source == "replay"
    assert created.replay_run_id == _RUN_ID
    return created


async def _claim(
    store: SQLiteStorage, outbox_id: str, worker_id: str
) -> DeliveryOutboxItem:
    claimed = await store.claim_due_outbox_items(
        now=_now().isoformat(),
        worker_id=worker_id,
        lease_seconds=7200,
        limit=10,
    )
    assert [item.outbox_id for item in claimed] == [outbox_id]
    return claimed[0]


async def _finalize_transient_failure(
    store: SQLiteStorage,
    outbox_id: str,
    *,
    worker_id: str,
    attempt_number: int,
) -> None:
    """Finalize one failed attempt into the retry lane, as a refusal would."""
    await store.mark_outbox_retry_wait(
        outbox_id,
        next_attempt_at=_past().isoformat(),
        failure_kind="adapter_transient",
        error_summary="seeded transient refusal",
        attempt_number=attempt_number,
        expected_worker_id=worker_id,
    )


async def _append_replay_attempt_receipt(
    store: SQLiteStorage,
    *,
    outbox_id: str,
    event_id: str,
    attempt_number: int,
    status: str = "failed",
    source: str = "replay",
) -> DeliveryReceipt:
    receipt = DeliveryReceipt(
        receipt_id=f"rcpt-{outbox_id}-a{attempt_number}",
        event_id=event_id,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=_CHANNEL,
        route_id=_ROUTE_ID,
        status=status,  # type: ignore[arg-type]
        failure_kind="adapter_transient" if status == "failed" else None,
        error="seeded replay attempt failure" if status == "failed" else None,
        attempt_number=attempt_number,
        source=source,  # type: ignore[arg-type]
        replay_run_id=_RUN_ID,
        outbox_id=outbox_id,
        created_at=_now(),
    )
    await store.append_receipt(receipt)
    return receipt


def _retry_pipeline(
    store: SQLiteStorage, adapter: FakePresentationAdapter
) -> PipelineRunner:
    route = Route(
        id=_ROUTE_ID,
        source=RouteSource(
            adapter="fake_transport", event_kinds=("message.created",), channel=None
        ),
        targets=[RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)],
    )
    return PipelineRunner(
        make_pipeline_config_for_pipeline(
            store, Router(routes=[route]), {_ADAPTER: adapter}
        )
    )


def _observation(
    *,
    outbox_id: str,
    attempt_number: int,
    source: str,
    replay_run_id: str | None,
    channel: str = _CHANNEL,
) -> Any:
    return make_post_handoff_observation(
        event_id=_EVENT,
        adapter=_ADAPTER,
        state="delivered",
        outbox_id=outbox_id,
        delivery_plan_id=_PLAN_ID,
        attempt_number=attempt_number,
        native_channel_id=channel,
        native_message_id=f"{outbox_id}-a{attempt_number}",
        metadata={},
        source=source,  # type: ignore[arg-type]
        replay_run_id=replay_run_id,
    )


# ---------------------------------------------------------------------------
# Storage level: reservation preserves replay origin; live rows stay live
# ---------------------------------------------------------------------------


async def test_replay_origin_row_keeps_replay_authority_across_retry_reservation(
    temp_storage: SQLiteStorage,
) -> None:
    """A replay generation entering the retry lane keeps its replay authority.

    Full failure chain: exact-target replay admitted on the source hold ->
    transient refusal finalizes retry_wait at attempt 1 -> recovery claims and
    reserves attempt 2 -> the reservation must NOT rewrite the replay origin,
    so both the outbox-body read authority and the native-submission fence
    authorize the deliverable message on the still-live admission hold.
    """
    await _admit_event_with_hold(temp_storage)
    row = await _create_replay_generation(temp_storage)
    assert row.attempt_number == 1

    first_worker = "pipeline:replay"
    claimed = await _claim(temp_storage, row.outbox_id, first_worker)
    assert claimed.dispatch_source == "replay"
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )

    retry_worker = "retry:worker-1"
    reclaimed = await _claim(temp_storage, row.outbox_id, retry_worker)
    assert reclaimed.attempt_number == 1
    assert reclaimed.dispatch_source == "replay"

    reserved = await temp_storage.reserve_outbox_attempt(row.outbox_id, retry_worker, 1)
    assert reserved == 2

    reserved_row = await temp_storage.get_outbox_item(row.outbox_id)
    assert reserved_row is not None
    assert reserved_row.active_attempt == 2
    # The reservation must preserve the durable replay origin: the row's
    # content authority is the admission hold, and the authority check
    # recognizes replay generations only by this column.
    assert reserved_row.dispatch_source == "replay"

    # Read authority admits the deliverable body on the live admission hold.
    read = await temp_storage.read_event_for_outbox(_EVENT, row.outbox_id, now=_now())
    assert read.status == "retained"
    # The native-submission fence authorizes the reserved attempt.
    assert await temp_storage.authorize_native_submission(
        _EVENT,
        row.outbox_id,
        worker_id=retry_worker,
        attempt_number=reserved,
        now=_now(),
    )


async def test_preserved_replay_origin_still_fails_closed_once_hold_expires(
    temp_storage: SQLiteStorage,
) -> None:
    """Preserved replay origin never outlives its source admission hold."""
    await _admit_event_with_hold(temp_storage, hold_minutes=1)
    row = await _create_replay_generation(temp_storage)
    first_worker = "pipeline:replay"
    await _claim(temp_storage, row.outbox_id, first_worker)
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )

    retry_worker = "retry:worker-1"
    await _claim(temp_storage, row.outbox_id, retry_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, retry_worker, 1) == 2
    )

    # Past the hold deadline the replay generation fails closed: no hold, no
    # dispatch authority, whether the row still says replay (post-fix) or was
    # rewritten to retry (pre-fix) — the hold is the only replay authority.
    late = _now() + timedelta(minutes=5)
    read = await temp_storage.read_event_for_outbox(_EVENT, row.outbox_id, now=late)
    assert read.status == "denied"
    assert not await temp_storage.authorize_native_submission(
        _EVENT,
        row.outbox_id,
        worker_id=retry_worker,
        attempt_number=2,
        now=late,
    )


async def test_live_retry_lane_still_records_retry(
    temp_storage: SQLiteStorage,
) -> None:
    """Live-origin provenance semantics are unchanged by the preservation."""
    await _admit_event_with_hold(temp_storage)
    item = DeliveryOutboxItem(
        outbox_id="obox-live-retry",
        event_id=_EVENT,
        route_id=_ROUTE_ID,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=_CHANNEL,
        attempt_number=0,
        status="pending",
    )
    created = await temp_storage.create_outbox_item(item)

    first_worker = "pipeline:live"
    await _claim(temp_storage, created.outbox_id, first_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(created.outbox_id, first_worker, 0)
        == 1
    )
    first_row = await temp_storage.get_outbox_item(created.outbox_id)
    assert first_row is not None
    assert first_row.dispatch_source == "live"

    await _finalize_transient_failure(
        temp_storage, created.outbox_id, worker_id=first_worker, attempt_number=1
    )
    retry_worker = "retry:worker-1"
    await _claim(temp_storage, created.outbox_id, retry_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(created.outbox_id, retry_worker, 1)
        == 2
    )
    retry_row = await temp_storage.get_outbox_item(created.outbox_id)
    assert retry_row is not None
    assert retry_row.dispatch_source == "retry"


async def test_retry_lane_cannot_claim_replay_authority_it_never_had(
    temp_storage: SQLiteStorage,
) -> None:
    """A live-origin retry lane never rides the event's admission hold."""
    await _admit_event_with_hold(temp_storage)
    item = DeliveryOutboxItem(
        outbox_id="obox-live-no-ride",
        event_id=_EVENT,
        route_id=_ROUTE_ID,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=_CHANNEL,
        attempt_number=1,
        status="pending",
    )
    # The plain create adopts its own target-scoped content dependency.
    created = await temp_storage.create_outbox_item(item)
    await temp_storage._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE owner_kind='outbox' AND owner_id=?",
        (_now().isoformat(), created.outbox_id),
    )

    worker = "retry:worker-1"
    await _claim(temp_storage, created.outbox_id, worker)
    assert await temp_storage.reserve_outbox_attempt(created.outbox_id, worker, 1) == 2
    reserved_row = await temp_storage.get_outbox_item(created.outbox_id)
    assert reserved_row is not None
    assert reserved_row.dispatch_source == "retry"

    # The event's source admission hold is live, but this row is not a replay
    # generation: the fail-closed authority check must refuse it.
    assert not await temp_storage.authorize_native_submission(
        _EVENT,
        created.outbox_id,
        worker_id=worker,
        attempt_number=2,
        now=_now(),
    )


# ---------------------------------------------------------------------------
# Pipeline level: the recovery redispatch delivers instead of dead-lettering
# ---------------------------------------------------------------------------


async def test_replay_retry_redispatch_delivers_instead_of_dead_lettering(
    temp_storage: SQLiteStorage,
) -> None:
    """The retry worker's replay redispatch reaches the transport.

    Pre-fix chain: reserve rewrites the replay origin to ``retry``, the
    worker's outbox-body read is denied, and the deliverable message is
    abandoned with ``content_authority_denied`` before any transport call.
    """
    await _admit_event_with_hold(temp_storage)
    adapter = FakePresentationAdapter(adapter_id=_ADAPTER)
    row = await _create_replay_generation(temp_storage)

    first_worker = "pipeline:replay"
    await _claim(temp_storage, row.outbox_id, first_worker)
    await _append_replay_attempt_receipt(
        temp_storage,
        outbox_id=row.outbox_id,
        event_id=_EVENT,
        attempt_number=1,
    )
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )

    retry_worker = "retry:worker-1"
    claimed = await _claim(temp_storage, row.outbox_id, retry_worker)

    worker = RetryWorker(
        temp_storage,
        _retry_pipeline(temp_storage, adapter),
        None,
        enabled=True,
        max_attempts=3,
    )
    await worker._retry_outbox_item(claimed)

    final_row = await temp_storage.get_outbox_item(row.outbox_id)
    assert final_row is not None
    # The deliverable message must NOT be abandoned or dead-lettered.
    assert final_row.status in {"queued", "sent"}
    assert len(adapter.delivered_payloads) == 1
    assert adapter.delivered_payloads[0].event_id == _EVENT

    receipts = await temp_storage.list_receipts_for_outbox(row.outbox_id)
    by_attempt = {receipt.attempt_number: receipt for receipt in receipts}
    assert set(by_attempt) == {1, 2}
    assert by_attempt[1].source == "replay"
    assert by_attempt[2].source == "retry"
    assert by_attempt[2].replay_run_id == _RUN_ID
    preserved = await temp_storage.get_outbox_item(row.outbox_id)
    assert preserved is not None
    assert preserved.dispatch_source == "replay"


# ---------------------------------------------------------------------------
# Callback provenance fence: origin versus retry lane
# ---------------------------------------------------------------------------


async def test_replay_origin_row_admits_retry_lane_envelope(
    temp_storage: SQLiteStorage,
) -> None:
    """The retry lane's envelope is valid evidence over the preserved origin."""
    await _admit_event_with_hold(temp_storage)
    row = await _create_replay_generation(temp_storage)
    first_worker = "pipeline:replay"
    await _claim(temp_storage, row.outbox_id, first_worker)
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )

    retry_worker = "retry:worker-1"
    await _claim(temp_storage, row.outbox_id, retry_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, retry_worker, 1) == 2
    )

    lifecycle = DeliveryLifecycleService()
    assert await lifecycle.record_post_handoff_observation(
        temp_storage,
        _observation(
            outbox_id=row.outbox_id,
            attempt_number=2,
            source="retry",
            replay_run_id=_RUN_ID,
        ),
        _now(),
    )


async def test_fence_still_rejects_cross_lane_envelopes(
    temp_storage: SQLiteStorage,
) -> None:
    """Origin validation stays fail-closed in every other direction."""
    await _admit_event_with_hold(temp_storage)
    replay_row = await _create_replay_generation(
        temp_storage, outbox_id="obox-fence-replay"
    )
    first_worker = "pipeline:replay"
    await _claim(temp_storage, replay_row.outbox_id, first_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(replay_row.outbox_id, first_worker, 1)
        == 2
    )

    lifecycle = DeliveryLifecycleService()
    now = _now()
    # A live-lane envelope over a replay-origin row is not replay evidence.
    assert not await lifecycle.record_post_handoff_observation(
        temp_storage,
        _observation(
            outbox_id=replay_row.outbox_id,
            attempt_number=2,
            source="live",
            replay_run_id=None,
        ),
        now,
    )

    # A live-origin row reserved into its retry lane never admits a replay
    # envelope: the retry lane cannot claim replay provenance it never had.
    # A separate channel keeps this identity disjoint from the replay row.
    live_item = DeliveryOutboxItem(
        outbox_id="obox-fence-live",
        event_id=_EVENT,
        route_id=_ROUTE_ID,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel="ch-live",
        attempt_number=1,
        status="pending",
    )
    live_row = await temp_storage.create_outbox_item(live_item)
    live_worker = "retry:worker-2"
    await _claim(temp_storage, live_row.outbox_id, live_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(live_row.outbox_id, live_worker, 1)
        == 2
    )
    assert not await lifecycle.record_post_handoff_observation(
        temp_storage,
        _observation(
            outbox_id=live_row.outbox_id,
            attempt_number=2,
            source="replay",
            replay_run_id=_RUN_ID,
            channel="ch-live",
        ),
        now,
    )


# ---------------------------------------------------------------------------
# Radio checkpoint fence: durable origin versus attempt lane
# ---------------------------------------------------------------------------


def _wire() -> RadioTextPlan:
    """Use a real multibyte, prefixed plan with several ordered native packets."""
    return plan_radio_text("café α " * 16, max_text_bytes=40, prefix="[North] ")


def _radio_provenance(
    *,
    outbox_id: str,
    attempt_number: int,
    source: str,
    replay_run_id: str | None,
    channel: str = _CHANNEL,
) -> DeliveryAttemptProvenance:
    """Freeze the radio envelope exactly as the dispatch lane builds it."""
    return DeliveryAttemptProvenance(
        event_id=_EVENT,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=channel,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        source=source,  # type: ignore[arg-type]
        replay_run_id=replay_run_id,
    )


async def _radio_row(
    store: SQLiteStorage,
    *,
    outbox_id: str,
    worker_id: str,
    origin: str = "replay",
    replay_run_id: str | None = None,
    channel: str = _CHANNEL,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Admit one owned, leased in_progress row through production validation.

    Only admission-time origins exist here (``live`` or ``replay``); the
    ``retry`` row state is reached through ``reserve_outbox_attempt`` exactly
    as the retry worker reserves it. Distinct channels keep delivery
    identities disjoint so sibling-generation fences cannot interfere.
    """
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=_EVENT,
        route_id=_ROUTE_ID,
        delivery_plan_id=_PLAN_ID,
        target_adapter=_ADAPTER,
        target_channel=channel,
        attempt_number=attempt_number,
        status="in_progress",
        worker_id=worker_id,
        lease_until=(_now() + timedelta(minutes=30)).isoformat(),
        dispatch_source=origin,  # type: ignore[arg-type]
        replay_run_id=replay_run_id,
    )
    return await create_outbox_item_with_parent(
        store, item, allocate_new_generation=(origin == "replay")
    )


async def test_radio_replay_row_admits_replay_lane_checkpoint(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``replay`` row + ``replay`` lane: unchanged exact match."""
    await _admit_event_with_hold(temp_storage)
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-replay-replay",
        worker_id="pipeline:replay",
        replay_run_id=_RUN_ID,
    )
    assert row.active_attempt is None

    provenance = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=row.attempt_number,
        source="replay",
        replay_run_id=_RUN_ID,
    )
    bound = await temp_storage.bind_radio_attempt(
        provenance, "pipeline:replay", _wire()
    )
    assert bound is not None
    submitting = await temp_storage.begin_radio_part(bound, "pipeline:replay", 1)
    assert submitting is not None
    observed = await temp_storage.observe_radio_part(
        submitting, "pipeline:replay", 1, handoff_id="radio:north:1"
    )
    assert observed is not None


async def test_radio_replay_row_admits_retry_lane_checkpoint_on_active_reservation(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``replay`` row + ``retry`` lane: allow with matching reservation.

    Full chain: the replay generation fails once, the retry worker reserves
    attempt 2 on the preserved replay origin, and its retry-lane envelope
    binds, starts and observes radio checkpoints under that exact reservation.
    Without the fence extension the durable origin would reject every
    legitimate retry-lane radio checkpoint.
    """
    await _admit_event_with_hold(temp_storage)
    row = await _create_replay_generation(
        temp_storage, outbox_id="obox-radio-retry-lane"
    )
    first_worker = "pipeline:replay"
    await _claim(temp_storage, row.outbox_id, first_worker)
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )

    retry_worker = "retry:worker-1"
    await _claim(temp_storage, row.outbox_id, retry_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, retry_worker, 1) == 2
    )
    reserved_row = await temp_storage.get_outbox_item(row.outbox_id)
    assert reserved_row is not None
    assert reserved_row.dispatch_source == "replay"
    assert reserved_row.active_attempt == 2

    provenance = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id=_RUN_ID,
    )
    bound = await temp_storage.bind_radio_attempt(provenance, retry_worker, _wire())
    assert bound is not None
    submitting = await temp_storage.begin_radio_part(bound, retry_worker, 1)
    assert submitting is not None
    observed = await temp_storage.observe_radio_part(
        submitting, retry_worker, 1, handoff_id="radio:retry:1"
    )
    assert observed is not None


async def test_radio_retry_row_admits_retry_lane_checkpoint(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``retry`` row + ``retry`` lane: unchanged exact match."""
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-retry-retry",
        worker_id="retry:worker-1",
        origin="live",
        channel="ch-radio-retry-retry",
    )
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, "retry:worker-1", 1)
        == 2
    )
    reserved_row = await temp_storage.get_outbox_item(row.outbox_id)
    assert reserved_row is not None
    assert reserved_row.dispatch_source == "retry"
    assert reserved_row.active_attempt == 2

    provenance = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id=None,
        channel="ch-radio-retry-retry",
    )
    bound = await temp_storage.bind_radio_attempt(provenance, "retry:worker-1", _wire())
    assert bound is not None
    submitting = await temp_storage.begin_radio_part(bound, "retry:worker-1", 1)
    assert submitting is not None


async def test_radio_replay_row_refuses_live_lane_checkpoint(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``replay`` row + ``live`` lane: refuse, not a run-id artifact.

    The unnamed replay row and the live envelope both carry no replay run
    identifier, so the run ids match exactly and the refusal is attributable
    to the source labels alone. The replay-lane control binding on the same
    untouched row proves the state itself is bindable.
    """
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-replay-live",
        worker_id="pipeline:replay",
        replay_run_id=None,
        channel="ch-radio-replay-live",
    )
    live_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=1,
        source="live",
        replay_run_id=None,
        channel="ch-radio-replay-live",
    )
    assert (
        await temp_storage.bind_radio_attempt(live_lane, "pipeline:replay", _wire())
        is None
    )
    replay_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=1,
        source="replay",
        replay_run_id=None,
        channel="ch-radio-replay-live",
    )
    assert (
        await temp_storage.bind_radio_attempt(replay_lane, "pipeline:replay", _wire())
        is not None
    )


async def test_radio_retry_row_refuses_replay_lane_checkpoint(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``retry`` row + ``replay`` lane: refuse, not a run-id artifact.

    Both the reserved live-origin row and the unnamed replay envelope carry no
    replay run identifier, so the refusal is attributable to the source labels
    alone; the retry-lane control binding proves the reserved row is bindable.
    A named replay envelope over a second reserved row is refused as well.
    """
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-retry-replay",
        worker_id="retry:worker-1",
        origin="live",
        channel="ch-radio-retry-replay",
    )
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, "retry:worker-1", 1)
        == 2
    )
    replay_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="replay",
        replay_run_id=None,
        channel="ch-radio-retry-replay",
    )
    assert (
        await temp_storage.bind_radio_attempt(replay_lane, "retry:worker-1", _wire())
        is None
    )
    retry_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id=None,
        channel="ch-radio-retry-replay",
    )
    assert (
        await temp_storage.bind_radio_attempt(retry_lane, "retry:worker-1", _wire())
        is not None
    )

    named_row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-retry-replay-named",
        worker_id="retry:worker-1",
        origin="live",
        channel="ch-radio-retry-replay-named",
    )
    assert (
        await temp_storage.reserve_outbox_attempt(
            named_row.outbox_id, "retry:worker-1", 1
        )
        == 2
    )
    named_replay = _radio_provenance(
        outbox_id=named_row.outbox_id,
        attempt_number=2,
        source="replay",
        replay_run_id=_RUN_ID,
        channel="ch-radio-retry-replay-named",
    )
    assert (
        await temp_storage.bind_radio_attempt(named_replay, "retry:worker-1", _wire())
        is None
    )


async def test_radio_live_row_refuses_replay_lane_checkpoint(
    temp_storage: SQLiteStorage,
) -> None:
    """Table row ``live`` row + ``replay`` lane: refuse, not a run-id artifact.

    The live row and the unnamed replay envelope both carry no replay run
    identifier, so the refusal is attributable to the source labels alone;
    the live-lane control binding proves the row state is bindable.
    """
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-live-replay",
        worker_id="pipeline:sender-1",
        origin="live",
        channel="ch-radio-live-replay",
    )
    replay_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=1,
        source="replay",
        replay_run_id=None,
        channel="ch-radio-live-replay",
    )
    assert (
        await temp_storage.bind_radio_attempt(replay_lane, "pipeline:sender-1", _wire())
        is None
    )
    live_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=1,
        source="live",
        replay_run_id=None,
        channel="ch-radio-live-replay",
    )
    assert (
        await temp_storage.bind_radio_attempt(live_lane, "pipeline:sender-1", _wire())
        is not None
    )


async def test_radio_retry_lane_never_binds_unreserved_replay_base_attempt(
    temp_storage: SQLiteStorage,
) -> None:
    """A retry-lane envelope without the active reservation never binds.

    The replay row is in_progress with no reservation at all: the attempt
    guard in the claim query passes on the unreserved base attempt, so this
    refusal proves the fence's exact active-reservation requirement rather
    than the status or attempt guards. The replay-lane control binding on the
    same untouched row proves the state itself is bindable.
    """
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-unreserved-base",
        worker_id="pipeline:replay",
        replay_run_id=_RUN_ID,
        channel="ch-radio-unreserved-base",
    )
    assert row.active_attempt is None

    retry_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=row.attempt_number,
        source="retry",
        replay_run_id=_RUN_ID,
        channel="ch-radio-unreserved-base",
    )
    assert (
        await temp_storage.bind_radio_attempt(retry_lane, "pipeline:replay", _wire())
        is None
    )
    replay_lane = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=row.attempt_number,
        source="replay",
        replay_run_id=_RUN_ID,
        channel="ch-radio-unreserved-base",
    )
    assert (
        await temp_storage.bind_radio_attempt(replay_lane, "pipeline:replay", _wire())
        is not None
    )


async def test_radio_retry_lane_refuses_stale_and_future_attempts(
    temp_storage: SQLiteStorage,
) -> None:
    """Stale and future retry-lane attempts never bind a replay row.

    A future attempt number does not match the active reservation, and once
    the reservation is consumed into retry_wait the old attempt-2 envelope no
    longer binds an in_progress reservation at all.
    """
    await _admit_event_with_hold(temp_storage)
    row = await _create_replay_generation(temp_storage, outbox_id="obox-radio-stale")
    first_worker = "pipeline:replay"
    await _claim(temp_storage, row.outbox_id, first_worker)
    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=first_worker, attempt_number=1
    )
    retry_worker = "retry:worker-1"
    await _claim(temp_storage, row.outbox_id, retry_worker)
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, retry_worker, 1) == 2
    )

    future = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=3,
        source="retry",
        replay_run_id=_RUN_ID,
    )
    assert await temp_storage.bind_radio_attempt(future, retry_worker, _wire()) is None

    await _finalize_transient_failure(
        temp_storage, row.outbox_id, worker_id=retry_worker, attempt_number=2
    )
    stale = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id=_RUN_ID,
    )
    assert await temp_storage.bind_radio_attempt(stale, retry_worker, _wire()) is None


async def test_radio_retry_lane_on_replay_row_still_requires_matching_run(
    temp_storage: SQLiteStorage,
) -> None:
    """The divergence admits the reservation, not a foreign replay run.

    With the lane divergence held constant (replay row, retry lane, exact
    active reservation), flipping only the replay run identifier flips the
    verdict: the run binding is enforced independently of the source rule.
    """
    row = await _radio_row(
        temp_storage,
        outbox_id="obox-radio-run-mismatch",
        worker_id="retry:worker-1",
        replay_run_id=_RUN_ID,
        channel="ch-radio-run-mismatch",
    )
    assert (
        await temp_storage.reserve_outbox_attempt(row.outbox_id, "retry:worker-1", 1)
        == 2
    )
    foreign_run = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id="run-other-lineage",
        channel="ch-radio-run-mismatch",
    )
    assert (
        await temp_storage.bind_radio_attempt(foreign_run, "retry:worker-1", _wire())
        is None
    )
    matching_run = _radio_provenance(
        outbox_id=row.outbox_id,
        attempt_number=2,
        source="retry",
        replay_run_id=_RUN_ID,
        channel="ch-radio-run-mismatch",
    )
    assert (
        await temp_storage.bind_radio_attempt(matching_run, "retry:worker-1", _wire())
        is not None
    )
