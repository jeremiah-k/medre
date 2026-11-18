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
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from msgspec.structs import replace

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
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
from medre.core.storage.sqlite.connection import sync_read_batch
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event

_FUTURE = "2030-01-01T00:00:00+00:00"
_PAST = "2020-01-01T00:00:00+00:00"
_FIXED_SAMPLE = "2026-01-02T00:00:00+00:00"

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

        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
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
        sampled_at = _FIXED_SAMPLE
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
        assert await store.mark_outbox_sent(first.outbox_id, attempt_number=2)
        replayed = await _add_row(
            store, event.event_id, replay=True, status="pending", plan="plan-r"
        )
        assert replayed.outbox_id != first.outbox_id
        assert replayed.attempt_number > 2

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

        # A foreign-plan receipt with this attempt key is not evidence for
        # the selected identity. A valid receipt must match the full scope.
        receipt = _make_receipt(store, held, attempt_number=1, status="queued")
        await store.append_receipt(
            replace(receipt, receipt_id=_next_id("rcpt"), delivery_plan_id="other-plan")
        )
        await store.append_receipt(
            replace(
                receipt,
                receipt_id=_next_id("rcpt"),
                status="abandoned",
                receipt_kind="lifecycle",
            )
        )
        summary = await store.query_work_status_summary(sampled_at=_sampled_at())
        assert summary["adapter_rows"][0]["reserved_without_generation_receipt"] == 1
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
        await store._write(
            "UPDATE delivery_outbox SET updated_at=? WHERE outbox_id=?",
            (_FIXED_SAMPLE, fresh.outbox_id),
        )

        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
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
        first = await _add_row(store, event.event_id, adapter="a")
        other = await _add_row(store, event.event_id, adapter="b")
        await _claim_row(store, other)
        assert await store.mark_outbox_sent(other.outbox_id)
        await store.append_receipt(
            _make_receipt(store, first, attempt_number=1, status="queued")
        )
        await store.append_receipt(
            _make_receipt(store, other, attempt_number=1, status="sent")
        )

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
        assert summary["receipts_by_status"] == {"queued": 1}
        with pytest.raises(ValueError, match="adapter filter"):
            await store.query_work_status_summary(adapter="")

        unknown = await store.query_work_status_summary(
            sampled_at=_sampled_at(), adapter="missing"
        )
        assert unknown["adapter_rows"] == []
        assert unknown["oldest_rows"] == []
        assert unknown["current_generations_total"] == 0
        assert unknown["raw_status_rows"] == []
        assert unknown["receipts_by_status"] == {}
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

        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
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
        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
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
        writes = []

        def observe(action, *_args):
            if action not in (
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_RECURSIVE,
            ):
                writes.append(action)
            return sqlite3.SQLITE_OK

        db.set_authorizer(observe)
        try:
            for _ in range(3):
                summary = await store.query_work_status_summary(
                    sampled_at=_sampled_at()
                )
                assert summary["current_generations_total"] == 1
        finally:
            db.set_authorizer(None)
        assert writes == []
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
                    "queue_staleness_unknown": 0,
                }
            ]
            assert summary["current_generations_total"] == 1
        finally:
            writer.rollback()
            writer.close()
    finally:
        await store.close()


