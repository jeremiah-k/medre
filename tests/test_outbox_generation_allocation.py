"""Atomic replay generation allocation across retry/replay writer races."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.events.canonical import CanonicalEvent, EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem

_PLAN_ID = "plan-generation-allocation"
_WORKER = "retry-worker-generation"
_PAST = datetime.now(timezone.utc) - timedelta(seconds=120)
_FUTURE = datetime.now(timezone.utc) + timedelta(seconds=300)


async def _seed_event(storage, event_id: str) -> CanonicalEvent:
    event = CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="src",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "generation allocation test"},
        metadata=EventMetadata(),
    )
    await storage.append(event)
    return event


async def _seed_outbox(
    storage,
    *,
    outbox_id: str,
    event_id: str,
    target_channel: str | None,
) -> DeliveryOutboxItem:
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-generation-allocation",
        delivery_plan_id=_PLAN_ID,
        target_adapter="lxmf-main",
        target_channel=target_channel,
        attempt_number=1,
        status="in_progress",
        locked_at=_PAST.isoformat(),
        lease_until=_PAST.isoformat(),
        worker_id=_WORKER,
    )
    return await storage.create_outbox_item(item)


def _replay_candidate(
    *,
    outbox_id: str,
    event_id: str,
    target_channel: str | None,
    attempt_number: int = 1,
    worker_id: str = "replay-worker",
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-generation-allocation",
        delivery_plan_id=_PLAN_ID,
        target_adapter="lxmf-main",
        target_channel=target_channel,
        attempt_number=attempt_number,
        status="in_progress",
        worker_id=worker_id,
    )


@pytest.mark.parametrize("target_channel", [None, "aa" * 16])
async def test_reservation_rejects_generation_already_held_by_sibling(
    temp_storage, target_channel: str | None
) -> None:
    """A retry cannot reserve a generation already represented by replay."""
    event = await _seed_event(temp_storage, "evt-sibling-generation")
    original = await _seed_outbox(
        temp_storage,
        outbox_id="obox-sibling-original",
        event_id=event.event_id,
        target_channel=target_channel,
    )
    sibling = _replay_candidate(
        outbox_id="obox-sibling-replay",
        event_id=event.event_id,
        target_channel=target_channel,
        attempt_number=2,
    )
    await temp_storage.create_outbox_item(sibling)

    assert (
        await temp_storage.reserve_outbox_attempt(original.outbox_id, _WORKER, 1)
        is None
    )
    row = await temp_storage.get_outbox_item(original.outbox_id)
    assert row is not None
    assert row.active_attempt is None


@pytest.mark.parametrize("target_channel", [None, "aa" * 16])
async def test_idempotent_create_reuses_live_reservation_generation(
    temp_storage, target_channel: str | None
) -> None:
    """Generic idempotent create reuses a generation held by a live reservation."""
    event = await _seed_event(temp_storage, "evt-create-reservation-race")
    original = await _seed_outbox(
        temp_storage,
        outbox_id="obox-create-reservation-original",
        event_id=event.event_id,
        target_channel=target_channel,
    )
    assert (
        await temp_storage.reserve_outbox_attempt(original.outbox_id, _WORKER, 1) == 2
    )

    duplicate = _replay_candidate(
        outbox_id="obox-create-reservation-duplicate",
        event_id=event.event_id,
        target_channel=target_channel,
        attempt_number=2,
    )
    resolved = await temp_storage.create_outbox_item(duplicate)

    assert resolved.outbox_id == original.outbox_id
    rows = await temp_storage.list_outbox_items_for_event(event.event_id)
    assert [row.outbox_id for row in rows] == [original.outbox_id]
    assert await temp_storage.mark_outbox_retry_wait(
        original.outbox_id,
        next_attempt_at=_FUTURE.isoformat(),
        failure_kind="adapter_transient",
        attempt_number=2,
        expected_worker_id=_WORKER,
    )


@pytest.mark.parametrize("target_channel", [None, "aa" * 16])
async def test_atomic_replay_allocation_advances_past_live_reservation(
    temp_storage, target_channel: str | None
) -> None:
    """Replay allocation commits strictly above an in-flight retry reservation."""
    event = await _seed_event(temp_storage, "evt-atomic-replay-reserved")
    original = await _seed_outbox(
        temp_storage,
        outbox_id="obox-atomic-replay-reserved-original",
        event_id=event.event_id,
        target_channel=target_channel,
    )
    assert (
        await temp_storage.reserve_outbox_attempt(original.outbox_id, _WORKER, 1) == 2
    )

    replay = _replay_candidate(
        outbox_id="obox-atomic-replay-reserved-new",
        event_id=event.event_id,
        target_channel=target_channel,
    )
    created = await temp_storage.create_outbox_item(
        replay,
        allocate_new_generation=True,
    )

    assert created.outbox_id == replay.outbox_id
    assert created.attempt_number == 3
    still_reserved = await temp_storage.get_outbox_item(original.outbox_id)
    assert still_reserved is not None
    assert still_reserved.attempt_number == 1
    assert still_reserved.active_attempt == 2
    assert still_reserved.worker_id == _WORKER


async def test_concurrent_atomic_replay_allocations_get_distinct_generations(
    temp_storage,
) -> None:
    """Concurrent replay creators serialize into distinct durable generations."""
    event = await _seed_event(temp_storage, "evt-concurrent-atomic-replay")
    original = await _seed_outbox(
        temp_storage,
        outbox_id="obox-concurrent-atomic-replay-original",
        event_id=event.event_id,
        target_channel=None,
    )

    first, second = await asyncio.gather(
        temp_storage.create_outbox_item(
            _replay_candidate(
                outbox_id="obox-concurrent-atomic-replay-a",
                event_id=event.event_id,
                target_channel=None,
                worker_id="replay-worker-a",
            ),
            allocate_new_generation=True,
        ),
        temp_storage.create_outbox_item(
            _replay_candidate(
                outbox_id="obox-concurrent-atomic-replay-b",
                event_id=event.event_id,
                target_channel=None,
                worker_id="replay-worker-b",
            ),
            allocate_new_generation=True,
        ),
    )

    assert {first.attempt_number, second.attempt_number} == {2, 3}
    rows = await temp_storage.list_outbox_items_for_event(event.event_id)
    assert sorted(row.attempt_number for row in rows) == [1, 2, 3]
    assert (await temp_storage.get_outbox_item(original.outbox_id)) is not None


@pytest.mark.parametrize("target_channel", [None, "aa" * 16])
async def test_atomic_replay_allocation_does_not_reclaim_finalized_retry(
    temp_storage, target_channel: str | None
) -> None:
    """A stale replay candidate cannot reclaim a retry generation that already ran."""
    event = await _seed_event(temp_storage, "evt-atomic-replay-finalized")
    original = await _seed_outbox(
        temp_storage,
        outbox_id="obox-atomic-replay-finalized-original",
        event_id=event.event_id,
        target_channel=target_channel,
    )
    assert (
        await temp_storage.reserve_outbox_attempt(original.outbox_id, _WORKER, 1) == 2
    )
    assert await temp_storage.mark_outbox_retry_wait(
        original.outbox_id,
        next_attempt_at=_FUTURE.isoformat(),
        failure_kind="adapter_transient",
        attempt_number=2,
        expected_worker_id=_WORKER,
    )
    before = await temp_storage.get_outbox_item(original.outbox_id)
    assert before is not None
    assert before.status == "retry_wait"
    assert before.attempt_number == 2
    assert before.next_attempt_at == _FUTURE.isoformat()

    # This object deliberately carries the stale generation a replay could
    # have computed before retry finalization. Atomic storage allocation must
    # ignore it and choose generation 3 without reclaiming generation 2.
    replay = _replay_candidate(
        outbox_id="obox-atomic-replay-finalized-new",
        event_id=event.event_id,
        target_channel=target_channel,
        attempt_number=2,
    )
    created = await temp_storage.create_outbox_item(
        replay,
        allocate_new_generation=True,
    )

    assert created.outbox_id == replay.outbox_id
    assert created.attempt_number == 3
    unchanged = await temp_storage.get_outbox_item(original.outbox_id)
    assert unchanged is not None
    assert unchanged.status == "retry_wait"
    assert unchanged.attempt_number == 2
    assert unchanged.next_attempt_at == _FUTURE.isoformat()
    assert unchanged.worker_id is None
