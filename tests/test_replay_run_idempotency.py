"""Durable replay-run idempotency and provenance regressions.

A non-empty replay run ID is execution provenance, not DeliveryIdentity.  It
claims one outbox generation for one logical target atomically so concurrent
executions of the same replay cannot both dispatch, while distinct/empty run
IDs remain intentionally repeatable.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import DeliveryReceipt
from medre.core.evidence.retry_outbox import build_retry_outbox_summary
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.routing import Route, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.delivery_callbacks import make_terminal_record
from tests.helpers.pipeline import make_event
from tests.helpers.storage_outbox import admit_event


def _outbox(
    *,
    outbox_id: str,
    event_id: str = "evt-replay-claim",
    run_id: str | None = "run-1",
    worker_id: str = "worker-a",
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-replay-claim",
        delivery_plan_id="plan-replay-claim",
        target_adapter="dest",
        target_channel="room",
        attempt_number=1,
        status="in_progress",
        worker_id=worker_id,
        replay_run_id=run_id,
    )


async def test_same_nonempty_replay_run_claim_is_atomic(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-replay-claim")
    left = _outbox(outbox_id="obox-left", worker_id="worker-left")
    right = _outbox(outbox_id="obox-right", worker_id="worker-right")

    first, second = await asyncio.gather(
        temp_storage.create_outbox_item(left, allocate_new_generation=True),
        temp_storage.create_outbox_item(right, allocate_new_generation=True),
    )

    assert first.outbox_id == second.outbox_id
    assert first.replay_run_id == second.replay_run_id == "run-1"
    history = await temp_storage.list_outbox_items_for_delivery(
        DeliveryIdentity("evt-replay-claim", "plan-replay-claim", "dest", "room")
    )
    assert len(history) == 1
    assert history[0].attempt_number == 1


async def test_terminal_named_run_reexecution_reuses_existing_generation(
    temp_storage: SQLiteStorage,
) -> None:
    """Re-executing a named run whose claim is already terminal returns
    that generation unchanged instead of creating a sibling dispatch."""
    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-terminal", worker_id="worker-a"),
        allocate_new_generation=True,
    )
    committed = await temp_storage.mark_outbox_sent(
        claimed.outbox_id, receipt_id="rcpt-run-1"
    )
    assert committed is True

    reexecuted = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-reexec", worker_id="worker-b"),
        allocate_new_generation=True,
    )

    assert reexecuted.outbox_id == claimed.outbox_id
    assert reexecuted.status == "sent"
    history = await temp_storage.list_outbox_items_for_delivery(
        DeliveryIdentity("evt-replay-claim", "plan-replay-claim", "dest", "room")
    )
    assert len(history) == 1
    assert history[0].attempt_number == 1


async def test_distinct_and_empty_replay_runs_remain_repeatable(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-replay-claim")
    rows = []
    for index, run_id in enumerate(("run-a", "run-b", None, None), start=1):
        rows.append(
            await temp_storage.create_outbox_item(
                _outbox(
                    outbox_id=f"obox-{index}",
                    run_id=run_id,
                    worker_id=f"worker-{index}",
                ),
                allocate_new_generation=True,
            )
        )

    assert [row.attempt_number for row in rows] == [1, 2, 3, 4]
    assert len({row.outbox_id for row in rows}) == 4
    assert [row.replay_run_id for row in rows] == ["run-a", "run-b", None, None]


async def test_replay_run_count_includes_admitted_claim_before_first_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-count-claim", run_id="run-count"),
        allocate_new_generation=True,
    )

    assert claimed.replay_run_id == "run-count"
    assert await temp_storage.count_replay_runs() == 1

    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-count-claim",
            event_id=claimed.event_id,
            delivery_plan_id=claimed.delivery_plan_id,
            target_adapter=claimed.target_adapter,
            target_channel=claimed.target_channel,
            route_id=claimed.route_id,
            status="sent",
            attempt_number=claimed.attempt_number,
            outbox_id=claimed.outbox_id,
            source="replay",
            replay_run_id="run-count",
        )
    )
    assert await temp_storage.count_replay_runs() == 1


async def test_atomic_replay_claim_does_not_depend_on_unique_index(
    temp_storage: SQLiteStorage,
) -> None:
    """BEGIN IMMEDIATE is the concurrency primitive; the index is defense in depth."""
    await admit_event(temp_storage, "evt-replay-claim")
    peer = SQLiteStorage(temp_storage._db_path)
    await peer.initialize()
    try:
        await temp_storage._write(
            "DROP INDEX IF EXISTS idx_outbox_replay_run_identity_unique"
        )
        left = _outbox(outbox_id="obox-no-index-left", worker_id="worker-left")
        right = _outbox(outbox_id="obox-no-index-right", worker_id="worker-right")

        first, second = await asyncio.gather(
            temp_storage.create_outbox_item(left, allocate_new_generation=True),
            peer.create_outbox_item(right, allocate_new_generation=True),
        )

        assert first.outbox_id == second.outbox_id
        history = await temp_storage.list_outbox_items_for_delivery(
            DeliveryIdentity("evt-replay-claim", "plan-replay-claim", "dest", "room")
        )
        assert len(history) == 1
    finally:
        await peer.close()


async def test_named_replay_claim_with_missing_lease_is_reclaimable(
    temp_storage: SQLiteStorage,
) -> None:
    """Recovery classification and SQL claiming agree on missing leases."""
    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(
            outbox_id="obox-missing-lease",
            run_id="run-missing-lease",
            worker_id="crashed-replay",
        ),
        allocate_new_generation=True,
    )
    assert claimed.lease_until is None

    recovered = await temp_storage.claim_due_outbox_items(
        now="2026-09-24T12:00:00+00:00",
        worker_id="retry-recovery",
        lease_seconds=30,
        limit=10,
    )

    assert [item.outbox_id for item in recovered] == [claimed.outbox_id]
    assert recovered[0].worker_id == "retry-recovery"
    assert recovered[0].replay_run_id == "run-missing-lease"
    assert recovered[0].status == "in_progress"


async def test_named_replay_provenance_requires_replay_allocator(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-replay-claim")
    with pytest.raises(ValueError, match="allocate_new_generation=True"):
        await temp_storage.create_outbox_item(
            _outbox(outbox_id="obox-invalid-mode", run_id="run-invalid")
        )


async def test_outbox_manager_reports_atomic_same_run_duplicate(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(event_id="evt-manager-replay-claim", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-manager-replay-claim",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-manager-replay-claim",
        event_id=event.event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())

    first = await manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id="run-manager",
    )
    duplicate = await manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id="run-manager",
    )

    assert first.replay_duplicate is False
    assert first.skip_reason is None
    assert duplicate.replay_duplicate is True
    assert duplicate.outbox_id == first.outbox_id
    assert duplicate.skip_reason == "replay_run_claimed:in_progress"


async def test_replay_terminal_callback_before_queued_receipt_preserves_origin(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(event_id="evt-replay-terminal-race", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-replay-terminal-race",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-terminal-race",
        event_id=event.event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    claim = await manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id="run-terminal-race",
    )

    await manager.record_terminal(
        make_terminal_record(
            event_id=event.event_id,
            adapter="dest",
            outcome="permanent_failed",
            outbox_id=claim.outbox_id,
            delivery_plan_id=plan.plan_id,
            attempt_number=claim.attempt_number,
            native_channel_id="room",
            error="adapter rejected queued send",
            source="replay",
            replay_run_id="run-terminal-race",
        )
    )

    receipts = await temp_storage.list_receipts_for_delivery(
        DeliveryIdentity(event.event_id, plan.plan_id, "dest", "room")
    )
    assert [receipt.status for receipt in receipts] == ["failed", "dead_lettered"]
    assert all(receipt.source == "replay" for receipt in receipts)
    assert all(receipt.replay_run_id == "run-terminal-race" for receipt in receipts)


async def test_unnamed_replay_terminal_callback_before_queued_receipt_preserves_source(
    temp_storage: SQLiteStorage,
) -> None:
    """Unnamed replay source survives the admission-to-queued-receipt race."""
    event = make_event(
        event_id="evt-unnamed-replay-terminal-race", source_adapter="src"
    )
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-unnamed-replay-terminal-race",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-unnamed-replay-terminal-race",
        event_id=event.event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    claim = await manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id=None,
    )

    row = await temp_storage.get_outbox_item(claim.outbox_id)
    assert row is not None
    assert row.dispatch_source == "replay"
    assert row.replay_run_id is None

    await manager.record_terminal(
        make_terminal_record(
            event_id=event.event_id,
            adapter="dest",
            outcome="permanent_failed",
            outbox_id=claim.outbox_id,
            delivery_plan_id=plan.plan_id,
            attempt_number=claim.attempt_number,
            native_channel_id="room",
            error="adapter rejected unnamed replay send",
            source="replay",
        )
    )

    receipts = await temp_storage.list_receipts_for_delivery(
        DeliveryIdentity(event.event_id, plan.plan_id, "dest", "room")
    )
    assert [receipt.status for receipt in receipts] == ["failed", "dead_lettered"]
    assert all(receipt.source == "replay" for receipt in receipts)
    assert all(receipt.replay_run_id is None for receipt in receipts)


async def test_finalized_unnamed_replay_without_queued_receipt_preserves_source(
    temp_storage: SQLiteStorage,
) -> None:
    event = make_event(event_id="evt-replay-missing-queued", source_adapter="src")
    await temp_storage.append(event)
    target = RouteTarget(adapter="dest", channel="room")
    route = Route(
        id="route-replay-missing-queued",
        source=RouteSource(
            adapter="src", event_kinds=("message.created",), channel=None
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-missing-queued",
        event_id=event.event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    claim = await manager.create_for_delivery(
        event,
        route,
        plan,
        target,
        "dest",
        source="replay",
        replay_run_id=None,
    )
    assert await temp_storage.mark_outbox_queued(
        claim.outbox_id, attempt_number=claim.attempt_number
    )
    queued_row = await temp_storage.get_outbox_item(claim.outbox_id)
    assert queued_row is not None
    assert queued_row.dispatch_source == "replay"
    assert queued_row.replay_run_id is None

    await manager.record_terminal(
        make_terminal_record(
            event_id=event.event_id,
            adapter="dest",
            outcome="permanent_failed",
            outbox_id=claim.outbox_id,
            delivery_plan_id=plan.plan_id,
            attempt_number=claim.attempt_number,
            native_channel_id="room",
            error="late queue failure",
            source="replay",
        )
    )

    row = await temp_storage.get_outbox_item(claim.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    receipts = await temp_storage.list_receipts_for_event(event.event_id)
    assert [receipt.status for receipt in receipts] == ["failed", "dead_lettered"]
    assert all(receipt.source == "replay" for receipt in receipts)
    assert all(receipt.replay_run_id is None for receipt in receipts)


async def test_retry_preserves_originating_replay_run_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medre.runtime.retry as retry_module

    item = _outbox(
        outbox_id="obox-replay-retry",
        event_id="evt-replay-retry",
        run_id="run-replay-retry",
        worker_id="retry-worker-test",
    )
    item = replace(item, status="in_progress", attempt_number=1)
    sent = DeliveryReceipt(
        receipt_id="rcpt-replay-retry-sent",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status="sent",
        attempt_number=2,
        outbox_id=item.outbox_id,
        source="retry",
        replay_run_id=item.replay_run_id,
    )
    storage = MagicMock()
    storage.get = AsyncMock(return_value=object())
    storage.delivery_status = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.deliver_to_target = AsyncMock(return_value=sent)
    lifecycle = MagicMock()
    lifecycle.reserve_retry_attempt = AsyncMock(return_value=2)
    lifecycle.renew_retry_lease = AsyncMock(return_value=True)
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.finalize_retry_success = AsyncMock(return_value=True)
    monkeypatch.setattr(
        retry_module,
        "reconstruct_retry_delivery_plan",
        lambda **_: SimpleNamespace(
            route=MagicMock(),
            plan=MagicMock(),
            retry_policy=RetryPolicy(max_attempts=3),
        ),
    )
    worker = RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=True,
        lifecycle=lifecycle,
    )

    await worker._retry_outbox_item(item)

    assert pipeline.deliver_to_target.await_args.kwargs["source"] == "retry"
    assert (
        pipeline.deliver_to_target.await_args.kwargs["replay_run_id"]
        == "run-replay-retry"
    )


def test_retry_outbox_evidence_exposes_durable_replay_origin() -> None:
    summary = build_retry_outbox_summary(
        outbox_items=[
            _outbox(
                outbox_id="obox-evidence-origin",
                event_id="evt-evidence-origin",
                run_id="run-evidence-origin",
            )
        ]
    )
    assert len(summary.items) == 1
    assert summary.items[0].replay_run_id == "run-evidence-origin"


async def test_replay_run_claim_index_is_present_and_schema_revision_stays_one(
    temp_storage: SQLiteStorage,
) -> None:
    columns = await temp_storage._read_all("PRAGMA table_info(delivery_outbox)")
    column_names = {row["name"] for row in columns}
    assert "dispatch_source" in column_names
    assert "replay_run_id" in column_names
    indexes = await temp_storage._read_all("PRAGMA index_list(delivery_outbox)")
    names = {row["name"] for row in indexes}
    assert "idx_outbox_replay_run_identity_unique" in names
    index_row = await temp_storage._read_one(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_outbox_replay_run_identity_unique",),
    )
    assert index_row is not None
    assert "UNIQUE INDEX" in index_row["sql"]
    assert "replay_run_id" in index_row["sql"]
    schema_row = await temp_storage._read_one(
        "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
    )
    assert schema_row is not None
    assert schema_row["value"] == "1"


def test_replay_request_normalizes_operator_run_id() -> None:
    named = ReplayRequest(mode=ReplayMode.BEST_EFFORT, run_id="  named-run  ")
    unnamed = ReplayRequest(mode=ReplayMode.BEST_EFFORT, run_id="   ")
    assert named.run_id == "named-run"
    assert unnamed.run_id == ""


def test_outbox_item_normalizes_replay_run_id() -> None:
    item = _outbox(outbox_id="obox-normalized-run", run_id="  run-normalized  ")
    unnamed = _outbox(outbox_id="obox-unnamed-run", run_id="   ")
    assert item.replay_run_id == "run-normalized"
    assert unnamed.replay_run_id is None


async def test_replay_run_receipt_lookup_normalizes_named_run(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-run-lookup")
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-run-lookup",
            event_id="evt-run-lookup",
            delivery_plan_id="plan-run-lookup",
            target_adapter="dest",
            status="sent",
            source="replay",
            replay_run_id="run-lookup",
        )
    )

    rows = await temp_storage.list_receipts_by_replay_run("  run-lookup  ")
    assert [row.receipt_id for row in rows] == ["rcpt-run-lookup"]
    with pytest.raises(ValueError, match="non-empty run ID"):
        await temp_storage.list_receipts_by_replay_run("   ")


async def test_named_replay_claim_is_queryable_before_first_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-query-claim", run_id="run-query"),
        allocate_new_generation=True,
    )

    rows = await temp_storage.list_outbox_items_by_replay_run("  run-query  ")
    assert [row.outbox_id for row in rows] == [claimed.outbox_id]
    assert rows[0].replay_run_id == "run-query"
    with pytest.raises(ValueError, match="non-empty run ID"):
        await temp_storage.list_outbox_items_by_replay_run("   ")


async def test_replay_timeline_exposes_admission_before_first_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    from medre.runtime.timeline import assemble_replay_timeline

    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-timeline-claim", run_id="run-timeline"),
        allocate_new_generation=True,
    )

    result = await assemble_replay_timeline(temp_storage, "run-timeline")

    assert result is not None
    assert result["outbox_items"][0].outbox_id == claimed.outbox_id
    assert result["receipts"] == []
    assert result["origin"] == "replay"
    assert result["sources_seen"] == []
    replay = result["timeline_entries"]
    assert replay["status"] == "admitted"
    assert replay["outbox_count"] == 1
    assert replay["receipt_count"] == 0
    assert replay["event_ids"] == ["evt-replay-claim"]
    assert any(
        entry["entry_type"] == "outbox_generation"
        and entry["data"]["outbox_id"] == claimed.outbox_id
        for entry in replay["timeline"]
    )


async def test_replay_timeline_preserves_retry_mechanism_and_replay_origin(
    temp_storage: SQLiteStorage,
) -> None:
    from medre.runtime.timeline import assemble_replay_timeline

    await admit_event(temp_storage, "evt-replay-claim")
    claimed = await temp_storage.create_outbox_item(
        _outbox(outbox_id="obox-timeline-retry", run_id="run-retry-origin"),
        allocate_new_generation=True,
    )
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="rcpt-timeline-retry",
            event_id=claimed.event_id,
            delivery_plan_id=claimed.delivery_plan_id,
            target_adapter=claimed.target_adapter,
            target_channel=claimed.target_channel,
            route_id=claimed.route_id,
            status="failed",
            error="transient",
            failure_kind="transient",
            attempt_number=claimed.attempt_number,
            outbox_id=claimed.outbox_id,
            source="retry",
            replay_run_id=claimed.replay_run_id,
        )
    )

    result = await assemble_replay_timeline(temp_storage, "run-retry-origin")

    assert result is not None
    assert result["origin"] == "replay"
    assert result["sources_seen"] == ["retry"]
    replay = result["timeline_entries"]
    assert replay["sources_seen"] == ["retry"]
    receipt_entries = [
        entry for entry in replay["timeline"] if entry["entry_type"] == "receipt"
    ]
    assert receipt_entries[0]["data"]["source"] == "retry"
    assert receipt_entries[0]["data"]["replay_run_id"] == "run-retry-origin"


async def test_replay_timeline_is_active_when_any_admitted_target_remains_nonterminal(
    temp_storage: SQLiteStorage,
) -> None:
    from medre.runtime.timeline import assemble_replay_timeline

    await admit_event(temp_storage, "evt-replay-active")
    sent = await temp_storage.create_outbox_item(
        replace(
            _outbox(
                outbox_id="obox-replay-active-sent",
                run_id="run-active",
            ),
            event_id="evt-replay-active",
            target_channel="room-sent",
        ),
        allocate_new_generation=True,
    )
    pending = await temp_storage.create_outbox_item(
        replace(
            _outbox(
                outbox_id="obox-replay-active-pending",
                run_id="run-active",
            ),
            event_id="evt-replay-active",
            target_channel="room-pending",
        ),
        allocate_new_generation=True,
    )
    receipt = DeliveryReceipt(
        receipt_id="rcpt-replay-active-sent",
        event_id=sent.event_id,
        delivery_plan_id=sent.delivery_plan_id,
        target_adapter=sent.target_adapter,
        target_channel=sent.target_channel,
        route_id=sent.route_id,
        status="sent",
        attempt_number=sent.attempt_number,
        outbox_id=sent.outbox_id,
        source="replay",
        replay_run_id=sent.replay_run_id,
    )
    await temp_storage.append_receipt(receipt)
    assert await temp_storage.mark_outbox_sent(
        sent.outbox_id,
        receipt_id=receipt.receipt_id,
        attempt_number=sent.attempt_number,
        expected_worker_id=sent.worker_id,
    )

    result = await assemble_replay_timeline(temp_storage, "run-active")

    assert result is not None
    assert result["timeline_entries"]["status"] == "active"
    assert {item.outbox_id for item in result["outbox_items"]} == {
        sent.outbox_id,
        pending.outbox_id,
    }
