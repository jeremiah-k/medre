"""Measurement harness verifies cardinality, not production capacity."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.sqlite.storage import SQLiteStorage
from scripts.measure_mixed_sqlite import measure_mixed_sqlite


async def test_claim_percentiles_exclude_empty_queries(tmp_path, monkeypatch):
    from scripts import measure_mixed_sqlite as measurement

    successful_batches = 0
    empty_polls = 0
    original = SQLiteStorage.claim_due_outbox_items

    async def counted_claim(self, **kwargs):
        nonlocal successful_batches, empty_polls
        batch = await original(self, **kwargs)
        if batch:
            successful_batches += 1
        else:
            empty_polls += 1
        return batch

    monkeypatch.setattr(SQLiteStorage, "claim_due_outbox_items", counted_claim)
    # A sample-count estimator exposes which queries feed each metric without
    # relying on machine speed or the relative duration of successful queries.
    monkeypatch.setattr(measurement, "_percentile", lambda samples, pct: len(samples))
    result = await measure_mixed_sqlite(
        tmp_path, events=1, fanout=1, producers=1, workers=8
    )
    assert successful_batches == 1
    assert empty_polls >= 8
    assert result["claim_p95_ms"] == successful_batches
    assert result["claim_p99_ms"] == successful_batches
    assert result["claim_batch_count"] == successful_batches
    assert result["empty_claim_poll_count"] == empty_polls
    assert result["empty_claim_p95_ms"] == empty_polls


async def test_mixed_sqlite_measurement_claims_each_target_once(tmp_path: Path):
    result = await measure_mixed_sqlite(
        tmp_path, events=13, fanout=3, producers=3, workers=2
    )
    assert result["qualification"] == "synthetic_mixed_core_sqlite_no_native_io"
    assert result["targets_claimed"] == 39
    assert sum(result["claimed_by_worker"].values()) == 39
    assert result["admission_p99_ms"] >= 0
    assert result["sqlite_file_and_sidecar_bytes"]
    assert result["storage_close_ms"] >= 0
    db = sqlite3.connect(tmp_path / "mixed.sqlite")
    try:
        assert (
            db.execute("SELECT count(*) FROM durable_ingress_work").fetchone()[0] == 13
        )
    finally:
        db.close()
    with pytest.raises(FileExistsError):
        await measure_mixed_sqlite(tmp_path, events=1)


async def test_measurement_failure_settles_other_producers_before_close(
    tmp_path, monkeypatch
):
    other_started = asyncio.Event()
    other_cancelled = asyncio.Event()
    never = asyncio.Event()

    async def fail_or_wait(self, item):
        if item.event_id == "mix-00000000":
            await other_started.wait()
            raise RuntimeError("synthetic staging failure")
        other_started.set()
        try:
            await never.wait()
        finally:
            other_cancelled.set()

    original_close = SQLiteStorage.close
    closed_while_active = []

    async def checked_close(self):
        closed_while_active.append(not other_cancelled.is_set())
        await original_close(self)

    monkeypatch.setattr(SQLiteStorage, "create_outbox_item", fail_or_wait)
    monkeypatch.setattr(SQLiteStorage, "close", checked_close)
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await measure_mixed_sqlite(tmp_path, events=2, producers=2, workers=1)
    assert other_cancelled.is_set()
    assert closed_while_active == [False]
