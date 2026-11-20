# medre-test-tier: compatibility
"""Explicit-replay refusal reconciliation after generation allocation.

An explicit replay can decline before generation allocation (pre-allocation
health admission, exact-target refusal, preflight suppression). A refusal
that arrives *after* the coordinator allocated the replay generation is
different: the generation exists with no attempt evidence and no releasable
reservation, so without an explicit reconciliation transition it would sit
``in_progress`` until its lease expired and the recovery claim would
auto-redispatch the refused replay as a retry generation.

These tests pin the deterministic reconciliation contract:

* a post-allocation health refusal reconciles the allocated replay
  generation to terminal ``cancelled`` with a ``cancelled`` lifecycle
  receipt; the adapter is never invoked and no failure evidence exists;
* the reconciled generation is never claimable by recovery, and a
  subsequent replay of the same obligation allocates the next generation
  and delivers;
* the live-source health deferral keeps its held-obligation semantics
  (retry_wait with a pushed next attempt) — reconciliation is replay-only;
* content-authority refusals after allocation already finalize the replay
  generation to a terminal, unclaimable state without any attempt evidence.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.adapter_health import AdapterHealthGate
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import (
    REPLAY_REFUSAL_RECONCILED_DETAIL,
    OutboxContext,
    OutboxManager,
)
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, TerminalOutboxFinalization
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _counter_health_gate(healthy_until: int) -> AdapterHealthGate:
    """Gate that reports no blocking health for the first observations.

    The delivery coordinator consults admission once before allocating the
    replay generation; post-allocation health admission consults it again
    immediately before rendering. Flipping the observation between the two
    calls reproduces the health degradation that happens mid-flight.
    """

    calls = {"count": 0}

    def _health(adapter_id: str) -> str | None:
        calls["count"] += 1
        if calls["count"] <= healthy_until:
            return None
        return "degraded"

    return AdapterHealthGate(_health)


def _route_and_plan(
    event_id: str,
    *,
    channel: str | None = "room",
) -> tuple[Route, DeliveryPlan]:
    target = RouteTarget(adapter="dest", channel=channel)
    route = Route(
        id="route-replay-refusal",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-replay-refusal",
        event_id=event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    return route, plan


def _make_runner(
    temp_storage: SQLiteStorage,
    route: Route,
    adapter: FakePresentationAdapter,
) -> PipelineRunner:
    return PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={"dest": adapter},
        )
    )


async def _outbox_rows(storage: SQLiteStorage, event_id: str) -> list[dict]:
    return await storage._read_all(
        "SELECT * FROM delivery_outbox WHERE event_id = ? " "ORDER BY attempt_number",
        (event_id,),
    )


def _future_now_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------------------
# Post-allocation refusal: deterministic reconciliation
# ---------------------------------------------------------------------------


async def test_refused_replay_generation_reconciles_to_cancelled(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replay refused after allocation becomes terminal cancelled evidence."""
    event = make_event(event_id="replay-refusal-001")
    await temp_storage.append(event)
    route, plan = _route_and_plan(event.event_id)
    adapter = FakePresentationAdapter(adapter_id="dest", channel="room")
    runner = _make_runner(temp_storage, route, adapter)
    runner.set_adapter_health_gate(_counter_health_gate(healthy_until=1))
    try:
        with caplog.at_level(logging.INFO):
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, plan)],
                source="replay",
                replay_run_id="run-refused",
            )

        # The refusal held the send: the adapter was never invoked.
        assert adapter.delivered_payloads == []
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert outcomes[0].failure_kind_detail == REPLAY_REFUSAL_RECONCILED_DETAIL

        # The allocated generation reached the explicit reconciled terminal
        # state instead of dangling as outstanding in_progress work.
        rows = await _outbox_rows(temp_storage, event.event_id)
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["attempt_number"] == 1
        assert rows[0]["worker_id"] is None
        assert rows[0]["lease_until"] is None
        assert rows[0]["next_attempt_at"] is None

        # The reconciled generation carries its own cancelled lifecycle
        # evidence with the replay provenance preserved. No failed attempt
        # receipt exists: nothing was ever attempted.
        receipts = await temp_storage.list_receipts_for_event(event.event_id)
        assert [receipt.status for receipt in receipts] == ["cancelled"]
        lifecycle = receipts[0]
        assert lifecycle.receipt_kind == "lifecycle"
        assert lifecycle.attempt_number == 1
        assert lifecycle.source == "replay"
        assert lifecycle.replay_run_id == "run-refused"
        assert lifecycle.outbox_id == rows[0]["outbox_id"]

        # Refusal surfaced once through the existing deferral diagnostic;
        # the reconciliation added a single record and no traceback.
        diagnostician = runner._diagnostician
        assert diagnostician.snapshot()["deferrals"] == {"dest": 1}
        assert diagnostician.snapshot()["adapter_failures"] == {}
        reconciled_lines = [
            record
            for record in caplog.records
            if "replay_refusal_reconciled:" in record.getMessage()
        ]
        assert len(reconciled_lines) == 1
        assert "Traceback" not in caplog.text
    finally:
        await runner.stop()


