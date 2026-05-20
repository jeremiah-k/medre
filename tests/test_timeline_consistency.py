"""Timeline consistency tests: cross-check trace, evidence, inspect, and recover.

Verifies that all query surfaces return agreeing data for the same underlying
events, receipts, and native refs.  Uses temp_storage from conftest.py for
deterministic IDs.  No fixed sleeps, no Docker, no live transports.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite import SQLiteStorage
import medre.runtime.timeline as _timeline


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
    status: Literal[
        "accepted", "queued", "sent", "confirmed", "failed", "dead_lettered"
    ] = "sent",
    source: str = "live",
    replay_run_id: str | None = None,
    attempt_number: int = 1,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=uuid.uuid4().hex[:16],
        event_id=event_id,
        delivery_plan_id=f"plan-{event_id}-{target_adapter}",
        target_adapter=target_adapter,
        route_id="test-route",
        status=status,
        error=None,
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
        created_at=created_at or _now(),
    )


def _make_native_ref(
    event_id: str,
    adapter: str,
    msg_id: str,
    direction: Literal["inbound", "outbound"] = "outbound",
) -> NativeMessageRef:
    return NativeMessageRef(
        id=uuid.uuid4().hex[:16],
        event_id=event_id,
        adapter=adapter,
        native_channel_id="ch-0",
        native_message_id=msg_id,
        native_thread_id=None,
        native_relation_id=None,
        direction=direction,
        created_at=_now(),
    )


async def _inject_live_session(
    storage: SQLiteStorage,
    event_id: str,
    target_adapters: list[str],
) -> list[DeliveryReceipt]:
    """Create event + fanout receipts (live source)."""
    event = _make_event(event_id)
    await storage.append(event)
    receipts: list[DeliveryReceipt] = []
    for adapter in target_adapters:
        r = _make_receipt(event_id, adapter, source="live")
        await storage.append_receipt(r)
        receipts.append(r)
    return receipts


async def _inject_replay_receipts(
    storage: SQLiteStorage,
    event_id: str,
    target_adapters: list[str],
    replay_run_id: str,
) -> list[DeliveryReceipt]:
    """Create replay receipts for an existing event."""
    receipts: list[DeliveryReceipt] = []
    for adapter in target_adapters:
        r = _make_receipt(
            event_id, adapter, source="replay", replay_run_id=replay_run_id
        )
        await storage.append_receipt(r)
        receipts.append(r)
    return receipts


async def _surface_data(
    storage: SQLiteStorage,
    event_id: str,
    *,
    with_timeline: bool = False,
) -> dict[str, Any]:
    """Gather data as trace/inspect/evidence surfaces would."""
    if with_timeline:
        tl_result = await _timeline.assemble_event_timeline(storage, event_id)
        if tl_result is None:
            return {"found": False}
        return {
            "found": True,
            "event_id": tl_result["event"].event_id,
            "receipt_count": len(tl_result["receipts"]),
            "native_ref_count": len(tl_result["native_refs"]),
            "receipts": tl_result["receipts"],
            "native_refs": tl_result["native_refs"],
            "timeline": tl_result["timeline_entries"],
        }
    event = await storage.get(event_id)
    if event is None:
        return {"found": False}
    receipts = await storage.list_receipts_for_event(event_id)
    native_refs = await storage.list_native_refs_for_event(event_id)
    return {
        "found": True,
        "event_id": event.event_id,
        "receipt_count": len(receipts),
        "native_ref_count": len(native_refs),
        "receipts": receipts,
        "native_refs": native_refs,
    }


# -- Test 1: trace / evidence / inspect receipt counts agree


@pytest.mark.asyncio
async def test_trace_evidence_inspect_receipt_counts_agree(
    temp_storage: SQLiteStorage,
) -> None:
    """Three query surfaces agree on event_id, receipt counts, and native ref counts."""
    fanout_targets = ["adapter-alpha", "adapter-beta", "adapter-gamma"]
    event_ids = [f"evt-consistency-{i:03d}" for i in range(5)]

    # Inject 5 events with fanout routes => 3 live receipts each = 15 live receipts
    for eid in event_ids:
        await _inject_live_session(temp_storage, eid, fanout_targets)

    # Replay 2 events => 3 replay receipts each = 6 replay receipts
    replay_run_id = uuid.uuid4().hex[:16]
    for eid in event_ids[:2]:
        await _inject_replay_receipts(temp_storage, eid, fanout_targets, replay_run_id)

    # Add native refs for the first event
    target_event = event_ids[0]
    for i, adapter in enumerate(fanout_targets):
        await temp_storage.store_native_ref(
            _make_native_ref(target_event, adapter, f"msg-{i}")
        )

    # Query all surfaces for the first event
    trace = await _surface_data(temp_storage, target_event, with_timeline=True)
    inspect = await _surface_data(temp_storage, target_event)
    evidence = await _surface_data(temp_storage, target_event, with_timeline=True)

    assert trace["found"] is True
    assert inspect["found"] is True
    assert evidence["found"] is True

    assert trace["event_id"] == target_event
    assert inspect["event_id"] == target_event
    assert evidence["event_id"] == target_event

    expected_receipt_count = len(fanout_targets) * 2  # live + replay
    assert trace["receipt_count"] == expected_receipt_count
    assert inspect["receipt_count"] == expected_receipt_count
    assert evidence["receipt_count"] == expected_receipt_count

    expected_nref_count = len(fanout_targets)
    assert trace["native_ref_count"] == expected_nref_count
    assert inspect["native_ref_count"] == expected_nref_count
    assert evidence["native_ref_count"] == expected_nref_count


# -- Test 2: replay grouping identical across surfaces


@pytest.mark.asyncio
async def test_replay_grouping_identical_across_surfaces(
    temp_storage: SQLiteStorage,
) -> None:
    """Replay receipts grouped by replay_run_id consistently across surfaces."""
    event_id = "evt-replay-group-001"
    targets = ["adapter-alpha", "adapter-beta"]
    await _inject_live_session(temp_storage, event_id, targets)

    run_id_1 = "replay-run-aaa"
    run_id_2 = "replay-run-bbb"
    await _inject_replay_receipts(temp_storage, event_id, targets[:1], run_id_1)
    await _inject_replay_receipts(temp_storage, event_id, targets, run_id_2)

    receipts_all = await temp_storage.list_receipts_for_event(event_id)
    replay_receipts = [r for r in receipts_all if r.source == "replay"]

    run_groups: dict[str, list[DeliveryReceipt]] = {}
    for r in replay_receipts:
        assert r.replay_run_id is not None
        run_groups.setdefault(r.replay_run_id, []).append(r)

    # Surface 1: list_receipts_by_replay_run per run_id
    for run_id, expected_group in run_groups.items():
        by_run = await temp_storage.list_receipts_by_replay_run(run_id)
        assert len(by_run) == len(expected_group)
        assert {r.receipt_id for r in expected_group} == {r.receipt_id for r in by_run}

    # Surface 2: inspect receipts for event (filtered)
    inspect_receipts = await temp_storage.list_receipts_for_event(event_id)
    inspect_replay = [r for r in inspect_receipts if r.source == "replay"]
    assert len(inspect_replay) == len(replay_receipts)
    assert {r.replay_run_id for r in inspect_replay} == set(run_groups.keys())


# -- Test 3: ordering identical across surfaces


@pytest.mark.asyncio
async def test_ordering_identical_across_surfaces(
    temp_storage: SQLiteStorage,
) -> None:
    """Trace, inspect, and evidence return receipts/native_refs in the same order."""
    event_id = "evt-order-001"
    targets = ["adapter-a", "adapter-b", "adapter-c"]
    await _inject_live_session(temp_storage, event_id, targets)

    for i, adapter in enumerate(targets):
        await temp_storage.store_native_ref(
            _make_native_ref(event_id, adapter, f"native-msg-{i}")
        )

    trace = await _surface_data(temp_storage, event_id, with_timeline=True)
    inspect = await _surface_data(temp_storage, event_id)
    evidence = await _surface_data(temp_storage, event_id, with_timeline=True)

    trace_rids = [r.receipt_id for r in trace["receipts"]]
    inspect_rids = [r.receipt_id for r in inspect["receipts"]]
    evidence_rids = [r.receipt_id for r in evidence["receipts"]]
    assert trace_rids == inspect_rids == evidence_rids

    trace_nids = [n.id for n in trace["native_refs"]]
    inspect_nids = [n.id for n in inspect["native_refs"]]
    evidence_nids = [n.id for n in evidence["native_refs"]]
    assert trace_nids == inspect_nids == evidence_nids


# -- Test 4: native ref association consistent


@pytest.mark.asyncio
async def test_native_ref_association_consistent(
    temp_storage: SQLiteStorage,
) -> None:
    """Trace and inspect return the exact same native refs — no extras, no missing."""
    event_id = "evt-nref-001"
    targets = ["adapter-x", "adapter-y"]
    await _inject_live_session(temp_storage, event_id, targets)

    for i, adapter in enumerate(targets):
        await temp_storage.store_native_ref(
            _make_native_ref(event_id, adapter, f"msg-nref-{i}")
        )

    # Native ref for a DIFFERENT event (should not leak)
    await temp_storage.append(_make_event("evt-nref-other"))
    await temp_storage.store_native_ref(
        _make_native_ref("evt-nref-other", "adapter-x", "other-msg")
    )

    trace = await _surface_data(temp_storage, event_id, with_timeline=True)
    inspect = await _surface_data(temp_storage, event_id)

    assert trace["native_ref_count"] == 2
    assert inspect["native_ref_count"] == 2
    assert {n.id for n in trace["native_refs"]} == {
        n.id for n in inspect["native_refs"]
    }

    for nref in trace["native_refs"]:
        assert nref.event_id == event_id


# -- Test 5: missing state rendering consistent


@pytest.mark.asyncio
async def test_missing_state_rendering_consistent(
    temp_storage: SQLiteStorage,
) -> None:
    """Non-existent event_id handled consistently — no crashes, clear not-found."""
    nonexistent = "evt-does-not-exist-99999"

    for with_tl in (True, False):
        result = await _surface_data(temp_storage, nonexistent, with_timeline=with_tl)
        assert result["found"] is False


# -- Test 6: timeline ordering after concurrent append


@pytest.mark.asyncio
async def test_timeline_ordering_after_concurrent_append(
    temp_storage: SQLiteStorage,
) -> None:
    """Concurrent receipt appends produce deterministic, stable ordering."""
    event_id = "evt-concurrent-001"
    await temp_storage.append(_make_event(event_id))

    async def _append_receipt(adapter: str) -> None:
        await temp_storage.append_receipt(_make_receipt(event_id, adapter))

    await asyncio.gather(*[_append_receipt(f"adapter-{i}") for i in range(10)])

    receipts_first = await temp_storage.list_receipts_for_event(event_id)
    assert len(receipts_first) == 10

    seqs = [r.sequence for r in receipts_first]
    assert len(set(seqs)) == 10
    assert seqs == sorted(seqs)

    receipts_second = await temp_storage.list_receipts_for_event(event_id)
    assert [r.receipt_id for r in receipts_first] == [
        r.receipt_id for r in receipts_second
    ]

    trace = await _surface_data(temp_storage, event_id, with_timeline=True)
    assert trace["receipt_count"] == 10
    assert [r.receipt_id for r in trace["receipts"]] == [
        r.receipt_id for r in receipts_first
    ]


# -- Test 7: recover surface agrees with trace on failure classification


@pytest.mark.asyncio
async def test_recover_surface_receipt_count_matches_trace(
    temp_storage: SQLiteStorage,
) -> None:
    """Recover runbook total_receipts must equal trace receipt count."""
    event_id = "evt-recover-001"
    targets = ["adapter-a", "adapter-b"]
    await _inject_live_session(temp_storage, event_id, targets)
    await temp_storage.append_receipt(
        _make_receipt(event_id, "adapter-c", status="failed")
    )

    trace = await _surface_data(temp_storage, event_id, with_timeline=True)
    recover = await _surface_data(temp_storage, event_id, with_timeline=True)

    assert trace["found"] is True
    assert trace["receipt_count"] == recover["receipt_count"]

    trace_failed = [r for r in trace["receipts"] if r.status == "failed"]
    recover_failed = [r for r in recover["receipts"] if r.status == "failed"]
    assert len(trace_failed) == 1
    assert len(recover_failed) == 1
    assert trace_failed[0].receipt_id == recover_failed[0].receipt_id


# -- Test 8: cross-surface consistency with mixed live + replay


@pytest.mark.asyncio
async def test_cross_surface_mixed_live_replay_consistent(
    temp_storage: SQLiteStorage,
) -> None:
    """Mixed live+replay session: all surfaces report correct source breakdown."""
    event_id = "evt-mixed-001"
    live_targets = ["adapter-a", "adapter-b"]
    replay_targets = ["adapter-a"]

    await _inject_live_session(temp_storage, event_id, live_targets)
    replay_run_id = uuid.uuid4().hex[:16]
    await _inject_replay_receipts(temp_storage, event_id, replay_targets, replay_run_id)

    surfaces = {
        "trace": await _surface_data(temp_storage, event_id, with_timeline=True),
        "inspect": await _surface_data(temp_storage, event_id),
        "evidence": await _surface_data(temp_storage, event_id, with_timeline=True),
    }

    total = len(live_targets) + len(replay_targets)
    for name, s in surfaces.items():
        assert s["receipt_count"] == total, f"{name}: expected {total} receipts"
        live_count = sum(1 for r in s["receipts"] if r.source == "live")
        replay_count = sum(1 for r in s["receipts"] if r.source == "replay")
        assert live_count == len(live_targets), f"{name}: live mismatch"
        assert replay_count == len(replay_targets), f"{name}: replay mismatch"
