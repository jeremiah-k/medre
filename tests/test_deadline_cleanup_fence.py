"""The strictly fenced deadline cleanup transition and native-submit fence.

Covers the storage-level retirement of provably unstarted, unclaimed outbox
work past its effective earlier-of deadline: the dedicated guarded terminal
transition under ``BEGIN IMMEDIATE`` with compare-and-set revalidation, its
refusal to touch reserved, claimed, in-progress-without-reservation, queued
or superseded rows, its idempotence, its serialization with a concurrent
claimant, and the native-submit fence that refuses submissions past the
effective deadline without producing receipts or new generations.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite._deadline_cleanup import (
    DEADLINE_RETIREMENT_FAILURE_KIND,
    DEADLINE_RETIREMENT_STATUS,
    DEADLINE_RETIREMENT_SUMMARY,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _direct_snapshot(deadline: str | None) -> OutboxDispatchSnapshotWrite:
    """Freeze the reduced direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": "native",
            "capability_field": None,
            "capability_reason": None,
            "deadline": deadline,
        },
    )


async def _stage(
    store: SQLiteStorage,
    event_id: str,
    *,
    plan_deadline: datetime | None = None,
    delivery_plan_id: str = "plan-1",
) -> str:
    """Create one pending direct generation and return its outbox id."""
    item = make_outbox_item(event_id=event_id, delivery_plan_id=delivery_plan_id)
    created = await store.create_outbox_item(
        item,
        dispatch_snapshot=_direct_snapshot(
            plan_deadline.isoformat() if plan_deadline is not None else None
        ),
    )
    return created.outbox_id


async def _claim(
    store: SQLiteStorage, outbox_id: str, worker_id: str, lease_seconds: int = 7200
) -> None:
    claimed = await store.claim_due_outbox_items(
        now=datetime.now(timezone.utc).isoformat(),
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        limit=10,
    )
    assert [item.outbox_id for item in claimed] == [outbox_id]


async def _diagnostics(store: SQLiteStorage, outbox_id: str) -> list[dict]:
    return await store._read_all(
        "SELECT field, attempt, diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family='outbox' AND row_id=? ORDER BY diag_id",
        (outbox_id,),
    )


# ---------------------------------------------------------------------------
# The cleanup transition retires expired, provably unstarted work
# ---------------------------------------------------------------------------