async def test_reconciled_generation_is_never_redispatched_and_replay_continues(
    temp_storage: SQLiteStorage,
) -> None:
    """Recovery never claims the reconciled generation; a new replay delivers."""
    event = make_event(event_id="replay-refusal-002")
    await temp_storage.append(event)
    route, plan = _route_and_plan(event.event_id)
    adapter = FakePresentationAdapter(adapter_id="dest", channel="room")
    runner = _make_runner(temp_storage, route, adapter)
    runner.set_adapter_health_gate(_counter_health_gate(healthy_until=1))
    try:
        refused = await runner.deliver_to_targets(
            event,
            [(route, plan)],
            source="replay",
            replay_run_id="run-refused",
        )
        assert refused[0].failure_kind_detail == REPLAY_REFUSAL_RECONCILED_DETAIL

        # Recovery with an expired lease finds nothing to claim: terminal
        # rows are outside the claim vocabulary entirely.
        claimable = await temp_storage.claim_due_outbox_items(
            _future_now_iso(),
            "retry-worker",
            lease_seconds=30,
        )
        assert claimable == []

        # A subsequent replay of the same obligation is not blocked by the
        # refused generation: it allocates the next generation and delivers.
        runner.set_adapter_health_gate(AdapterHealthGate(lambda adapter_id: "healthy"))
        delivered = await runner.deliver_to_targets(
            event,
            [(route, plan)],
            source="replay",
            replay_run_id="run-second",
        )
        assert delivered[0].status == "success"
        assert len(adapter.delivered_payloads) == 1

        rows = await _outbox_rows(temp_storage, event.event_id)
        assert [row["status"] for row in rows] == ["cancelled", "sent"]
        assert [row["attempt_number"] for row in rows] == [1, 2]

        receipts = await temp_storage.list_receipts_for_event(event.event_id)
        assert [receipt.status for receipt in receipts] == [
            "cancelled",
            "sent",
        ]
        assert receipts[1].attempt_number == 2
        assert receipts[1].replay_run_id == "run-second"
    finally:
        await runner.stop()


async def test_live_health_deferral_keeps_held_obligation_semantics(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Live-source deferral still releases the reserved attempt to retry_wait."""
    event = make_event(event_id="live-deferral-guard")
    route, _ = _route_and_plan(event.event_id)
    adapter = FakePresentationAdapter(adapter_id="dest", channel="room")
    runner = _make_runner(temp_storage, route, adapter)
    runner.set_adapter_health_gate(AdapterHealthGate(lambda adapter_id: "degraded"))
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(event)

        assert adapter.delivered_payloads == []

        rows = await _outbox_rows(temp_storage, event.event_id)
        assert len(rows) == 1
        assert rows[0]["status"] == "retry_wait"
        assert rows[0]["next_attempt_at"] is not None
        assert await temp_storage.list_receipts_for_event(event.event_id) == []
        reconciled_lines = [
            record
            for record in caplog.records
            if "replay_refusal_reconciled:" in record.getMessage()
        ]
        assert reconciled_lines == []
    finally:
        await runner.stop()


async def test_content_authority_refusal_finalizes_replay_generation_terminal(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-allocation authority refusals already end unclaimable: pinned."""
    event = make_event(event_id="replay-refusal-003")
    await temp_storage.append(event)
    route, plan = _route_and_plan(event.event_id)
    adapter = FakePresentationAdapter(adapter_id="dest", channel="room")
    runner = _make_runner(temp_storage, route, adapter)
    monkeypatch.setattr(
        temp_storage,
        "authorize_native_submission",
        AsyncMock(return_value=False),
    )
    try:
        outcomes = await runner.deliver_to_targets(
            event,
            [(route, plan)],
            source="replay",
            replay_run_id="run-authority-refused",
        )

        assert adapter.delivered_payloads == []
        assert len(outcomes) == 1
        assert outcomes[0].status == "permanent_failure"
        assert outcomes[0].error == "content_authority_denied"

        # The generation terminated deterministically with no fabricated
        # attempt evidence, and recovery cannot claim it.
        rows = await _outbox_rows(temp_storage, event.event_id)
        assert len(rows) == 1
        assert rows[0]["status"] == "dead_lettered"
        assert await temp_storage.list_receipts_for_event(event.event_id) == []
        claimable = await temp_storage.claim_due_outbox_items(
            _future_now_iso(),
            "retry-worker",
            lease_seconds=30,
        )
        assert claimable == []
    finally:
        await runner.stop()


# ---------------------------------------------------------------------------
# Reconciliation guard arms: refused inputs and lost transitions
# ---------------------------------------------------------------------------


def _owned_generation_ctx(
    row: DeliveryOutboxItem,
    *,
    pipeline_worker: str = "pipeline:owner",
) -> OutboxContext:
    return OutboxContext(
        outbox_id=row.outbox_id,
        created=True,
        pipeline_worker=pipeline_worker,
        skip_reason=None,
        attempt_number=row.attempt_number,
    )


async def _claim_in_progress_row(
    temp_storage: SQLiteStorage,
    event_id: str,
    worker_id: str,
) -> DeliveryOutboxItem:
    """Admit an event, stage attempt-zero work, and claim it for *worker_id*."""
    item = make_outbox_item(status="pending", attempt_number=0, event_id=event_id)
    item = await create_outbox_item_with_parent(temp_storage, item)
    claimed = await temp_storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), worker_id
    )
    assert any(entry.outbox_id == item.outbox_id for entry in claimed)
    return await temp_storage.get_outbox_item(item.outbox_id)


