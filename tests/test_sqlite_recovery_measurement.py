"""Synthetic storage measurements validate evidence, not a timing threshold."""

from __future__ import annotations

import pytest

from scripts.measure_sqlite_recovery import measure_sqlite_recovery


async def test_bounded_measurement_reopens_all_synthetic_events(tmp_path):
    """Measured events must survive reopening and an existing database must be preserved."""
    report = await measure_sqlite_recovery(tmp_path / "sample", 24)
    assert report["events"] == report["recovered_events"] == 24
    assert report["events_per_second"] > 0
    assert report["database_and_sidecars_bytes"] > 0
    assert report["qualification"] == "synthetic_single_host_no_native_io"
    with pytest.raises(FileExistsError):
        await measure_sqlite_recovery(tmp_path / "sample", 24)


@pytest.mark.parametrize("count", [0, -1, 10_001, True])
async def test_measurement_refuses_invalid_workload_count(tmp_path, count):
    """Reject invalid or coerced workloads before creating measurement state."""
    with pytest.raises(ValueError):
        await measure_sqlite_recovery(tmp_path / "sample", count)


async def test_reopen_metric_excludes_verification_and_close(tmp_path, monkeypatch):
    """Expensive verification and cleanup cannot inflate storage initialization time."""
    from scripts import measure_sqlite_recovery as subject

    clock = 0.0
    original_count = subject.SQLiteStorage.count_events
    original_close = subject.SQLiteStorage.close

    def tick():
        """Advance deterministic timing without a wall-clock sleep."""
        nonlocal clock
        clock += 0.001
        return clock

    async def expensive_count(storage):
        """Make verification observably slower than reopening in the synthetic clock."""
        nonlocal clock
        clock += 1000
        return await original_count(storage)

    async def expensive_close(storage):
        """Charge cleanup separately so it cannot be hidden inside the reopen metric."""
        nonlocal clock
        clock += 1000
        await original_close(storage)

    monkeypatch.setattr(subject.time, "perf_counter", tick)
    monkeypatch.setattr(subject.SQLiteStorage, "count_events", expensive_count)
    monkeypatch.setattr(subject.SQLiteStorage, "close", expensive_close)
    report = await subject.measure_sqlite_recovery(tmp_path / "sample", 3)
    assert report["restart_ms"] == pytest.approx(1.0)
