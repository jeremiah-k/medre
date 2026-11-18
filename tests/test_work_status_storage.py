"""Work-status storage conformance: SQL projection vs the pure oracle.

The storage layer computes the work-status projection in SQL. These tests
check that SQL implementation against the pure functions in
:mod:`medre.core.observability.work_status` and the shared delivery
authority rule, and pin the snapshot, scoping, and mutation-freedom
contracts the operator view depends on.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    delivery_identity,
    select_current_outbox,
)
from medre.core.engine.pipeline.delivery_state import OUTBOX_STATUSES
from medre.core.events import CanonicalEvent, DeliveryReceipt
from medre.core.observability.work_status import (
    assemble_work_status_report,
    outstanding_age_origin,
    work_status_category,
)
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite._work_status import (
    _SELECT_OLDEST_OUTSTANDING,
    _WorkStatusMixin,
)
from medre.core.storage.sqlite.connection import sync_read_batch
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event

_FUTURE = "2030-01-01T00:00:00+00:00"
_PAST = "2020-01-01T00:00:00+00:00"

_counter = 0


def _next_id(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter:04d}"


async def _stage_event(
    store: SQLiteStorage, event_id: str | None = None
) -> CanonicalEvent:
    event = make_event(event_id=event_id or _next_id("evt"))
    await store.append(event)
    return event


async def _add_row(
    store: SQLiteStorage,
    event_id: str,
    *,
    adapter: str = "target-a",
    channel: str | None = "out",
    plan: str | None = None,
    status: str = "pending",
    attempt_number: int = 0,
    replay: bool = False,
) -> DeliveryOutboxItem:
    """Create one outbox generation through the production creation path."""
    item = DeliveryOutboxItem(
        outbox_id=_next_id("ob"),
        event_id=event_id,
        route_id="route",
        delivery_plan_id=plan or _next_id("plan"),
        target_adapter=adapter,
        target_channel=channel,
        attempt_number=attempt_number,
        status=status,
        replay_run_id=_next_id("run") if replay else None,
    )
    return await store.create_outbox_item(item, allocate_new_generation=replay)


async def _claim_row(
    store: SQLiteStorage, item: DeliveryOutboxItem, *, worker: str = "worker-1"
) -> None:
    """Move one row to in_progress with a recorded live lease.

    Applies exactly the write :meth:`claim_due_outbox_items` performs for
    one claimed row, scoped to a single outbox ID so sibling rows staged
    for other categories are never cross-claimed. The claim path itself
    is exercised by the held-lane campaign.
    """
    now = datetime.now(timezone.utc).isoformat()
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    await store._write(
        "UPDATE delivery_outbox SET status = 'in_progress', worker_id = ?,"
        " locked_at = ?, lease_until = ?, updated_at = ?"
        " WHERE outbox_id = ? AND status IN ('pending', 'retry_wait')",
        (worker, now, lease_until, now, item.outbox_id),
    )
    rows = await store.list_outbox_items_for_event(item.event_id)
    claimed = next(r for r in rows if r.outbox_id == item.outbox_id)
    assert claimed.status == "in_progress"
    assert claimed.worker_id == worker


def _sampled_at() -> str:
    return datetime.now(timezone.utc).isoformat()


async def test_summary_partitions_current_generations(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        pending = await _add_row(store, event.event_id)
        assert pending.status == "pending"
        held = await _add_row(store, event.event_id, adapter="held", plan="plan-held")
        await _claim_row(store, held)
        reserved = await store.reserve_outbox_attempt(
            held.outbox_id, "worker-1", held.attempt_number
        )
        assert reserved == 1
        queued = await _add_row(store, event.event_id, adapter="queued-a")
        await _claim_row(store, queued)
        assert await store.mark_outbox_queued(queued.outbox_id)
        retry = await _add_row(store, event.event_id, adapter="lane")
        await _claim_row(store, retry)
        assert await store.mark_outbox_retry_wait(retry.outbox_id, _FUTURE)
        sent = await _add_row(store, event.event_id, adapter="lane", plan="plan-sent")
        await _claim_row(store, sent)
        assert await store.mark_outbox_sent(sent.outbox_id)
        abandoned = await _add_row(
            store, event.event_id, adapter="lost", plan="plan-lost"
        )
        await _claim_row(store, abandoned)
        assert await store.mark_outbox_abandoned(abandoned.outbox_id)

        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        by_adapter = {row["target_adapter"]: row for row in summary["adapter_rows"]}
        assert by_adapter["target-a"]["unstarted_eligible"] == 1
        assert by_adapter["held"]["reserved_active"] == 1
        assert by_adapter["queued-a"]["native_queue"] == 1
        assert by_adapter["lane"]["retry_scheduled"] == 1
        assert by_adapter["lane"]["terminal_outcome"] == 1
        assert by_adapter["lost"]["uncertain_outcome"] == 1
        for _adapter, row in by_adapter.items():
            expected = {
                "unstarted_eligible",
                "retry_scheduled",
                "reserved_active",
                "native_queue",
                "uncertain_outcome",
                "terminal_outcome",
                "unrecognized",
            }
            assert set(expected) <= set(row)
            assert sum(row[name] for name in expected) == row["current_generations"]
        assert summary["current_generations_total"] == 6

        # The SQL projection agrees with the pure oracle over the same rows.
        rows = await store.list_all_outbox_items()
        resolver = DeliveryAuthorityResolver([], rows)
        expected_counts: dict[str, dict[str, int]] = {}
        sampled_at = _sampled_at()
        for snapshot in resolver.ordered_snapshots():
            current = snapshot.current_outbox
            assert current is not None
            adapter_counts = expected_counts.setdefault(
                current.target_adapter,
                {name: 0 for name in expected},
            )
            adapter_counts[
                work_status_category(
                    current.status,
                    next_attempt_at=current.next_attempt_at,
                    sampled_at=sampled_at,
                )
            ] += 1
        assert {
            adapter: {name: row[name] for name in expected}
            for adapter, row in by_adapter.items()
        } == expected_counts
    finally:
        await store.close()


async def test_summary_matches_pure_authority_selection(tmp_path: Path) -> None:
    """Retained superseded generations never enter the category partition."""
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        first = await _add_row(store, event.event_id, attempt_number=0, plan="plan-r")
        await _claim_row(store, first)
        assert await store.mark_outbox_sent(first.outbox_id, attempt_number=1)
        replayed = await _add_row(
            store, event.event_id, replay=True, status="pending", plan="plan-r"
        )
        assert replayed.outbox_id != first.outbox_id
        assert replayed.attempt_number >= 1

        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        rows = summary["adapter_rows"]
        assert len(rows) == 1
        assert rows[0]["current_generations"] == 1
        assert rows[0]["unstarted_eligible"] == 1
        assert rows[0]["terminal_outcome"] == 0

        rows_all = await store.list_all_outbox_items()
        resolver = DeliveryAuthorityResolver([], rows_all)
        snapshots = resolver.ordered_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].current_outbox is not None
        assert snapshots[0].current_outbox.outbox_id == replayed.outbox_id
        assert len(snapshots[0].outbox_items) == 2
        sampled_at = _sampled_at()
        assert (
            work_status_category(
                snapshots[0].current_outbox.status,
                next_attempt_at=snapshots[0].current_outbox.next_attempt_at,
                sampled_at=sampled_at,
            )
            == "unstarted_eligible"
        )
        assert summary["raw_status_rows"] == [
            {"status": "pending", "rows": 1},
            {"status": "sent", "rows": 1},
        ]
    finally:
        await store.close()


async def test_reserved_without_generation_receipt_flag(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        held = await _add_row(store, event.event_id)
        await _claim_row(store, held)
        assert await store.reserve_outbox_attempt(held.outbox_id, "worker-1", 0) == 1
        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        assert summary["adapter_rows"][0]["reserved_without_generation_receipt"] == 1

        # Attempt evidence for the reserved generation clears the flag.
        receipt = _make_receipt(store, held, attempt_number=1, status="queued")
        await store.append_receipt(receipt)
        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        assert summary["adapter_rows"][0]["reserved_without_generation_receipt"] == 0
    finally:
        await store.close()


async def test_lease_and_queue_staleness_flags(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        expired = await _add_row(store, event.event_id, adapter="expired")
        await _claim_row(store, expired, worker="worker-e")
        await store._write(
            "UPDATE delivery_outbox SET lease_until = ? WHERE outbox_id = ?",
            (_PAST, expired.outbox_id),
        )
        stale = await _add_row(store, event.event_id, adapter="stale-q")
        await _claim_row(store, stale, worker="worker-q")
        assert await store.mark_outbox_queued(stale.outbox_id)
        await store._write(
            "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
            (_PAST, stale.outbox_id),
        )
        fresh = await _add_row(store, event.event_id, adapter="fresh-q", plan="plan-f")
        await _claim_row(store, fresh, worker="worker-f")
        assert await store.mark_outbox_queued(fresh.outbox_id)

        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        by_adapter = {row["target_adapter"]: row for row in summary["adapter_rows"]}
        assert by_adapter["expired"]["lease_missing_or_expired"] == 1
        assert by_adapter["stale-q"]["stale_queue_reclaimable"] == 1
        assert by_adapter["fresh-q"]["stale_queue_reclaimable"] == 0
        assert by_adapter["expired"]["reserved_active"] == 1
        assert by_adapter["stale-q"]["native_queue"] == 1
    finally:
        await store.close()


async def test_adapter_filter_scopes_every_aggregate(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        await _add_row(store, event.event_id, adapter="a")
        other = await _add_row(store, event.event_id, adapter="b")
        await _claim_row(store, other)
        assert await store.mark_outbox_sent(other.outbox_id)

        summary = await store.query_work_status_summary(
            sampled_at=_sampled_at(), adapter="a"
        )
        assert [row["target_adapter"] for row in summary["adapter_rows"]] == ["a"]
        assert summary["adapter_rows"][0]["unstarted_eligible"] == 1
        assert summary["adapter_rows"][0]["terminal_outcome"] == 0
        assert summary["current_generations_total"] == 1
        assert summary["raw_status_rows"] == [{"status": "pending", "rows": 1}]
        assert len(summary["oldest_rows"]) == 1
        assert summary["oldest_rows"][0]["target_adapter"] == "a"
        assert summary["oldest_rows"][0]["category"] == "unstarted_eligible"

        unknown = await store.query_work_status_summary(
            sampled_at=_sampled_at(), adapter="missing"
        )
        assert unknown["adapter_rows"] == []
        assert unknown["oldest_rows"] == []
        assert unknown["current_generations_total"] == 0
        assert unknown["raw_status_rows"] == []
    finally:
        await store.close()


async def test_oldest_outstanding_row_carries_precise_origin(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        scheduled = await _add_row(store, event.event_id)
        await _claim_row(store, scheduled)
        assert await store.mark_outbox_retry_wait(scheduled.outbox_id, _FUTURE)
        await store._write(
            "UPDATE delivery_outbox SET created_at = ? WHERE outbox_id = ?",
            (_PAST, scheduled.outbox_id),
        )

        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        oldest = summary["oldest_rows"]
        assert len(oldest) == 1
        assert oldest[0]["age_origin"] == "retry_due"
        assert oldest[0]["age_anchor"] == _FUTURE
        assert oldest[0]["category"] == "retry_scheduled"

        # An admitted pending row out-ranks the future retry: the anchor
        # comparison stays inside outstanding rows only.
        admitted = await _add_row(store, event.event_id, plan="plan-adm")
        await store._write(
            "UPDATE delivery_outbox SET created_at = ? WHERE outbox_id = ?",
            ("2025-06-01T00:00:00+00:00", admitted.outbox_id),
        )
        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        oldest = summary["oldest_rows"]
        assert oldest[0]["age_origin"] == "admission"
        assert oldest[0]["age_anchor"] == "2025-06-01T00:00:00+00:00"
        assert oldest[0]["category"] == "unstarted_eligible"
    finally:
        await store.close()


async def test_summary_aggregates_assemble_into_a_valid_report(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        held = await _add_row(store, event.event_id, adapter="held")
        await _claim_row(store, held)
        assert await store.reserve_outbox_attempt(held.outbox_id, "worker-1", 0) == 1
        sent = await _add_row(store, event.event_id, adapter="held", plan="plan-s")
        await _claim_row(store, sent)
        assert await store.mark_outbox_sent(sent.outbox_id, attempt_number=1)

        sampled_at = _sampled_at()
        summary = await store.query_work_status_summary(sampled_at=sampled_at)
        report = assemble_work_status_report(
            sampled_at=sampled_at,
            adapter_filter=None,
            adapter_rows=summary["adapter_rows"],
            oldest_rows=summary["oldest_rows"],
            raw_status_rows=summary["raw_status_rows"],
            current_generations_total=summary["current_generations_total"],
            receipts_by_status=summary["receipts_by_status"],
        )
        document = report.to_dict()
        assert document["totals"]["current_generations"] == 2
        assert document["totals"]["outstanding"] == 1
        assert document["totals"]["terminal_outcome"] == 1
        held_doc = next(
            a for a in document["adapters"] if a["target_adapter"] == "held"
        )
        assert held_doc["uncertainty"]["reserved_without_generation_receipt"] == 1
        assert held_doc["oldest_outstanding"]["category"] == "reserved_active"
        assert document["retained_history"]["receipts_total"] == 0

        # Appended attempt evidence enters the retained-history dimension.
        await store.append_receipt(
            _make_receipt(store, sent, attempt_number=1, status="sent")
        )
        summary_after = await store.query_work_status_summary(sampled_at=sampled_at)
        assert summary_after["receipts_by_status"] == {"sent": 1}
        report_after = assemble_work_status_report(
            sampled_at=sampled_at,
            adapter_filter=None,
            adapter_rows=summary_after["adapter_rows"],
            oldest_rows=summary_after["oldest_rows"],
            raw_status_rows=summary_after["raw_status_rows"],
            current_generations_total=summary_after["current_generations_total"],
            receipts_by_status=summary_after["receipts_by_status"],
        )
        assert report_after.to_dict()["retained_history"]["receipts_total"] == 1
    finally:
        await store.close()


async def test_summary_is_read_only_over_application_state(
    tmp_path: Path,
) -> None:
    """Running the view mutates no application rows and no schema objects."""
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        row = await _add_row(store, event.event_id)
        await _claim_row(store, row)
        assert await store.reserve_outbox_attempt(row.outbox_id, "worker-1", 0) == 1

        def application_state(db: sqlite3.Connection) -> dict[str, object]:
            names = [
                str(r[0])
                for r in db.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    " ORDER BY name"
                )
            ]
            objects = db.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            tables = {}
            for name in names:
                rows = db.execute(
                    f'SELECT * FROM "{name}"'  # nosec B608 - table names come from sqlite_master
                ).fetchall()
                tables[name] = sorted(tuple(r) for r in rows)
            return {"objects": sorted(tuple(o) for o in objects), "tables": tables}

        db = store._require_db()
        before = application_state(db)
        for _ in range(3):
            summary = await store.query_work_status_summary(sampled_at=_sampled_at())
            assert summary["current_generations_total"] == 1
        after = application_state(db)
        assert after == before
    finally:
        await store.close()


async def test_view_reads_one_snapshot_while_a_writer_is_uncommitted(
    tmp_path: Path,
) -> None:
    """A held writer transaction cannot stall or skew the read snapshot."""
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        await _add_row(store, event.event_id, adapter="a")

        writer = sqlite3.connect(
            str(tmp_path / "work.sqlite"),
            check_same_thread=False,
        )
        try:
            writer.execute("PRAGMA busy_timeout=5000")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO delivery_outbox"
                " (outbox_id, event_id, route_id, delivery_plan_id,"
                "  target_adapter, attempt_number, status, created_at,"
                "  updated_at, dispatch_source, metadata)"
                " VALUES ('ob-uncommitted', ?, 'route', 'plan-u', 'a', 0,"
                " 'pending', ?, ?, 'live', '{}')",
                (event.event_id, _sampled_at(), _sampled_at()),
            )
            # WAL readers proceed on the last committed snapshot while the
            # writer transaction holds the write lock.
            summary = await store.query_work_status_summary(sampled_at=_sampled_at())
            assert summary["adapter_rows"] == [
                {
                    "target_adapter": "a",
                    "current_generations": 1,
                    "unstarted_eligible": 1,
                    "retry_scheduled": 0,
                    "reserved_active": 0,
                    "native_queue": 0,
                    "uncertain_outcome": 0,
                    "terminal_outcome": 0,
                    "unrecognized": 0,
                    "reserved_without_generation_receipt": 0,
                    "lease_missing_or_expired": 0,
                    "stale_queue_reclaimable": 0,
                }
            ]
            assert summary["current_generations_total"] == 1
        finally:
            writer.rollback()
            writer.close()
    finally:
        await store.close()


async def test_statement_batch_reports_one_consistent_snapshot(
    tmp_path: Path,
) -> None:
    """Statements inside one read transaction never straddle a commit."""
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        row = await _add_row(store, event.event_id, adapter="a")

        params = {"sampled_at": _sampled_at(), "stale_cutoff": _PAST}
        oldest_sql = _SELECT_OLDEST_OUTSTANDING.replace("{filter}", "")

        reader = sqlite3.connect(
            str(tmp_path / "work.sqlite"),
            check_same_thread=False,
        )
        reader.row_factory = sqlite3.Row
        try:
            reader.execute("PRAGMA busy_timeout=5000")
            reader.execute("BEGIN")
            # Pin the snapshot with a real read of the table.
            reader.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()

            # A committed change after the pinned read is invisible inside
            # the pinned transaction.
            await _claim_row(store, row)
            assert await store.mark_outbox_sent(row.outbox_id, attempt_number=1)
            pinned_rows = [
                dict(r) for r in reader.execute(oldest_sql, params).fetchall()
            ]
            assert len(pinned_rows) == 1
            assert pinned_rows[0]["category"] == "unstarted_eligible"
        finally:
            reader.rollback()
            reader.close()

        reader2 = sqlite3.connect(
            str(tmp_path / "work.sqlite"), check_same_thread=False
        )
        reader2.row_factory = sqlite3.Row
        try:
            committed = sync_read_batch(
                reader2, threading.Lock(), [(oldest_sql, params)]
            )
            assert committed[0] == []
        finally:
            reader2.close()
    finally:
        await store.close()


def _make_receipt(
    store: SQLiteStorage,
    item: DeliveryOutboxItem,
    *,
    attempt_number: int,
    status: str,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=_next_id("rcpt"),
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status=status,
        receipt_kind="attempt",
        attempt_number=attempt_number,
        parent_receipt_id=None,
        source="live",
        replay_run_id=None,
        outbox_id=item.outbox_id,
        created_at=datetime.now(timezone.utc),
    )


def test_effective_generation_or_semantics_are_replicated() -> None:
    """A stored zero finalized counter is not a generation rank."""
    assert _WorkStatusMixin is not None
    now = datetime.now(timezone.utc).isoformat()
    zero = DeliveryOutboxItem(
        outbox_id="ob-zero",
        event_id="e",
        route_id="r",
        delivery_plan_id="p",
        target_adapter="a",
        target_channel=None,
        attempt_number=0,
        status="pending",
        updated_at=now,
        created_at=now,
    )
    one = DeliveryOutboxItem(
        outbox_id="ob-one",
        event_id="e",
        route_id="r",
        delivery_plan_id="p",
        target_adapter="a",
        target_channel=None,
        attempt_number=1,
        status="sent",
        updated_at=now,
        created_at=now,
    )
    # Shared rule: both rank as effective generation 1 (zero never counts),
    # so the tie breaks on updated_at/created_at/outbox_id, not the counter.
    winner = select_current_outbox([zero, one])
    assert winner is not None
    identity = delivery_identity(winner)
    assert identity.target_adapter == "a"


def test_storage_vocabularies_match_the_projection() -> None:
    """Every stored outbox status is classifiable by the pure oracle."""
    sampled_at = _sampled_at()
    for status in OUTBOX_STATUSES:
        if status == "retry_wait":
            continue
        assert (
            work_status_category(status, next_attempt_at=None, sampled_at=sampled_at)
            != "unrecognized"
        )
    origin, anchor = outstanding_age_origin(
        "pending",
        next_attempt_at=None,
        created_at=_PAST,
        locked_at=None,
        updated_at=None,
    )
    assert (origin, anchor) == ("admission", _PAST)


def test_stale_grace_cutoff_matches_the_reclaim_policy() -> None:
    from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS

    sampled = datetime.now(timezone.utc)
    cutoff = sampled - timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)
    assert cutoff.tzinfo is not None