async def test_statement_batch_reports_one_consistent_snapshot(tmp_path: Path) -> None:
    db_path = str(tmp_path / "work.sqlite")
    store = SQLiteStorage(db_path)
    try:
        await store.initialize()
        event = await _stage_event(store)
        row = await _add_row(store, event.event_id, adapter="a")
        reader = await SQLiteStorage.open_readonly(db_path)
        committed = []

        def interleave(sql: str) -> None:
            if "oldest AS" not in sql or committed:
                return
            # The prior aggregate has completed. Commit through an independent
            # connection between production batch statements, not a sleep.
            with closing(sqlite3.connect(db_path)) as writer:
                writer.execute(
                    "UPDATE delivery_outbox SET status='sent', attempt_number=1"
                    " WHERE outbox_id=?",
                    (row.outbox_id,),
                )
                writer.execute(
                    "INSERT INTO delivery_outbox (outbox_id,event_id,route_id,delivery_plan_id,target_adapter,attempt_number,status,created_at,updated_at,dispatch_source,metadata) VALUES ('ob-interleaved',?,'route','plan-interleaved','b',0,'pending',?,?,'live','{}')",
                    (event.event_id, _FIXED_SAMPLE, _FIXED_SAMPLE),
                )
                writer.execute(
                    "INSERT INTO delivery_receipts (receipt_id,event_id,delivery_plan_id,target_adapter,route_id,status,attempt_number,created_at,receipt_kind,source,outbox_id) VALUES ('receipt-interleaved',?,?,'a','route','sent',1,?,'attempt','live',?)",
                    (
                        event.event_id,
                        row.delivery_plan_id,
                        _FIXED_SAMPLE,
                        row.outbox_id,
                    ),
                )
                writer.commit()
            committed.append(True)

        try:
            reader._require_db().set_trace_callback(interleave)
            snapshot = await reader.query_work_status_summary()
            assert committed == [True]
            assert snapshot["adapter_rows"][0]["unstarted_eligible"] == 1
            assert snapshot["oldest_rows"][0]["outbox_id"] == row.outbox_id
            assert snapshot["raw_status_rows"] == [{"status": "pending", "rows": 1}]
            assert snapshot["current_generations_total"] == 1
            assert snapshot["receipts_by_status"] == {}
            reader._require_db().set_trace_callback(None)
            fresh = await reader.query_work_status_summary()
            assert fresh["adapter_rows"][0]["terminal_outcome"] == 1
            assert fresh["current_generations_total"] == 2
            assert fresh["receipts_by_status"] == {"sent": 1}
            assert fresh["oldest_rows"][0]["outbox_id"] == "ob-interleaved"
        finally:
            reader._require_db().set_trace_callback(None)
            await reader.close()
    finally:
        await store.close()


def test_read_batch_does_not_rollback_foreign_transaction():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE t(value TEXT)")
        db.execute("INSERT INTO t VALUES ('uncommitted')")
        with pytest.raises(sqlite3.OperationalError, match="idle connection"):
            sync_read_batch(db, threading.Lock(), [("SELECT * FROM t", ())])
        assert db.in_transaction
        assert db.execute("SELECT value FROM t").fetchone()[0] == "uncommitted"
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        db.close()


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


