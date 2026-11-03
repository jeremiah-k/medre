"""Adversarial qualification of the exact-target property on real SQLite.

Every test here runs against an on-disk SQLite database (never ``:memory:``
where transaction interleaving matters) and drives real transactions:
competing writers step genuine ``BEGIN IMMEDIATE`` transactions on separate
connections, replay admission runs the production delivery gate, and the
delivery lifecycle stages genuine outbox generations and receipts through
the fake presentation adapter.

Properties qualified:

* **Serialization of write authorities.** Exact-target cancellation and
  the audited decision authority serialize against a live competing write
  transaction; the superseded authority observes a typed refusal, storage
  keeps exactly one committed outcome, and nothing half-applies.
* **Restart durability.** A recorded decision, the persisted outbox state,
  and the preview's classification all survive closing and reopening the
  storage handle, and the normal claim/attempt fences keep refusing
  superseded observations afterwards.
* **Cancellation fencing.** A scoped replay refuses named targets whose
  persisted state drifted, records the bounded
  ``exact_target_selection_refused`` diagnostic evidence, and stages
  nothing for refused or excluded siblings.
* **Sibling-send avoidance.** A scoped replay stages exactly one new
  generation and one receipt for the selected target; sibling channels and
  sibling adapters of the same event are never staged, dispatched, or
  receipted.
* **Stale observations never send.** A preview classification that is
  stale by execution time causes admission refusal or fresh re-derivation;
  execution re-validates persisted state through the same selector the
  read-only preview uses.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.replay import delivery as replay_delivery
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, DeliveryReceipt
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    stable_delivery_plan_id as _stable_plan_id,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
)
from medre.core.storage.sqlite._operator_decision import (
    RESOLVE_UNCERTAIN_ACTION,
    CancelUnstartedDecision,
    ExactTargetDecision,
    OperatorDecisionRefused,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_pipeline_config_for_pipeline
from tests.helpers.replay import make_engine
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)

_EVENT = "evt-exact-qual"
_SOURCE_ADAPTER = "fake_transport"
_SOURCE_CHANNEL = "ch-0"
_ADAPTER = "fake_presentation"
_OTHER_ADAPTER = "fake_presentation-b"
_CHANNEL_A = "ch-a"
_CHANNEL_B = "ch-b"
_ROUTE = "qual-route"
_RUN_ID = "qual-run"


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _plan_id(
    channel: str | None,
    adapter: str = _ADAPTER,
    event_id: str = _EVENT,
    index: int = 0,
) -> str:
    """The deterministic plan id the production planner derives."""
    return _stable_plan_id(
        event_id,
        RouteTarget(adapter=adapter, channel=channel),
        route_id=_ROUTE,
        target_index=index,
    )


def _identity(
    channel: str | None,
    adapter: str = _ADAPTER,
    event_id: str = _EVENT,
    index: int = 0,
) -> DeliveryIdentity:
    plan = _plan_id(channel, adapter=adapter, event_id=event_id, index=index)
    return DeliveryIdentity(event_id, plan, adapter, channel)


def _selection(
    channel: str | None,
    adapter: str = _ADAPTER,
    event_id: str = _EVENT,
    index: int = 0,
) -> ExactTargetSelection:
    return ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=_plan_id(
            channel, adapter=adapter, event_id=event_id, index=index
        ),
        target_adapter=adapter,
        target_channel=channel,
    )


async def _seed_pending_row(
    storage: SQLiteStorage,
    channel: str | None,
    *,
    adapter: str = _ADAPTER,
    event_id: str = _EVENT,
    index: int = 0,
) -> DeliveryOutboxItem:
    """Admit the event and create one production pending generation."""
    return await create_outbox_item_with_parent(
        storage,
        make_outbox_item(
            delivery_plan_id=_plan_id(
                channel, adapter=adapter, event_id=event_id, index=index
            ),
            target_adapter=adapter,
            target_channel=channel,
            event_id=event_id,
        ),
    )


def _cancel_decision(
    row: DeliveryOutboxItem,
    identity: DeliveryIdentity,
    *,
    decision_id: str = "qual-cancel",
    expected_status: str | None = None,
    expected_updated_at: str | None = None,
) -> CancelUnstartedDecision:
    return CancelUnstartedDecision(
        decision_id=decision_id,
        actor_id="qual-operator",
        identity=identity,
        outbox_id=row.outbox_id,
        expected_generation=row.attempt_number,
        expected_status=expected_status or row.status,
        expected_updated_at=expected_updated_at or row.updated_at,
    )


def _target_decision(
    row: DeliveryOutboxItem,
    identity: DeliveryIdentity,
    *,
    decision_id: str = "qual-decision",
    action: str = RESOLVE_UNCERTAIN_ACTION,
    expected_status: str | None = None,
    expected_updated_at: str | None = None,
) -> ExactTargetDecision:
    return ExactTargetDecision(
        decision_id=decision_id,
        actor_id="qual-operator",
        action=action,
        identity=identity,
        outbox_id=row.outbox_id,
        expected_generation=row.attempt_number,
        expected_status=expected_status or row.status,
        expected_updated_at=expected_updated_at or row.updated_at,
    )


def _row_facts(row: DeliveryOutboxItem) -> tuple[Any, ...]:
    """The persisted facts a refusal must leave exactly as observed."""
    return (
        row.status,
        row.attempt_number,
        row.active_attempt,
        row.worker_id,
        row.updated_at,
        row.replay_run_id,
    )


async def _snapshot_outbox(
    storage: SQLiteStorage, event_id: str = _EVENT
) -> dict[str, tuple[Any, ...]]:
    rows = await storage.list_outbox_items_for_event(event_id)
    return {row.outbox_id: _row_facts(row) for row in rows}


def _router(*targets: RouteTarget, route_id: str = _ROUTE) -> Router:
    return Router(
        routes=[
            Route(
                id=route_id,
                source=RouteSource(
                    adapter=_SOURCE_ADAPTER,
                    event_kinds=("message.created",),
                    channel=_SOURCE_CHANNEL,
                ),
                targets=list(targets),
            )
        ]
    )


def _sibling_targets() -> tuple[RouteTarget, RouteTarget]:
    """Two same-adapter sibling channels on the shared qualification route."""
    return (
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_A),
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_B),
    )


def _rendering_evidence(channel: str | None, adapter: str) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "renderer": "text",
            "delivery_strategy": "direct",
            "target_adapter": adapter,
            "target_platform": "fake_presentation",
            "target_channel": channel,
            "capability_level": "native",
            "relation_evidence": [],
            "source_origin_label": "Qualification",
        }
    )


async def _seed_event_with_targets(
    storage: SQLiteStorage,
    *targets: RouteTarget,
    event_id: str = _EVENT,
) -> dict[str | None, DeliveryOutboxItem]:
    """Seed the event, one pending row and one live receipt per target.

    The live receipt gives the replay render stage its persisted rendering
    context per delivery identity, mirroring an event that was delivered
    live before recovery. The pending row gives the exact-target selection
    one uncontradicted persisted generation to resolve.
    """
    if await storage.get(event_id) is None:
        await storage.append(make_storage_event(event_id=event_id))
    rows: dict[str | None, DeliveryOutboxItem] = {}
    for index, target in enumerate(targets):
        row = await _seed_pending_row(
            storage,
            target.channel,
            adapter=target.adapter,
            event_id=event_id,
            index=index,
        )
        rows[target.channel] = row
        await storage.append_receipt(
            DeliveryReceipt(
                receipt_id=f"rcpt-live-{event_id}-{index}",
                event_id=event_id,
                delivery_plan_id=row.delivery_plan_id,
                target_adapter=target.adapter,
                target_channel=target.channel,
                route_id=_ROUTE,
                status="sent",
                source="live",
                rendering_evidence=_rendering_evidence(target.channel, target.adapter),
            )
        )
    return rows


async def _seed_binding_refused_row(
    storage: SQLiteStorage,
    channel: str | None,
    *,
    index: int = 0,
    adapter: str = _ADAPTER,
    event_id: str = _EVENT,
) -> DeliveryOutboxItem:
    """Create one pending row whose frozen staged plan names another plan.

    The row is created in one shot with its contradicting snapshot: an
    adopted existing row never gains a later snapshot, so the drift must
    be frozen at creation. The route binding agrees with the row; the
    rebound ``plan_id`` is the only contradiction.
    """
    if await storage.get(event_id) is None:
        await storage.append(make_storage_event(event_id=event_id))
    item = make_outbox_item(
        delivery_plan_id=_plan_id(
            channel, adapter=adapter, event_id=event_id, index=index
        ),
        target_adapter=adapter,
        target_channel=channel,
        event_id=event_id,
    )
    return await storage.create_outbox_item(
        item,
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context={
                "route": {"id": item.route_id},
                "plan": {
                    "plan_id": "plan-rebound",
                    "event_id": event_id,
                    "route_id": item.route_id,
                    "target": {"adapter": adapter, "channel": channel},
                },
                "routing": {},
            },
        ),
    )


def _real_pipeline(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any],
) -> PipelineRunner:
    return PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=storage, router=router, adapters=adapters
        )
    )


async def _deliver_result(
    storage: SQLiteStorage,
    pipeline: PipelineRunner,
    request: ReplayRequest,
    diagnostic: Diagnostician | None = None,
) -> Any:
    engine = make_engine(storage, pipeline=pipeline)
    if diagnostic is not None:
        engine._diagnostician = diagnostic
    results = [result async for result in engine.replay(request)]
    deliver = [result for result in results if result.stage == "deliver"]
    assert len(deliver) == 1
    return deliver[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _allocate_replay_generation(
    storage: SQLiteStorage, row: DeliveryOutboxItem
) -> DeliveryOutboxItem:
    """Allocate the next generation for one identity via the allocator."""
    created = await storage.create_outbox_item(
        make_outbox_item(
            delivery_plan_id=row.delivery_plan_id,
            target_adapter=row.target_adapter,
            target_channel=row.target_channel,
            event_id=row.event_id,
        ),
        allocate_new_generation=True,
    )
    assert created.outbox_id != row.outbox_id
    return created


# ---------------------------------------------------------------------------
# Property 1: write authorities serialize on real transactions
# ---------------------------------------------------------------------------


async def _run_parked_authority_race(
    storage: SQLiteStorage,
    authority: Any,
    *,
    competing_update: str,
    competing_params: tuple[Any, ...],
) -> tuple[BaseException | None, list[str]]:
    """Run *authority* against a live competing write transaction.

    The competitor holds a genuine ``BEGIN IMMEDIATE`` write transaction
    with an uncommitted UPDATE before the authority is dispatched, commits
    while the authority is in flight, and the storage connection's trace
    callback records the order in which the authority's transaction
    statements actually executed. SQLite's single-writer rule makes the
    interleaving deterministic: the authority's first in-transaction read
    can only execute after the competitor's commit.
    """
    db_path = storage._db_path
    competitor = sqlite3.connect(db_path, timeout=10.0)
    timeline: list[str] = []
    connection = storage._require_db()
    authority_started = threading.Event()

    def trace(statement: str) -> None:
        first_line = statement.splitlines()[0]
        if first_line == "BEGIN IMMEDIATE":
            timeline.append("authority:begin")
            authority_started.set()
        if first_line.startswith("SELECT * FROM operator_delivery_decisions"):
            timeline.append("authority:first-txn-read")

    connection.set_trace_callback(trace)
    try:
        competitor.execute("BEGIN IMMEDIATE")
        competitor.execute(competing_update, competing_params)
        task = asyncio.ensure_future(authority())
        try:
            started = await asyncio.to_thread(authority_started.wait, 5.0)
        finally:
            # Release the competitor even when the authority fails to start.
            timeline.append("competitor:commit")
            competitor.execute("COMMIT")
        try:
            await task
            failure: BaseException | None = None
        except BaseException as exc:
            failure = exc
        assert started, "authority did not reach its write transaction"
        return failure, timeline
    finally:
        connection.set_trace_callback(None)
        competitor.close()


async def test_cancel_serializes_behind_a_competing_claim_transaction(
    temp_storage: SQLiteStorage,
) -> None:
    """Cancellation parked behind a live claim observes it and refuses."""
    row = await _seed_pending_row(temp_storage, _CHANNEL_A)
    identity = _identity(_CHANNEL_A)
    before = _row_facts(row)
    assert await temp_storage.list_operator_decisions(_EVENT) == []

    failure, timeline = await _run_parked_authority_race(
        temp_storage,
        lambda: temp_storage.cancel_unstarted_delivery(_cancel_decision(row, identity)),
        competing_update=(
            "UPDATE delivery_outbox SET status='in_progress', "
            "worker_id='w-race', locked_at=?, lease_until=? "
            "WHERE outbox_id=?"
        ),
        competing_params=(_now_iso(), _now_iso(), row.outbox_id),
    )

    assert isinstance(failure, OperatorDecisionRefused)
    assert str(failure) == ("Stale generation or active attempt; nothing cancelled")
    # The authority's first in-transaction read ran after the competitor
    # committed: BEGIN IMMEDIATE serialized the two write transactions.
    assert (
        timeline.index("authority:begin")
        < timeline.index("competitor:commit")
        < timeline.index("authority:first-txn-read")
    )
    after = await temp_storage.get_outbox_item(row.outbox_id)
    assert after is not None
    assert (after.status, after.worker_id) == ("in_progress", "w-race")
    assert after.attempt_number == before[1]
    # No lost update and nothing half-applied: the refusal wrote no audit
    # row and left the competitor's claim as the only committed outcome.
    assert await temp_storage.list_operator_decisions(_EVENT) == []


async def test_decision_serializes_behind_a_competing_claim_transaction(
    temp_storage: SQLiteStorage,
) -> None:
    """A decided target claimed mid-flight refuses the decision, not CAS."""
    row = await _seed_pending_row(temp_storage, _CHANNEL_A)
    identity = _identity(_CHANNEL_A)

    failure, timeline = await _run_parked_authority_race(
        temp_storage,
        lambda: temp_storage.record_exact_target_decision(
            _target_decision(row, identity)
        ),
        competing_update=(
            "UPDATE delivery_outbox SET status='in_progress', "
            "worker_id='w-race', locked_at=?, lease_until=? "
            "WHERE outbox_id=?"
        ),
        competing_params=(_now_iso(), _now_iso(), row.outbox_id),
    )

    assert isinstance(failure, OperatorDecisionRefused)
    assert str(failure) == (
        "In-progress, queued or uncertain native work is not decidable by"
        " this authority"
    )
    assert (
        timeline.index("authority:begin")
        < timeline.index("competitor:commit")
        < timeline.index("authority:first-txn-read")
    )
    after = await temp_storage.get_outbox_item(row.outbox_id)
    assert after is not None
    assert (after.status, after.worker_id) == ("in_progress", "w-race")
    assert await temp_storage.list_operator_decisions(_EVENT) == []


async def test_cancel_after_a_competing_generation_refuses_typed(
    temp_storage: SQLiteStorage,
) -> None:
    """A committed sibling generation supersedes the cancel observation."""
    row = await _seed_pending_row(temp_storage, _CHANNEL_A)
    identity = _identity(_CHANNEL_A)

    newer = await _allocate_replay_generation(temp_storage, row)
    assert newer.attempt_number == row.attempt_number + 1

    with pytest.raises(OperatorDecisionRefused) as raised:
        await temp_storage.cancel_unstarted_delivery(_cancel_decision(row, identity))
    assert str(raised.value) == "A competing delivery generation exists"

    survivor = await temp_storage.get_outbox_item(row.outbox_id)
    assert survivor is not None
    assert survivor.status == "pending"
    assert await temp_storage.list_operator_decisions(_EVENT) == []


async def test_allocation_after_cancel_commits_above_the_cancelled_generation(
    temp_storage: SQLiteStorage,
) -> None:
    """Both serialization orders keep one committed outcome per authority."""
    row = await _seed_pending_row(temp_storage, _CHANNEL_A)
    identity = _identity(_CHANNEL_A)

    assert await temp_storage.cancel_unstarted_delivery(_cancel_decision(row, identity))
    cancelled = await temp_storage.get_outbox_item(row.outbox_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    newer = await _allocate_replay_generation(temp_storage, row)
    assert newer.attempt_number == 2

    # The cancel outcome is never lost: the terminal row stays cancelled
    # and the allocator staged a fresh generation instead of resurrecting
    # or rewinding it.
    settled = await temp_storage.get_outbox_item(row.outbox_id)
    assert settled is not None
    assert settled.status == "cancelled"
    assert settled.attempt_number == 1
    audits = await temp_storage.list_operator_decisions(_EVENT)
    assert [audit["action"] for audit in audits] == ["cancel_unstarted"]


async def test_decision_and_cancel_settle_one_outcome_in_each_order(
    temp_storage: SQLiteStorage,
) -> None:
    """Decision-then-cancel commits both; cancel-then-decision refuses."""
    decided = await _seed_pending_row(temp_storage, _CHANNEL_A)
    decided_identity = _identity(_CHANNEL_A)
    assert await temp_storage.record_exact_target_decision(
        _target_decision(decided, decided_identity, decision_id="d-first")
    )
    # The decision mutates nothing, so the cancel over the same observed
    # tuple still commits: the claim that follows a committed decision is
    # the decided outcome, not a conflict.
    assert await temp_storage.cancel_unstarted_delivery(
        _cancel_decision(decided, decided_identity, decision_id="c-second")
    )
    audits = await temp_storage.list_operator_decisions(_EVENT)
    assert {audit["decision_id"] for audit in audits} == {"d-first", "c-second"}
    settled_row = await temp_storage.get_outbox_item(decided.outbox_id)
    assert settled_row is not None
    assert settled_row.status == "cancelled"

    cancelled = await _seed_pending_row(temp_storage, _CHANNEL_B, index=1)
    cancelled_identity = _identity(_CHANNEL_B, index=1)
    assert await temp_storage.cancel_unstarted_delivery(
        _cancel_decision(cancelled, cancelled_identity, decision_id="c-first")
    )
    with pytest.raises(OperatorDecisionRefused) as raised:
        await temp_storage.record_exact_target_decision(
            _target_decision(cancelled, cancelled_identity, decision_id="d-second")
        )
    assert str(raised.value) == (
        "Completed or cancelled delivery work is not decidable by this" " authority"
    )
    # The refused decision recorded nothing over the settled target.
    actions = [
        audit["action"] for audit in await temp_storage.list_operator_decisions(_EVENT)
    ]
    assert actions.count("resolve_uncertain") == 1


# ---------------------------------------------------------------------------
# Property 2: decisions, classifications, and fences survive restart
# ---------------------------------------------------------------------------


async def test_decision_audit_and_classification_survive_restart(
    tmp_path: Path,
) -> None:
    """A recorded decision and preview re-derive identically after reopen."""
    db_path = str(tmp_path / "qual-restart.db")
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        await storage.append(make_storage_event(event_id=_EVENT))
        row = await _seed_pending_row(storage, _CHANNEL_A)
        identity = _identity(_CHANNEL_A)
        assert await storage.record_exact_target_decision(
            _target_decision(row, identity, decision_id="qual-restart")
        )
        before = await preview_exact_target(storage, _selection(_CHANNEL_A))
    finally:
        await storage.close()

    reopened = SQLiteStorage(db_path=db_path)
    await reopened.initialize()
    try:
        audits = await reopened.list_operator_decisions(_EVENT)
        assert [audit["decision_id"] for audit in audits] == ["qual-restart"]
        assert audits[0]["action"] == RESOLVE_UNCERTAIN_ACTION
        assert audits[0]["observed_status"] == "pending"

        # The identical decision is idempotent across handles; a reused ID
        # bound to different parameters fails closed.
        persisted = await reopened.get_outbox_item(row.outbox_id)
        assert persisted is not None
        assert not await reopened.record_exact_target_decision(
            _target_decision(row, identity, decision_id="qual-restart")
        )
        with pytest.raises(OperatorDecisionRefused) as raised:
            await reopened.record_exact_target_decision(
                ExactTargetDecision(
                    decision_id="qual-restart",
                    actor_id="other-operator",
                    action=RESOLVE_UNCERTAIN_ACTION,
                    identity=identity,
                    outbox_id=row.outbox_id,
                    expected_generation=row.attempt_number,
                    expected_status=row.status,
                    expected_updated_at=row.updated_at,
                )
            )
        assert str(raised.value) == "Decision ID already binds another request"

        # Replay re-derives the identical classification and fingerprint
        # from persisted state alone.
        after = await preview_exact_target(reopened, _selection(_CHANNEL_A))
        assert after.category == before.category == "pending"
        assert after.observed_fingerprint == before.observed_fingerprint
        assert after.outbox_id == before.outbox_id
        assert after.attempt_number == before.attempt_number
    finally:
        await reopened.close()


async def test_claim_and_attempt_fences_hold_after_restart(
    tmp_path: Path,
) -> None:
    """The normal claim/generation fences keep refusing after reopen."""
    db_path = str(tmp_path / "qual-fences.db")
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        await storage.append(make_storage_event(event_id=_EVENT))
        claimed_row = await _seed_pending_row(storage, _CHANNEL_A)
        await storage.record_exact_target_decision(
            _target_decision(claimed_row, _identity(_CHANNEL_A), decision_id="d-a")
        )
    finally:
        await storage.close()

    reopened = SQLiteStorage(db_path=db_path)
    await reopened.initialize()
    try:
        # A claim that follows a committed decision is the decided
        # outcome: the row is claimable exactly as before the restart.
        claimed = await reopened.claim_due_outbox_items(
            _now_iso(), "worker-after-restart"
        )
        assert [item.outbox_id for item in claimed] == [claimed_row.outbox_id]

        reserved = await reopened.reserve_outbox_attempt(
            claimed_row.outbox_id, "worker-after-restart", claimed_row.attempt_number
        )
        assert reserved == claimed_row.attempt_number + 1
        persisted = await reopened.get_outbox_item(claimed_row.outbox_id)
        assert persisted is not None
        assert persisted.active_attempt == reserved
        assert persisted.attempt_number == claimed_row.attempt_number
        assert (
            await reopened.reserve_outbox_attempt(
                claimed_row.outbox_id,
                "worker-after-restart",
                claimed_row.attempt_number,
            )
            is None
        )

        # The pre-restart observation is superseded by the claim: the
        # cancellation authority refuses it with the typed refusal.
        with pytest.raises(OperatorDecisionRefused) as cancel_refusal:
            await reopened.cancel_unstarted_delivery(
                _cancel_decision(claimed_row, _identity(_CHANNEL_A), decision_id="c-a")
            )
        assert str(cancel_refusal.value) == (
            "Stale generation or active attempt; nothing cancelled"
        )
        with pytest.raises(OperatorDecisionRefused) as decision_refusal:
            await reopened.record_exact_target_decision(
                _target_decision(
                    claimed_row,
                    _identity(_CHANNEL_A),
                    decision_id="d-a-2",
                    expected_status="in_progress",
                )
            )
        assert str(decision_refusal.value) == (
            "In-progress, queued or uncertain native work is not decidable"
            " by this authority"
        )

        # A new generation created after the restart supersedes the old
        # observation for the cancellation authority.
        superseded_row = await _seed_pending_row(reopened, _CHANNEL_B, index=1)
        newer = await _allocate_replay_generation(reopened, superseded_row)
        assert newer.attempt_number == 2
        with pytest.raises(OperatorDecisionRefused) as competing:
            await reopened.cancel_unstarted_delivery(
                _cancel_decision(
                    superseded_row,
                    _identity(_CHANNEL_B, index=1),
                    decision_id="c-b",
                )
            )
        assert str(competing.value) == ("A competing delivery generation exists")
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Property 3: cancellation fences in-flight scoped replay
# ---------------------------------------------------------------------------


async def test_scoped_replay_all_refused_stages_nothing(
    temp_storage: SQLiteStorage,
) -> None:
    """Every named target refused: bounded evidence, nothing staged."""
    targets = _sibling_targets()
    router = _router(*targets)
    await temp_storage.append(make_storage_event(event_id=_EVENT))
    row_b = await _seed_pending_row(temp_storage, _CHANNEL_B, index=1)

    # Drift target A with a contradicting frozen binding and contest B with
    # a second live generation: both named selections must refuse.
    await _seed_binding_refused_row(temp_storage, _CHANNEL_A)
    newer_b = await _allocate_replay_generation(temp_storage, row_b)
    assert newer_b.delivery_plan_id == _selection(_CHANNEL_B, index=1).delivery_plan_id
    contested = await preview_exact_target(
        temp_storage, _selection(_CHANNEL_B, index=1)
    )
    assert contested.category == "ambiguous"

    snapshot = await _snapshot_outbox(temp_storage)
    receipts_before = await temp_storage.list_receipts_for_event(_EVENT)
    diagnostic = Diagnostician()
    result = await _deliver_result(
        temp_storage,
        _real_pipeline(temp_storage, router, {}),
        ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[_EVENT],
            run_id=_RUN_ID,
            exact_target_selections=(
                _selection(_CHANNEL_A),
                _selection(_CHANNEL_B, index=1),
            ),
        ),
        diagnostic=diagnostic,
    )

    assert result.status == "skipped"
    assert result.error is not None
    reasons = result.error.split("; ")
    assert any(
        reason.startswith("exact_target_refused: route_or_plan_changed (")
        for reason in reasons
    )
    assert any(
        reason.startswith("exact_target_refused: ambiguous (") for reason in reasons
    )
    assert diagnostic.replay_skips == {"exact_target_selection_refused": 1}
    refusals = result.output["exact_target_refusals"]
    assert {refusal["category"] for refusal in refusals} == {
        "route_or_plan_changed",
        "ambiguous",
    }
    # Nothing was staged for the refused targets: the outbox and receipt
    # sets are byte-identical to the pre-replay snapshot.
    assert await _snapshot_outbox(temp_storage) == snapshot
    assert await temp_storage.list_receipts_for_event(_EVENT) == (receipts_before)


async def test_scoped_replay_partial_refusal_stages_only_the_survivor(
    temp_storage: SQLiteStorage,
) -> None:
    """A refused sibling leaves evidence and stages nothing for itself."""
    targets = _sibling_targets()
    router = _router(*targets)
    rows = await _seed_event_with_targets(temp_storage, targets[0])
    survivor_row = rows[_CHANNEL_A]
    refused_row = await _seed_binding_refused_row(temp_storage, _CHANNEL_B, index=1)

    snapshot = await _snapshot_outbox(temp_storage)
    diagnostic = Diagnostician()
    result = await _deliver_result(
        temp_storage,
        _real_pipeline(
            temp_storage,
            router,
            {
                _ADAPTER: FakePresentationAdapter(
                    adapter_id=_ADAPTER, channel=_CHANNEL_A
                )
            },
        ),
        ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[_EVENT],
            run_id=_RUN_ID,
            exact_target_selections=(
                _selection(_CHANNEL_A),
                _selection(_CHANNEL_B, index=1),
            ),
        ),
        diagnostic=diagnostic,
    )

    assert result.status == "passed"
    refusals = result.output["exact_target_refusals"]
    assert [refusal["delivery_plan_id"] for refusal in refusals] == [
        refused_row.delivery_plan_id
    ]
    assert refusals[0]["category"] == "route_or_plan_changed"
    # The bounded diagnostic key belongs to the all-refused skip path; a
    # delivered survivor surfaces the refusal in the result output instead.
    assert "exact_target_selection_refused" not in diagnostic.replay_skips

    run_rows = await temp_storage.list_outbox_items_by_replay_run(_RUN_ID)
    assert [row.attempt_number for row in run_rows] == [2]
    refused_rows = [
        row
        for row in await temp_storage.list_outbox_items_for_event(_EVENT)
        if row.delivery_plan_id == refused_row.delivery_plan_id
    ]
    assert len(refused_rows) == 1
    assert _row_facts(refused_rows[0]) == snapshot[refused_rows[0].outbox_id]
    replay_receipts = [
        receipt
        for receipt in await temp_storage.list_receipts_for_event(_EVENT)
        if receipt.replay_run_id == _RUN_ID
    ]
    assert [receipt.delivery_plan_id for receipt in replay_receipts] == [
        survivor_row.delivery_plan_id
    ]


async def test_cancelled_sibling_fences_the_scoped_replay_and_decisions(
    temp_storage: SQLiteStorage,
) -> None:
    """A cancelled sibling stays cancelled across a scoped replay."""
    targets = _sibling_targets()
    router = _router(*targets)
    rows = await _seed_event_with_targets(temp_storage, *targets)
    row_b = rows[_CHANNEL_B]
    identity_b = _identity(_CHANNEL_B, index=1)
    observed_b = (row_b.status, row_b.updated_at, row_b.attempt_number)

    assert await temp_storage.cancel_unstarted_delivery(
        _cancel_decision(row_b, identity_b, decision_id="c-sibling")
    )
    cancelled_b = await temp_storage.get_outbox_item(row_b.outbox_id)
    assert cancelled_b is not None
    assert cancelled_b.status == "cancelled"

    snapshot = await _snapshot_outbox(temp_storage)
    result = await _deliver_result(
        temp_storage,
        _real_pipeline(
            temp_storage,
            router,
            {
                _ADAPTER: FakePresentationAdapter(
                    adapter_id=_ADAPTER, channel=_CHANNEL_A
                )
            },
        ),
        ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[_EVENT],
            run_id=_RUN_ID,
            exact_target_selections=(_selection(_CHANNEL_A),),
        ),
    )

    assert result.status == "passed"
    assert "exact_target_refusals" not in result.output
    # The cancelled sibling was never widened into: no new generation, no
    # replay receipt, terminal status intact.
    sibling_rows = [
        row
        for row in await temp_storage.list_outbox_items_for_event(_EVENT)
        if row.delivery_plan_id == row_b.delivery_plan_id
    ]
    assert len(sibling_rows) == 1
    assert sibling_rows[0].status == "cancelled"
    assert _row_facts(sibling_rows[0]) == snapshot[sibling_rows[0].outbox_id]
    replay_receipts = [
        receipt
        for receipt in await temp_storage.list_receipts_for_event(_EVENT)
        if receipt.replay_run_id == _RUN_ID
    ]
    assert [receipt.target_channel for receipt in replay_receipts] == [_CHANNEL_A]

    # The fences keep holding after the replay: the idempotent cancel does
    # not reopen the row and a decision over the pre-cancel observation is
    # refused with the settled refusal.
    assert not await temp_storage.cancel_unstarted_delivery(
        _cancel_decision(row_b, identity_b, decision_id="c-sibling")
    )
    with pytest.raises(OperatorDecisionRefused) as raised:
        await temp_storage.record_exact_target_decision(
            ExactTargetDecision(
                decision_id="d-sibling",
                actor_id="qual-operator",
                action=RESOLVE_UNCERTAIN_ACTION,
                identity=identity_b,
                outbox_id=row_b.outbox_id,
                expected_generation=observed_b[2],
                expected_status=observed_b[0],
                expected_updated_at=observed_b[1],
            )
        )
    assert str(raised.value) == (
        "Completed or cancelled delivery work is not decidable by this" " authority"
    )


# ---------------------------------------------------------------------------
# Property 4: one selection never widens into sibling sends
# ---------------------------------------------------------------------------


async def test_scoped_replay_never_stages_or_receipts_sibling_targets(
    temp_storage: SQLiteStorage,
) -> None:
    """Only the selected identity gains a generation, dispatch, receipt."""
    targets = (
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_A),
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_B),
        RouteTarget(adapter=_OTHER_ADAPTER, channel=None),
    )
    router = _router(*targets)
    rows = await _seed_event_with_targets(temp_storage, *targets)
    row_a = rows[_CHANNEL_A]

    snapshot = await _snapshot_outbox(temp_storage)
    receipts_before = await temp_storage.list_receipts_for_event(_EVENT)
    result = await _deliver_result(
        temp_storage,
        _real_pipeline(
            temp_storage,
            router,
            {
                _ADAPTER: FakePresentationAdapter(
                    adapter_id=_ADAPTER, channel=_CHANNEL_A
                ),
                _OTHER_ADAPTER: FakePresentationAdapter(
                    adapter_id=_OTHER_ADAPTER, channel=None
                ),
            },
        ),
        ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[_EVENT],
            run_id=_RUN_ID,
            exact_target_selections=(_selection(_CHANNEL_A),),
        ),
    )

    assert result.status == "passed"
    delivered = result.output["adapter_results"]
    assert [outcome.target_channel for outcome in delivered] == [_CHANNEL_A]
    assert [outcome.target_adapter for outcome in delivered] == [_ADAPTER]

    # Exactly one new outbox generation exists, owned by the replay run and
    # allocated above the selected row; every sibling row is untouched.
    run_rows = await temp_storage.list_outbox_items_by_replay_run(_RUN_ID)
    assert [row.outbox_id for row in run_rows] != []
    assert all(row.delivery_plan_id == row_a.delivery_plan_id for row in run_rows)
    assert [row.attempt_number for row in run_rows] == [2]
    after = await _snapshot_outbox(temp_storage)
    assert set(after) - set(snapshot) == {run_rows[0].outbox_id}
    for outbox_id, facts in snapshot.items():
        if outbox_id != run_rows[0].outbox_id:
            assert after[outbox_id] == facts

    # Exactly one new receipt exists, for the selected target only.
    receipts_after = await temp_storage.list_receipts_for_event(_EVENT)
    new_receipts = [
        receipt for receipt in receipts_after if receipt not in receipts_before
    ]
    assert len(new_receipts) == 1
    assert new_receipts[0].replay_run_id == _RUN_ID
    assert new_receipts[0].target_channel == _CHANNEL_A
    assert new_receipts[0].target_adapter == _ADAPTER
    assert new_receipts[0].source == "replay"


# ---------------------------------------------------------------------------
# Property 5: a stale observation never sends
# ---------------------------------------------------------------------------


def test_execution_admission_shares_the_preview_selector() -> None:
    """Admission verifies through the exact preview function, structurally."""
    assert replay_delivery.preview_exact_target is preview_exact_target
    assert replay_delivery._EXACT_TARGET_REFUSAL_CATEGORIES == frozenset(
        {"not_found", "ambiguous", "route_or_plan_changed"}
    )


async def test_stale_expected_observation_classifies_stale_not_sendable(
    temp_storage: SQLiteStorage,
) -> None:
    """A superseded expectation is stale evidence, never authorization."""
    targets = (RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_A),)
    await _seed_event_with_targets(temp_storage, *targets)
    selection = _selection(_CHANNEL_A)

    observed = await preview_exact_target(temp_storage, selection)
    assert observed.category == "pending"
    assert observed.attempt_number == 1
    fingerprint = observed.observed_fingerprint

    # The normal claim/attempt fences advance the single live row after the
    # observation: the claim moves its status, then the attempt reservation
    # carries a live active_attempt.
    claimed = await temp_storage.claim_due_outbox_items(_now_iso(), "worker-stale")
    assert len(claimed) == 1
    stale_status = await preview_exact_target(
        temp_storage, selection, expected_fingerprint=fingerprint
    )
    assert stale_status.category == "stale"

    reserved = await temp_storage.reserve_outbox_attempt(
        claimed[0].outbox_id, "worker-stale", 1
    )
    assert reserved == 2
    stale_generation = await preview_exact_target(
        temp_storage, selection, expected_generation=1
    )
    assert stale_generation.category == "stale"
    fresh = await preview_exact_target(temp_storage, selection)
    assert fresh.category == "in_progress"
    assert fresh.active_attempt == 2
    # Observations stay read-only: the stale checks mutated nothing.
    row = await temp_storage.get_outbox_item(claimed[0].outbox_id)
    assert row is not None
    assert row.active_attempt == 2
    assert row.attempt_number == 1


async def test_stale_preview_classification_never_authorizes_the_send(
    temp_storage: SQLiteStorage,
) -> None:
    """A drifted target refuses execution even with an admitting preview."""
    targets = (RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_A),)
    router = _router(*targets)
    rows = await _seed_event_with_targets(temp_storage, *targets)
    row_a = rows[_CHANNEL_A]
    selection = _selection(_CHANNEL_A)

    observed = await preview_exact_target(temp_storage, selection)
    assert observed.category == "pending"
    assert observed.attempt_number == 1

    # A second live generation lands between observation and execution: the
    # identity now has contested live generations.
    newer = await _allocate_replay_generation(temp_storage, row_a)
    assert newer.attempt_number == 2
    fresh = await preview_exact_target(temp_storage, selection)
    assert fresh.category == "ambiguous"

    snapshot = await _snapshot_outbox(temp_storage)
    diagnostic = Diagnostician()
    result = await _deliver_result(
        temp_storage,
        _real_pipeline(
            temp_storage,
            router,
            {
                _ADAPTER: FakePresentationAdapter(
                    adapter_id=_ADAPTER, channel=_CHANNEL_A
                )
            },
        ),
        ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[_EVENT],
            run_id=_RUN_ID,
            exact_target_selections=(selection,),
        ),
        diagnostic=diagnostic,
    )

    # Execution accepts no caller-supplied expectation: it re-reads the
    # persisted facts, classifies the contested identity as ambiguous, and
    # refuses to stage a send for it.
    assert result.status == "skipped"
    assert result.error is not None
    assert result.error.startswith("exact_target_refused: ambiguous (")
    assert result.error.count(row_a.delivery_plan_id) == 1
    assert diagnostic.replay_skips == {"exact_target_selection_refused": 1}
    assert await _snapshot_outbox(temp_storage) == snapshot


async def test_cancel_between_admission_and_delivery_never_resurrects(
    temp_storage: SQLiteStorage,
) -> None:
    """A cancel committing after the gate never loses to a stale send."""
    targets = (RouteTarget(adapter=_ADAPTER, channel=_CHANNEL_A),)
    router = _router(*targets)
    rows = await _seed_event_with_targets(temp_storage, *targets)
    row_a = rows[_CHANNEL_A]
    selection = _selection(_CHANNEL_A)
    identity = _identity(_CHANNEL_A)

    pipeline = _real_pipeline(
        temp_storage,
        router,
        {_ADAPTER: FakePresentationAdapter(adapter_id=_ADAPTER, channel=_CHANNEL_A)},
    )
    engine = make_engine(temp_storage, pipeline=pipeline)
    event = await temp_storage.get(_EVENT)
    assert isinstance(event, CanonicalEvent)
    enriched, plans = await pipeline.route_event(event)
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id=_RUN_ID,
        exact_target_selections=(selection,),
    )

    admitted, refusals = await engine._admit_plans_by_exact_selection(plans, request)
    assert [pair[1].plan_id for pair in admitted] == [row_a.delivery_plan_id]
    assert refusals == []

    # The operator cancel commits after the gate verified but before the
    # delivery lifecycle stages anything.
    assert await temp_storage.cancel_unstarted_delivery(
        _cancel_decision(row_a, identity, decision_id="c-window")
    )
    cancelled = await temp_storage.get_outbox_item(row_a.outbox_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    result = await engine._stage_deliver(enriched, admitted, request)

    # The gate inside the deliver stage re-derived the persisted state; the
    # cancelled generation was never resurrected or re-sent. Any replay
    # send is a fresh generation above the terminal one.
    assert result.status == "passed"
    event_rows = await temp_storage.list_outbox_items_for_event(_EVENT)
    by_generation = {row.attempt_number: row for row in event_rows}
    assert by_generation[1].status == "cancelled"
    assert by_generation[1].outbox_id == row_a.outbox_id
    assert by_generation[2].dispatch_source == "replay"
    assert by_generation[2].replay_run_id == _RUN_ID
    assert by_generation[2].status == "sent"
    replay_receipts = [
        receipt
        for receipt in await temp_storage.list_receipts_for_event(_EVENT)
        if receipt.replay_run_id == _RUN_ID
    ]
    assert [receipt.attempt_number for receipt in replay_receipts] == [2]