async def test_expired_unclaimed_pending_work_is_retired_with_audit_evidence(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-expired.db"))
    await store.initialize()
    try:
        ceiling = _future(60)
        await store.admit_ingress(
            make_storage_event(event_id="sweep-expired"),
            None,
            "live",
            content_deadline=ceiling,
        )
        outbox_id = await _stage(store, "sweep-expired")

        retired = await store.retire_expired_unstarted_outbox(now=_future(120))

        assert [row.outbox_id for row in retired] == [outbox_id]
        assert retired[0].prior_status == "pending"
        assert retired[0].attempt_number == 1
        item = await store.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == DEADLINE_RETIREMENT_STATUS
        assert item.failure_kind == DEADLINE_RETIREMENT_FAILURE_KIND
        assert item.next_attempt_at is None
        assert item.worker_id is None and item.lease_until is None
        # The deadline refusal summary is durable audit evidence through the
        # existing outbox diagnostics side table, keyed to the generation.
        assert await _diagnostics(store, outbox_id) == [
            {
                "field": "error_summary",
                "attempt": 1,
                "diagnostic": DEADLINE_RETIREMENT_SUMMARY,
            }
        ]
        # The guarded terminal settlement released the row's own target
        # dependency and retired its frozen dispatch context in the same
        # transaction; the terminal row itself is immutable history.
        dependencies = await store.list_mandatory_content_dependencies("sweep-expired")
        target = [row for row in dependencies if row[0] == "outbox"]
        assert target and target[0][3] is True
        snapshot = await store._read_one(
            "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (outbox_id,),
        )
        assert snapshot is None
    finally:
        await store.close()


async def test_not_yet_expired_work_is_untouched(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-live.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-live"),
            None,
            "live",
            content_deadline=_future(600),
        )
        outbox_id = await _stage(store, "sweep-live")

        assert await store.retire_expired_unstarted_outbox(now=_future(300)) == []

        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == "pending"
        assert await _diagnostics(store, outbox_id) == []
    finally:
        await store.close()


async def test_work_without_any_deadline_stays_dormant(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-dormant.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-dormant"), None, "live"
        )
        outbox_id = await _stage(store, "sweep-dormant")

        assert await store.retire_expired_unstarted_outbox(now=_future(999_999)) == []

        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == "pending"
    finally:
        await store.close()


async def test_expired_retry_wait_work_is_retired(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-retry.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-retry"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-retry")
        await _claim(store, outbox_id, "retry:worker")
        committed = await store.mark_outbox_retry_wait(
            outbox_id, next_attempt_at=_future(30).isoformat()
        )
        assert committed

        retired = await store.retire_expired_unstarted_outbox(now=_future(120))

        assert [row.outbox_id for row in retired] == [outbox_id]
        assert retired[0].prior_status == "retry_wait"
        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == DEADLINE_RETIREMENT_STATUS
    finally:
        await store.close()


async def test_an_obligation_row_staged_after_its_plan_deadline_is_retired(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-obligation.db"))
    await store.initialize()
    try:
        # The ceiling stays live; the frozen plan deadline already elapsed at
        # staging, so the generation adopted no content dependency and can
        # never dispatch. The sweep must retire it from the snapshot bound.
        await store.admit_ingress(
            make_storage_event(event_id="sweep-obligation"),
            None,
            "live",
            content_deadline=_future(600),
        )
        outbox_id = await _stage(store, "sweep-obligation", plan_deadline=_future(-60))

        dependencies = await store.list_mandatory_content_dependencies(
            "sweep-obligation"
        )
        assert [row[0] for row in dependencies] == ["ingress"]

        retired = await store.retire_expired_unstarted_outbox(now=_future(1))

        assert [row.outbox_id for row in retired] == [outbox_id]
        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == DEADLINE_RETIREMENT_STATUS
    finally:
        await store.close()


async def test_sweep_pages_past_older_ineligible_work_with_limit_one(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-page.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="no-deadline"), None, "live"
        )
        older = await _stage(store, "no-deadline")
        await store.admit_ingress(
            make_storage_event(event_id="past-deadline"),
            None,
            "live",
            content_deadline=_future(30),
        )
        expired = await _stage(store, "past-deadline")
        # Make the order independent of same-second UUID allocation.
        await store._write_rowcount(
            "UPDATE delivery_outbox SET created_at=? WHERE outbox_id=?",
            ("2001-01-01T00:00:00+00:00", older),
        )
        retired = await store.retire_expired_unstarted_outbox(now=_future(90), limit=1)
        assert [row.outbox_id for row in retired] == [expired]
        untouched = await store.get_outbox_item(older)
        assert untouched is not None and untouched.status == "pending"
    finally:
        await store.close()


async def test_historical_inline_plan_deadline_fences_cleanup_and_native_start(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-inline.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="inline-plan"),
            None,
            "live",
            content_deadline=_future(3600),
        )
        item = make_outbox_item(event_id="inline-plan")
        item.metadata = {"plan": {"deadline": _future(30).isoformat()}}
        created = await store.create_outbox_item(item)
        assert (
            await store._read_one(
                "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id=?",
                (created.outbox_id,),
            )
            is None
        )
        await _claim(store, created.outbox_id, "pipeline:inline")
        assert not await store.authorize_native_submission(
            "inline-plan",
            created.outbox_id,
            worker_id="pipeline:inline",
            attempt_number=1,
            now=_future(60),
        )
        assert await store.mark_outbox_retry_wait(
            created.outbox_id, next_attempt_at=_future(30).isoformat()
        )
        retired = await store.retire_expired_unstarted_outbox(now=_future(60))
        assert [row.outbox_id for row in retired] == [created.outbox_id]
    finally:
        await store.close()


