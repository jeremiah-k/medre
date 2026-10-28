"""Idle polling and indexed adapter-lane selection contracts."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.initial_outbox import _pipeline, _stage


async def test_idle_dispatch_counts_refresh_on_retry_interval(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = _pipeline(temp_storage)
    refresh = AsyncMock(return_value={"sent": 1200, "pending": 0})
    monkeypatch.setattr(temp_storage, "count_outbox_by_status", refresh)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    for seconds in (0, 0.5, 1, 5, 9.9):
        await worker._process_due(now + timedelta(seconds=seconds))
    assert refresh.await_count == 1
    assert worker.outbox_counts == {"sent": 1200, "pending": 0}
    await worker._process_due(now + timedelta(seconds=10))
    assert refresh.await_count == 2


async def test_claimed_work_refreshes_counts_without_waiting_for_interval(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = _pipeline(temp_storage)
    refresh = AsyncMock(side_effect=[{"pending": 0}, {"sent": 2}])
    monkeypatch.setattr(temp_storage, "count_outbox_by_status", refresh)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=60,
    )
    now = datetime.now(timezone.utc)
    await worker._process_due(now)
    await _stage(temp_storage, runner, "after-empty-poll")
    await worker._process_due(now + timedelta(seconds=1))
    assert refresh.await_count == 2
    assert worker.outbox_counts == {"sent": 2}


async def test_claim_query_uses_due_and_active_adapter_indexes(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inspect the actual claim SQL, not a simplified surrogate predicate.
    original = temp_storage._read_all
    captured: list[tuple[str, tuple[object, ...]]] = []

    async def record(sql, params=()):
        if sql.startswith("WITH eligible"):
            captured.append((sql, params))
        return await original(sql, params)

    monkeypatch.setattr(temp_storage, "_read_all", record)
    now = datetime.now(timezone.utc).isoformat()
    await temp_storage.claim_due_outbox_items(
        now,
        "explain-owner",
        initial_only=True,
        one_per_adapter=True,
    )
    assert len(captured) == 1
    sql, params = captured[0]
    # sqlite3's context manager commits/rolls back but never closes; an
    # unclosed handle raises an unraisable ResourceWarning at GC time.
    with contextlib.closing(sqlite3.connect(temp_storage._db_path)) as conn:
        plan = [
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        ]
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(delivery_outbox)")}
    assert {"idx_outbox_due", "idx_outbox_active_adapter"} <= indexes
    assert any("idx_outbox_due" in step for step in plan)
    assert any("idx_outbox_active_adapter" in step for step in plan)


async def test_completed_dispatch_refreshes_counts_on_next_wake(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = _pipeline(temp_storage)
    refresh = AsyncMock(wraps=temp_storage.count_outbox_by_status)
    monkeypatch.setattr(temp_storage, "count_outbox_by_status", refresh)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=60,
    )
    now = datetime.now(timezone.utc)
    await worker._process_due(now)
    assert refresh.await_count == 1
    await _stage(temp_storage, runner, "completed-dispatch")
    async with asyncio.TaskGroup() as group:
        await worker._process_due(now + timedelta(seconds=1), group=group)
    assert worker._outbox_counts_dirty
    await worker._process_due(now + timedelta(seconds=1.5))
    assert refresh.await_count == 3
    assert not worker._outbox_counts_dirty
    assert worker.outbox_counts.get("sent", 0) == 2


async def test_completion_during_count_keeps_refresh_pending(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = _pipeline(temp_storage)
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = AsyncMock(side_effect=[{"pending": 1}, {"sent": 1}])

    async def delayed_refresh():
        entered.set()
        await release.wait()
        return await refresh()

    monkeypatch.setattr(temp_storage, "count_outbox_by_status", delayed_refresh)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=60,
    )
    now = datetime.now(timezone.utc)
    cycle = asyncio.create_task(worker._process_due(now))
    await asyncio.wait_for(entered.wait(), 1)
    worker._outbox_count_generation += 1
    worker._outbox_counts_dirty = True
    release.set()
    await cycle
    assert worker._outbox_counts_dirty
    await worker._process_due(now + timedelta(seconds=1))
    assert refresh.await_count == 2
    assert worker.outbox_counts == {"sent": 1}
    assert not worker._outbox_counts_dirty
