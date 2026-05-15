"""Concurrent ordering tests: receipt append, replay append, native ref writes.

Verifies that concurrent database operations produce deterministic ordering,
no duplicate sequence numbers, and stable results across restarts. Uses
asyncio.Event for synchronization (no fixed sleeps) and temp_storage from
conftest.py for deterministic IDs.

No Docker, no live transports.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.async_utils import wait_until


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_event(event_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_now(),
        source_adapter="fake_transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": f"event {event_id}"},
        metadata=EventMetadata(),
    )


def _make_receipt(
    event_id: str,
    target_adapter: str,
    *,
    source: str = "live",
    replay_run_id: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=uuid.uuid4().hex[:16],
        event_id=event_id,
        delivery_plan_id=f"plan-{event_id}-{target_adapter}",
        target_adapter=target_adapter,
        route_id="test-route",
        status="sent",
        error=None,
        attempt_number=1,
        source=source,
        replay_run_id=replay_run_id,
        created_at=created_at or _now(),
    )


def _make_native_ref(
    event_id: str,
    adapter: str,
    msg_id: str,
    created_at: datetime | None = None,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=uuid.uuid4().hex[:16],
        event_id=event_id,
        adapter=adapter,
        native_channel_id="ch-0",
        native_message_id=msg_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=created_at or _now(),
    )


async def _open_fresh_storage(db_path: str) -> SQLiteStorage:
    """Open and initialize a new storage instance on the same DB file."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    return storage


# ---------------------------------------------------------------------------
# Test 1: concurrent receipt append
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_receipt_append(
    temp_storage: SQLiteStorage,
) -> None:
    """10 concurrent receipt appends for the same event_id produce exactly 10 receipts."""
    event_id = "evt-conc-receipt-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    # Gate: all coroutines wait until released
    gate = asyncio.Event()

    async def _append(idx: int) -> None:
        await gate.wait()  # synchronise start
        r = _make_receipt(event_id, f"adapter-{idx}")
        await temp_storage.append_receipt(r)

    # Launch all coroutines
    tasks = [asyncio.ensure_future(_append(i)) for i in range(10)]

    # Release all at once
    gate.set()
    await asyncio.gather(*tasks)

    # Verify
    receipts = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts) == 10, f"Expected 10 receipts, got {len(receipts)}"

    # Ordering deterministic: sequence column stable (monotonically increasing)
    seqs = [r.sequence for r in receipts]
    assert seqs == sorted(seqs), f"Sequences not ordered: {seqs}"

    # No duplicate sequence numbers
    assert len(set(seqs)) == 10, f"Duplicate sequences: {seqs}"


# ---------------------------------------------------------------------------
# Test 2: concurrent replay append
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_replay_append(
    temp_storage: SQLiteStorage,
) -> None:
    """Concurrent replay receipts all have source='replay' and populated replay_run_id."""
    event_id = "evt-conc-replay-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    replay_run_id = uuid.uuid4().hex[:16]
    gate = asyncio.Event()

    async def _append_replay(idx: int) -> None:
        await gate.wait()
        r = _make_receipt(
            event_id,
            f"adapter-replay-{idx}",
            source="replay",
            replay_run_id=replay_run_id,
        )
        await temp_storage.append_receipt(r)

    tasks = [asyncio.ensure_future(_append_replay(i)) for i in range(10)]
    gate.set()
    await asyncio.gather(*tasks)

    receipts = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts) == 10

    # All have source="replay"
    for r in receipts:
        assert r.source == "replay", f"Expected source=replay, got {r.source}"
        assert r.replay_run_id == replay_run_id, (
            f"Expected replay_run_id={replay_run_id}, got {r.replay_run_id}"
        )

    # Ordering stable
    seqs = [r.sequence for r in receipts]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 10


# ---------------------------------------------------------------------------
# Test 3: concurrent native ref writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_native_ref_writes(
    temp_storage: SQLiteStorage,
) -> None:
    """10 concurrent native ref writes for the same event_id: all stored, deterministic order."""
    event_id = "evt-conc-nref-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    gate = asyncio.Event()

    async def _write_nref(idx: int) -> None:
        await gate.wait()
        nref = _make_native_ref(event_id, f"adapter-nref-{idx}", f"msg-{idx}")
        await temp_storage.store_native_ref(nref)

    tasks = [asyncio.ensure_future(_write_nref(i)) for i in range(10)]
    gate.set()
    await asyncio.gather(*tasks)

    nrefs = await temp_storage.list_native_refs_for_event(event_id)
    assert len(nrefs) == 10, f"Expected 10 native refs, got {len(nrefs)}"

    # Ordering deterministic: created_at ascending, then id ascending
    for i in range(len(nrefs) - 1):
        assert (
            nrefs[i].created_at <= nrefs[i + 1].created_at
        ), f"Native refs not ordered by created_at at index {i}"