async def test_retirement_clears_prior_failure_detail_for_same_attempt(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-clear.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-clear"),
            None,
            "live",
            content_deadline=_future(30),
        )
        outbox_id = await _stage(store, "sweep-clear")
        await _claim(store, outbox_id, "retry:clear")
        assert await store.mark_outbox_retry_wait(
            outbox_id,
            next_attempt_at=_future(20).isoformat(),
            failure_kind_detail="prior transient failure",
            error_summary="prior diagnostic",
        )
        retired = await store.retire_expired_unstarted_outbox(now=_future(90))
        assert len(retired) == 1
        settled = await store.get_outbox_item(outbox_id)
        assert settled is not None
        assert settled.failure_kind_detail is None
        assert settled.error_summary == DEADLINE_RETIREMENT_SUMMARY
        diagnostics = await _diagnostics(store, outbox_id)
        assert [row["diagnostic"] for row in diagnostics] == [
            "prior transient failure",
            "prior diagnostic",
            "",
            DEADLINE_RETIREMENT_SUMMARY,
        ]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# The earlier-of effective deadline, both directions
# ---------------------------------------------------------------------------


async def test_sweep_retires_when_the_plan_deadline_is_earlier(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-plan-early.db"))
    await store.initialize()
    try:
        plan = _future(60)
        await store.admit_ingress(
            make_storage_event(event_id="plan-early"),
            None,
            "live",
            content_deadline=_future(600),
        )
        outbox_id = await _stage(store, "plan-early", plan_deadline=plan)

        retired = await store.retire_expired_unstarted_outbox(now=_future(120))

        assert [row.outbox_id for row in retired] == [outbox_id]
        assert retired[0].effective_deadline == _normalize(plan)
    finally:
        await store.close()


async def test_sweep_retires_when_the_event_ceiling_is_earlier(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-ceiling-early.db"))
    await store.initialize()
    try:
        ceiling = _future(60)
        await store.admit_ingress(
            make_storage_event(event_id="ceiling-early"),
            None,
            "live",
            content_deadline=ceiling,
        )
        outbox_id = await _stage(store, "ceiling-early", plan_deadline=_future(3600))

        retired = await store.retire_expired_unstarted_outbox(now=_future(120))

        assert [row.outbox_id for row in retired] == [outbox_id]
        assert retired[0].effective_deadline == _normalize(ceiling)
    finally:
        await store.close()


def _normalize(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Rows the fence must never retire
# ---------------------------------------------------------------------------


async def test_reserved_attempt_is_never_retired(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-reserved.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-reserved"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-reserved")
        await _claim(store, outbox_id, "retry:worker")
        reserved = await store.reserve_outbox_attempt(outbox_id, "retry:worker", 1)
        assert reserved == 2

        assert await store.retire_expired_unstarted_outbox(now=_future(120)) == []

        item = await store.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == "in_progress"
        assert item.active_attempt == 2
    finally:
        await store.close()


async def test_claimed_in_progress_without_reservation_is_never_retired(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-claimed.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-claimed"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-claimed")
        # The hard case: a claimed row may hold an authorized first-dispatch
        # native send in flight between preflight authorization and the
        # transport call, without any active_attempt reservation.
        await _claim(store, outbox_id, "pipeline:main")

        assert await store.retire_expired_unstarted_outbox(now=_future(120)) == []

        item = await store.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == "in_progress"
        assert item.active_attempt is None
        assert item.worker_id == "pipeline:main"
    finally:
        await store.close()


async def test_queued_work_is_never_retired(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-queued.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-queued"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-queued")
        await _claim(store, outbox_id, "pipeline:main")
        committed = await store.mark_outbox_queued(outbox_id)
        assert committed

        assert await store.retire_expired_unstarted_outbox(now=_future(120)) == []

        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == "queued"
    finally:
        await store.close()


async def test_superseded_generation_is_never_retired(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-superseded.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-superseded"),
            None,
            "live",
            content_deadline=_future(600),
        )
        old_id = await _stage(store, "sweep-superseded", plan_deadline=_future(-60))
        fresh = make_outbox_item(event_id="sweep-superseded")
        fresh.replay_run_id = "run-superseded"
        created = await store.create_outbox_item(
            fresh,
            allocate_new_generation=True,
            dispatch_snapshot=_direct_snapshot(_future(600).isoformat()),
        )
        assert created.attempt_number == 2

        # The expired older generation is durable history superseded by the
        # newer authoritative generation; the fresh generation is not yet
        # past its own effective deadline, so neither may be retired.
        assert await store.retire_expired_unstarted_outbox(now=_future(1)) == []

        old = await store.get_outbox_item(old_id)
        fresh_item = await store.get_outbox_item(created.outbox_id)
        assert old is not None and old.status == "pending"
        assert fresh_item is not None and fresh_item.status == "pending"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Idempotence and serialization with the claim path
# ---------------------------------------------------------------------------


async def test_repeat_sweep_is_an_idempotent_no_op(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-idempotent.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-idempotent"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-idempotent")

        first = await store.retire_expired_unstarted_outbox(now=_future(120))
        second = await store.retire_expired_unstarted_outbox(now=_future(240))

        assert [row.outbox_id for row in first] == [outbox_id]
        assert second == []
        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == DEADLINE_RETIREMENT_STATUS
        # No second refusal summary and no rewritten timestamp: the terminal
        # row is immutable history.
        assert len(await _diagnostics(store, outbox_id)) == 1
        assert item.updated_at == first[0].retired_at
    finally:
        await store.close()


async def test_cleanup_serializes_with_a_concurrent_claim_under_begin_immediate(
    tmp_path,
) -> None:
    path = str(tmp_path / "sweep-race.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-race"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-race")

        # A second SQLite handle takes the write lock and claims the row
        # inside its own BEGIN IMMEDIATE transaction before committing.
        claimant = sqlite3.connect(path)
        claimant.row_factory = sqlite3.Row
        try:
            claimant.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            claimant.execute(
                "UPDATE delivery_outbox SET status='in_progress', "
                "worker_id='claimant', locked_at=?, lease_until=?, updated_at=?, "
                "next_attempt_at=NULL WHERE outbox_id=?",
                (
                    now,
                    (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                    now,
                    outbox_id,
                ),
            )
            sweep = asyncio.create_task(
                store.retire_expired_unstarted_outbox(now=_future(120))
            )
            for _ in range(10):
                await asyncio.sleep(0)
            # The sweep cannot enter its transaction while the claimant holds
            # the write lock, so nothing is retired from stale evidence.
            assert not sweep.done()
            claimant.execute("COMMIT")
            assert await sweep == []

            item = await store.get_outbox_item(outbox_id)
            assert item is not None
            assert item.status == "in_progress"
            assert item.worker_id == "claimant"
        finally:
            claimant.close()
    finally:
        await store.close()


async def test_claimant_gets_nothing_after_the_fence_retired_the_row(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-race-reverse.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="sweep-reverse"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "sweep-reverse")

        retired = await store.retire_expired_unstarted_outbox(now=_future(120))
        claimed = await store.claim_due_outbox_items(
            now=datetime.now(timezone.utc).isoformat(),
            worker_id="late-claimant",
            limit=10,
        )

        assert [row.outbox_id for row in retired] == [outbox_id]
        assert claimed == []
        item = await store.get_outbox_item(outbox_id)
        assert item is not None and item.status == DEADLINE_RETIREMENT_STATUS
    finally:
        await store.close()


async def test_sweep_argument_validation(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "sweep-args.db"))
    await store.initialize()
    try:
        for limit in (0, -1, 501, True, "20"):  # type: ignore[arg-type]
            try:
                await store.retire_expired_unstarted_outbox(limit=limit)
            except ValueError:
                pass
            else:
                raise AssertionError(f"limit {limit!r} must be refused")
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            await store.retire_expired_unstarted_outbox(now=naive)
        except ValueError:
            pass
        else:
            raise AssertionError("a naive cleanup clock must be refused")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# The native-submit fence
# ---------------------------------------------------------------------------


async def _count(store: SQLiteStorage, table: str, event_id: str) -> int:
    row = await store._read_one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE event_id=?",  # nosec B608
        (event_id,),
    )
    assert row is not None
    return int(row["n"])


async def test_submit_past_the_event_ceiling_is_refused_without_evidence(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "fence-ceiling.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="fence-ceiling"),
            None,
            "live",
            content_deadline=_future(60),
        )
        outbox_id = await _stage(store, "fence-ceiling")
        await _claim(store, outbox_id, "pipeline:main")

        authorized = await store.authorize_native_submission(
            "fence-ceiling",
            outbox_id,
            worker_id="pipeline:main",
            attempt_number=1,
            now=_future(120),
        )

        assert authorized is False
        # Fail-closed refusal only: no receipt, no observation, no native
        # reference, and no new generation was invented.
        assert await _count(store, "delivery_receipts", "fence-ceiling") == 0
        assert await _count(store, "delivery_observations", "fence-ceiling") == 0
        assert await _count(store, "native_message_refs", "fence-ceiling") == 0
        rows = await store.list_outbox_items_for_event("fence-ceiling")
        assert len(rows) == 1
        assert rows[0].status == "in_progress"
        assert rows[0].attempt_number == 1
    finally:
        await store.close()


async def test_submit_past_the_frozen_plan_deadline_is_refused_for_replay(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "fence-replay.db"))
    await store.initialize()
    try:
        # A replay generation adopts no target dependency and dispatches on
        # the still-live admission hold, whose deadline is the later event
        # ceiling. Only the frozen plan deadline leg can refuse this submit.
        await store.admit_ingress(
            make_storage_event(event_id="fence-replay"),
            None,
            "live",
            content_deadline=_future(3600),
        )
        replay = make_outbox_item(event_id="fence-replay")
        replay.replay_run_id = "run-fence"
        created = await store.create_outbox_item(
            replay,
            allocate_new_generation=True,
            dispatch_snapshot=_direct_snapshot(_future(60).isoformat()),
        )
        await _claim(store, created.outbox_id, "pipeline:replay")

        before_deadline = await store.authorize_native_submission(
            "fence-replay",
            created.outbox_id,
            worker_id="pipeline:replay",
            attempt_number=1,
            now=_future(30),
        )
        past_deadline = await store.authorize_native_submission(
            "fence-replay",
            created.outbox_id,
            worker_id="pipeline:replay",
            attempt_number=1,
            now=_future(120),
        )

        assert before_deadline is True
        assert past_deadline is False
        assert await _count(store, "delivery_receipts", "fence-replay") == 0
    finally:
        await store.close()


async def test_submit_within_a_live_deadline_stays_authorized(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "fence-live.db"))
    await store.initialize()
    try:
        # Default-off parity: with no ceiling and no frozen plan deadline the
        # fence contributes no bound and the claimed first dispatch proceeds.
        await store.admit_ingress(
            make_storage_event(event_id="fence-live"), None, "live"
        )
        outbox_id = await _stage(store, "fence-live")
        await _claim(store, outbox_id, "pipeline:main")

        authorized = await store.authorize_native_submission(
            "fence-live",
            outbox_id,
            worker_id="pipeline:main",
            attempt_number=1,
            now=_future(30),
        )

        assert authorized is True
    finally:
        await store.close()


async def test_malformed_frozen_deadline_refuses_the_submit(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "fence-malformed.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="fence-malformed"), None, "live"
        )
        outbox_id = await _stage(store, "fence-malformed")
        # Malformed frozen deadlines cannot enter storage through staging;
        # the fence still refuses fail-closed instead of treating the value
        # as an absent bound.
        await store._write(
            "UPDATE delivery_outbox_snapshots SET context_json=? WHERE outbox_id=?",
            ('{"deadline": 1234}', outbox_id),
        )
        await _claim(store, outbox_id, "pipeline:main")

        authorized = await store.authorize_native_submission(
            "fence-malformed",
            outbox_id,
            worker_id="pipeline:main",
            attempt_number=1,
            now=_future(30),
        )

        assert authorized is False
    finally:
        await store.close()