async def _allocate_replay(
    storage: SQLiteStorage, event_id: str, worker_id: str
) -> DeliveryOutboxItem:
    item = replace(
        make_outbox_item(status="in_progress", event_id=event_id),
        worker_id=worker_id,
        dispatch_source="replay",
        replay_run_id="run-refused",
    )
    return await create_outbox_item_with_parent(
        storage, item, allocate_new_generation=True
    )


async def test_reconciliation_preserves_evidence_added_before_terminal_write(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _allocate_replay(temp_storage, "replay-refusal-race", "holder")
    attempt_receipt = build_delivery_receipt(
        event_id=row.event_id,
        delivery_plan_id=row.delivery_plan_id,
        target_adapter=row.target_adapter,
        target_channel=row.target_channel,
        route_id=row.route_id,
        status="queued",
        receipt_kind="attempt",
        source="replay",
        replay_run_id=row.replay_run_id,
        attempt_number=row.attempt_number,
        outbox_id=row.outbox_id,
    )
    finalize = temp_storage.finalize_outbox_terminal

    async def commit_after_attempt_evidence(
        command: TerminalOutboxFinalization,
    ) -> bool:
        await temp_storage.append_receipt(attempt_receipt)
        return await finalize(command)

    monkeypatch.setattr(
        temp_storage, "finalize_outbox_terminal", commit_after_attempt_evidence
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    assert not await manager.reconcile_refused_replay_generation(
        _owned_generation_ctx(row, pipeline_worker="holder"), reason="refused"
    )
    assert await temp_storage.get_outbox_item(row.outbox_id) == row
    receipts = await temp_storage.list_receipts_for_event(row.event_id)
    assert [receipt.receipt_id for receipt in receipts] == [attempt_receipt.receipt_id]


def test_unsubmitted_replay_finalization_requires_an_explicit_owner() -> None:
    receipt = build_delivery_receipt(
        event_id="replay-refusal-unowned",
        delivery_plan_id="plan",
        target_adapter="dest",
        target_channel="room",
        route_id="route",
        status="cancelled",
        receipt_kind="lifecycle",
        source="replay",
        attempt_number=1,
        outbox_id="obox-unowned",
    )
    with pytest.raises(ValueError, match="requires an owned cancelled replay"):
        TerminalOutboxFinalization(
            lifecycle_receipt=receipt, require_unsubmitted_replay=True
        )


async def test_reconciliation_requires_an_allocated_generation(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outbox-less, uncreated, or duplicate-run contexts never touch storage."""
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    read = AsyncMock(wraps=temp_storage.get_outbox_item)
    monkeypatch.setattr(temp_storage, "get_outbox_item", read)
    assert (
        await manager.reconcile_refused_replay_generation(
            OutboxContext(
                outbox_id=None,
                created=True,
                pipeline_worker="pipeline:owner",
                skip_reason=None,
            ),
            reason="refused",
        )
        is False
    )
    assert (
        await manager.reconcile_refused_replay_generation(
            OutboxContext(
                outbox_id="obox-any",
                created=False,
                pipeline_worker="pipeline:owner",
                skip_reason=None,
            ),
            reason="refused",
        )
        is False
    )
    assert (
        await manager.reconcile_refused_replay_generation(
            OutboxContext(
                outbox_id="obox-any",
                created=True,
                pipeline_worker="pipeline:owner",
                skip_reason=None,
                replay_duplicate=True,
            ),
            reason="refused",
        )
        is False
    )
    read.assert_not_awaited()


async def test_reconciliation_returns_false_for_a_missing_generation(
    temp_storage: SQLiteStorage,
) -> None:
    """A vanished generation refuses reconciliation without touching state."""
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    assert (
        await manager.reconcile_refused_replay_generation(
            OutboxContext(
                outbox_id="obox-missing",
                created=True,
                pipeline_worker="pipeline:owner",
                skip_reason=None,
                attempt_number=1,
            ),
            reason="refused",
        )
        is False
    )


async def test_reconciliation_refuses_a_generation_owned_elsewhere(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row this worker does not own keeps its existing reclaim semantics."""
    row = await _allocate_replay(temp_storage, "replay-refusal-owner", "holder")
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    assert (
        await manager.reconcile_refused_replay_generation(
            _owned_generation_ctx(row, pipeline_worker="pipeline:other-worker"),
            reason="refused",
        )
        is False
    )
    current = await temp_storage.get_outbox_item(row.outbox_id)
    assert current is not None
    assert current.status == "in_progress"
    assert current.worker_id == "holder"
    assert await temp_storage.list_receipts_for_event(row.event_id) == []
    assert sum("reconciliation skipped" in r.getMessage() for r in caplog.records) == 1


async def test_reconciliation_refuses_live_work_owned_by_the_same_worker(
    temp_storage: SQLiteStorage,
) -> None:
    row = await _claim_in_progress_row(temp_storage, "replay-refusal-live", "holder")
    assert await temp_storage.reserve_outbox_attempt(row.outbox_id, "holder", 0) == 1
    context = replace(
        _owned_generation_ctx(row, pipeline_worker="holder"), attempt_number=1
    )
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    assert not await manager.reconcile_refused_replay_generation(
        context, reason="refused"
    )
    retained = await temp_storage.get_outbox_item(row.outbox_id)
    assert retained is not None and retained.active_attempt == 1
    assert retained.status == "in_progress"
    assert await temp_storage.list_receipts_for_event(row.event_id) == []


async def test_reconciliation_survives_storage_faults(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistence fault logs once and never raises out of reconciliation."""
    row = await _allocate_replay(temp_storage, "replay-refusal-fault", "holder")
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    monkeypatch.setattr(
        temp_storage,
        "get_outbox_item",
        AsyncMock(side_effect=RuntimeError("storage fault")),
    )
    with caplog.at_level(logging.WARNING):
        assert (
            await manager.reconcile_refused_replay_generation(
                _owned_generation_ctx(row, pipeline_worker="holder"),
                reason="refused",
            )
            is False
        )
    fault_lines = [
        record
        for record in caplog.records
        if "reconciliation failed" in record.getMessage()
    ]
    assert len(fault_lines) == 1
    assert "RuntimeError" in fault_lines[0].getMessage()


async def test_reconciliation_loss_keeps_preexisting_row_semantics(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost guarded transition leaves the row to its existing authority."""
    row = await _allocate_replay(temp_storage, "replay-refusal-loss", "holder")
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    monkeypatch.setattr(
        temp_storage,
        "finalize_outbox_terminal",
        AsyncMock(return_value=False),
    )
    with caplog.at_level(logging.WARNING):
        assert (
            await manager.reconcile_refused_replay_generation(
                _owned_generation_ctx(row, pipeline_worker="holder"),
                reason="refused",
            )
            is False
        )
    loss_lines = [
        record for record in caplog.records if "did not commit" in record.getMessage()
    ]
    assert len(loss_lines) == 1
    current = await temp_storage.get_outbox_item(row.outbox_id)
    assert current is not None
    assert current.status == "in_progress"
    assert current.worker_id == "holder"
    assert await temp_storage.list_receipts_for_event(row.event_id) == []


# ---------------------------------------------------------------------------
# Static guard: reconciliation detail vocabulary stays aligned
# ---------------------------------------------------------------------------


def test_reconciliation_detail_is_stable() -> None:
    """The outcome detail string is a stable operator-facing vocabulary."""
    assert REPLAY_REFUSAL_RECONCILED_DETAIL == (
        "replay_generation_reconciled:cancelled"
    )