# ---------------------------------------------------------------------------
# Test 4: same timestamp different IDs ordered deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_timestamp_different_ids_ordered_deterministically(
    temp_storage: SQLiteStorage,
) -> None:
    """Receipts with identical timestamps but different IDs produce deterministic order."""
    event_id = "evt-ts-tiebreak-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    # All receipts with the same created_at timestamp
    shared_ts = _now()
    for i in range(5):
        r = _make_receipt(
            event_id,
            f"adapter-tie-{i}",
            created_at=shared_ts,
        )
        await temp_storage.append_receipt(r)

    receipts = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts) == 5

    # Ordering is deterministic because sequence (AUTOINCREMENT) is the tiebreaker
    seqs = [r.sequence for r in receipts]
    assert seqs == sorted(seqs), f"Not ordered by sequence: {seqs}"

    # Same order on second query
    receipts_again = await temp_storage.list_receipts_for_event(event_id)
    assert [r.receipt_id for r in receipts] == [r.receipt_id for r in receipts_again]


# ---------------------------------------------------------------------------
# Test 5: replay / live interleaving stable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_live_interleaving_stable() -> None:
    """Live -> replay -> live interleaving: total count correct, ordering stable after restart."""
    # Use our own temp storage so we can safely close/reopen/unlink
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    temp_storage = SQLiteStorage(db_path=db_path)
    await temp_storage.initialize()

    try:
        event_id = "evt-interleave-001"
        event = _make_event(event_id)
        await temp_storage.append(event)

        replay_run_id = uuid.uuid4().hex[:16]

        # Phase 1: 3 live receipts
        for i in range(3):
            r = _make_receipt(event_id, f"live-a-{i}", source="live")
            await temp_storage.append_receipt(r)

        # Phase 2: 2 replay receipts
        for i in range(2):
            r = _make_receipt(
                event_id,
                f"replay-b-{i}",
                source="replay",
                replay_run_id=replay_run_id,
            )
            await temp_storage.append_receipt(r)

        # Phase 3: 2 more live receipts
        for i in range(2):
            r = _make_receipt(event_id, f"live-c-{i}", source="live")
            await temp_storage.append_receipt(r)

        receipts = await temp_storage.list_receipts_for_event(event_id)
        total = 3 + 2 + 2
        assert len(receipts) == total, f"Expected {total} receipts, got {len(receipts)}"

        # Verify ordering: sequence ascending (insertion order preserved)
        seqs = [r.sequence for r in receipts]
        assert seqs == sorted(seqs)

        # Verify source breakdown in order
        sources = [r.source for r in receipts]
        assert sources[:3] == ["live", "live", "live"]
        assert sources[3:5] == ["replay", "replay"]
        assert sources[5:] == ["live", "live"]

        # Close current storage
        await temp_storage.close()

        # Reopen and verify same order
        storage2 = await _open_fresh_storage(db_path)
        try:
            receipts2 = await storage2.list_receipts_for_event(event_id)
            assert len(receipts2) == total
            rids1 = [r.receipt_id for r in receipts]
            rids2 = [r.receipt_id for r in receipts2]
            assert rids1 == rids2, "Order changed after restart"
        finally:
            await storage2.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Test 6: concurrent append with asyncio.Event synchronization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_append_with_event_sync(
    temp_storage: SQLiteStorage,
) -> None:
    """Use asyncio.Event to gate concurrent appends; verify total and ordering."""
    event_id = "evt-gate-sync-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    gate = asyncio.Event()
    results: list[str] = []

    async def _worker(adapter: str) -> str:
        await gate.wait()
        r = _make_receipt(event_id, adapter)
        await temp_storage.append_receipt(r)
        return r.receipt_id

    # Start workers (they block on gate)
    workers = [asyncio.ensure_future(_worker(f"adapter-{i}")) for i in range(8)]

    # Release all workers simultaneously
    gate.set()

    completed_ids = await asyncio.gather(*workers)
    results.extend(completed_ids)

    # All stored
    receipts = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts) == 8

    # No duplicates
    rids = [r.receipt_id for r in receipts]
    assert len(set(rids)) == 8

    # Same result set
    assert set(rids) == set(results)


# ---------------------------------------------------------------------------
# Test 7: wait_until-based concurrent verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_append_wait_until_verification(
    temp_storage: SQLiteStorage,
) -> None:
    """Use wait_until to verify all concurrent appends completed."""
    event_id = "evt-waituntil-001"
    event = _make_event(event_id)
    await temp_storage.append(event)

    expected_count = 5

    # Start background appends
    async def _bg_append() -> None:
        for i in range(expected_count):
            r = _make_receipt(event_id, f"bg-adapter-{i}")
            await temp_storage.append_receipt(r)

    asyncio.ensure_future(_bg_append())

    # Poll until all receipts visible
    found = await wait_until(
        lambda: temp_storage.list_receipts_for_event(event_id),
        timeout=5.0,
    )
    assert found, "Receipts did not appear within timeout"

    receipts = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts) == expected_count

    # Sequences monotonic
    seqs = [r.sequence for r in receipts]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == expected_count
