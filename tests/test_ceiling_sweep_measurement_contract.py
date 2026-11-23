"""Contract checks for liveness and contention measurements.

These prove the probe and fixture semantics without running minute-scale loads.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import measure_ceiling_sweep as measurement


@pytest.mark.asyncio
async def test_claim_completion_gap_includes_slow_or_failing_calls(monkeypatch) -> None:
    class Store:
        calls = 0

        async def claim_due_outbox_items(self) -> list[object]:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("failed claim")
            return []

    ticks = iter([1.0, 1.01, 2.0, 2.5, 3.0, 3.01])
    monkeypatch.setattr(
        measurement,
        "time",
        SimpleNamespace(perf_counter=lambda: next(ticks)),
    )
    store = Store()
    probe = measurement._ClaimProbe(store)
    probe.install()
    try:
        await store.claim_due_outbox_items()
        with pytest.raises(RuntimeError, match="failed claim"):
            await store.claim_due_outbox_items()
        await store.claim_due_outbox_items()
        assert [call["outcome"] for call in probe.calls] == ["empty", "error", "empty"]
        assert probe.ends == [1.01, 2.5, 3.01]
        assert probe.max_claim_gap_seconds() == pytest.approx(1.49)
    finally:
        probe.restore()


@pytest.mark.asyncio
async def test_external_writer_runs_off_loop_and_releases_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "writer.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel (value INTEGER)")
    connection.commit()
    connection.close()
    blocker = measurement._BlockedWriter(path)
    task = asyncio.create_task(
        blocker.run_cycles(hold_seconds=(0.2,), release_seconds=0.01)
    )
    try:
        assert await asyncio.to_thread(blocker.active.wait, 1.0)
        # An active native SQLite reservation must not freeze this event loop.
        await asyncio.wait_for(asyncio.sleep(0.02), timeout=0.1)
        assert blocker.holds == 1
    finally:
        blocker.stop_requested.set()
        await task
    assert blocker.windows[0]["acquired"] is True
    assert blocker.windows[0]["released_at"] >= blocker.windows[0]["acquired_at"]
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    finally:
        connection.close()