async def test_effective_generation_or_semantics_are_replicated(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        for outbox_id, attempt, status in (
            ("ob-one", 1, "sent"),
            ("ob-zero", 0, "pending"),
        ):
            await store._write(
                "INSERT INTO delivery_outbox (outbox_id,event_id,route_id,delivery_plan_id,"
                "target_adapter,attempt_number,status,created_at,updated_at,dispatch_source,metadata)"
                " VALUES (?,?, 'route','p','a',?,?,?,?, 'live','{}')",
                (
                    outbox_id,
                    event.event_id,
                    attempt,
                    status,
                    _FIXED_SAMPLE,
                    _FIXED_SAMPLE,
                ),
            )
        rows = await store.list_outbox_items_for_event(event.event_id)
        winner = select_current_outbox(rows)
        assert winner is not None and winner.outbox_id == "ob-zero"
        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
        assert summary["adapter_rows"][0]["unstarted_eligible"] == 1
        assert summary["oldest_rows"][0]["outbox_id"] == "ob-zero"
    finally:
        await store.close()


async def test_equal_generation_mixed_offsets_share_authority(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        for outbox_id, attempt, status, stamp in (
            ("ob-later-instant", 1, "sent", "2026-01-02T09:30:00+00:00"),
            ("ob-later-text", 0, "pending", "2026-01-02T10:00:00+02:00"),
        ):
            await store._write(
                "INSERT INTO delivery_outbox (outbox_id,event_id,route_id,delivery_plan_id,"
                "target_adapter,attempt_number,status,created_at,updated_at,dispatch_source,metadata)"
                " VALUES (?,?, 'route','p','a',?,?,?,?, 'live','{}')",
                (outbox_id, event.event_id, attempt, status, stamp, stamp),
            )
        winner = select_current_outbox(
            await store.list_outbox_items_for_event(event.event_id)
        )
        assert winner is not None and winner.outbox_id == "ob-later-text"
        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
        assert summary["oldest_rows"][0]["outbox_id"] == winner.outbox_id
    finally:
        await store.close()


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


async def test_stale_grace_cutoff_matches_the_reclaim_policy(tmp_path: Path) -> None:
    from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS

    sampled = datetime.fromisoformat(_FIXED_SAMPLE)
    cutoff = sampled - timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        ids = {}
        for offset in (-1, 0, 1):
            name = f"lane-{offset}"
            row = await _add_row(store, event.event_id, adapter=name)
            await _claim_row(store, row)
            assert await store.mark_outbox_queued(row.outbox_id)
            await store._write(
                "UPDATE delivery_outbox SET updated_at=?, lease_until=NULL WHERE outbox_id=?",
                ((cutoff + timedelta(microseconds=offset)).isoformat(), row.outbox_id),
            )
            ids[name] = row.outbox_id
        snapshot = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
        flagged = {
            r["target_adapter"]
            for r in snapshot["adapter_rows"]
            if r["stale_queue_reclaimable"]
        }
        claimed = await store.claim_due_outbox_items(
            now=_FIXED_SAMPLE, worker_id="reclaim", limit=10
        )
        assert flagged == {"lane--1", "lane-0"}
        assert {r.outbox_id for r in claimed} == {ids[name] for name in flagged}
    finally:
        await store.close()


@pytest.mark.parametrize("sample", [_FIXED_SAMPLE, "2026-01-02T00:00:00Z"])
async def test_projection_compares_instants_and_orders_anchors(
    tmp_path: Path, sample: str
) -> None:
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        for name, stamp, expected in (
            ("past", "2026-01-02T01:00:00+02:00", "unstarted_eligible"),
            ("equal", "2026-01-02T00:00:00Z", "unstarted_eligible"),
            ("future", "2026-01-02T00:00:00.000001+00:00", "retry_scheduled"),
            ("unknown", "2026-01-02T00:00:00", "unstarted_eligible"),
            ("empty", "", "unstarted_eligible"),
        ):
            row = await _add_row(store, event.event_id, adapter=name)
            await _claim_row(store, row)
            assert await store.mark_outbox_retry_wait(row.outbox_id, stamp)
            summary = await store.query_work_status_summary(
                sampled_at=sample, adapter=name
            )
            assert summary["adapter_rows"][0][expected] == 1
            assert summary["oldest_rows"][0]["age_origin"] == (
                "retry_due" if stamp else "admission"
            )
        later = await _add_row(store, event.event_id, adapter="order")
        earlier = await _add_row(store, event.event_id, adapter="order")
        assert later.outbox_id < earlier.outbox_id
        for row, stamp in (
            (earlier, "2026-01-02T00:00:00Z"),
            (later, "2026-01-02T00:00:00.000001+00:00"),
        ):
            await store._write(
                "UPDATE delivery_outbox SET created_at=? WHERE outbox_id=?",
                (stamp, row.outbox_id),
            )
        summary = await store.query_work_status_summary(
            sampled_at=sample, adapter="order"
        )
        assert summary["oldest_rows"][0]["outbox_id"] == earlier.outbox_id
    finally:
        await store.close()


@pytest.mark.parametrize("invalid", ["2026-01-01T00:00:00", "not-a-date", ""])
async def test_unknown_queue_timestamp_is_counted_even_with_known_oldest(
    tmp_path, invalid
):
    store = SQLiteStorage(str(tmp_path / "work.sqlite"))
    try:
        await store.initialize()
        event = await _stage_event(store)
        known = await _add_row(store, event.event_id, adapter="a")
        unknown = await _add_row(store, event.event_id, adapter="a")
        for row, timestamp in ((known, _FIXED_SAMPLE), (unknown, invalid)):
            await _claim_row(store, row)
            assert await store.mark_outbox_queued(row.outbox_id)
            await store._write(
                "UPDATE delivery_outbox SET updated_at=? WHERE outbox_id=?",
                (timestamp, row.outbox_id),
            )
        summary = await store.query_work_status_summary(sampled_at=_FIXED_SAMPLE)
        assert summary["adapter_rows"][0]["queue_staleness_unknown"] == 1
        assert summary["adapter_rows"][0]["stale_queue_reclaimable"] == 0
        assert summary["oldest_rows"][0]["outbox_id"] == known.outbox_id
        assert outstanding_age_origin(
            "queued",
            next_attempt_at=None,
            locked_at=None,
            updated_at=invalid,
            created_at=_PAST,
        ) == ("queue_admission", None)
    finally:
        await store.close()


@pytest.mark.skipif(
    not hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"),
    reason="requires Python 3.12 autocommit API",
)
def test_explicit_read_batch_ends_transaction_in_autocommit_mode():
    with closing(sqlite3.connect(":memory:", autocommit=True)) as db:
        db.row_factory = sqlite3.Row
        lock = threading.Lock()
        assert sync_read_batch(db, lock, [("SELECT 1 AS n", ())]) == [[{"n": 1}]]
        assert not db.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            sync_read_batch(
                db, lock, [("SELECT 1", ()), ("SELECT * FROM missing_table", ())]
            )
        assert not db.in_transaction
        assert sync_read_batch(db, lock, [("SELECT 2 AS n", ())]) == [[{"n": 2}]]
