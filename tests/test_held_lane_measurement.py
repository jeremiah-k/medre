"""Held-lane measurement contracts, independent of machine timing thresholds."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import measure_held_lane_workload as subject


def _profile() -> subject._Profile:
    return subject._Profile(
        name="test",
        delivery_limit=2,
        batch_size=2,
        interval_seconds=0.2,
        retries_enabled=True,
        healthy_batches=1,
        events_per_batch=2,
    )


@pytest.mark.parametrize("history_rows", [0, 1])
async def test_measurement_retains_real_attempt_history(tmp_path, history_rows):
    result = await subject._run_profile(tmp_path, _profile(), history_rows=history_rows)
    assert result["terminal_history_retained"]["seeded_sent_rows"] == history_rows
    assert result["healthy_vs_held"]["healthy_rows_completed"] == 6
    assert result["healthy_vs_held"]["healthy_rows_expected"] == 6
    assert result["capacity_snapshot_while_held"]["delivery_current"] == 1
    assert result["healthy_lane_completion"]["sample_count"] == 6
    completion = result["healthy_lane_completion"]["observed_completion_latency_ms"]
    assert completion["p50_ms"] >= 0
    round_sample = result["round_progress"]["rounds"][0]
    assert round_sample["first_observed_progress_ms"] >= round_sample["staging_ms"]
    limits = result["config"]["runtime_limits"]
    assert limits["delivery_acquire_timeout_seconds"] == 0.05
    assert result["config"]["route_retry_policy"]["jitter"] is False
    db = sqlite3.connect(tmp_path / "held-test.sqlite")
    try:
        rows = db.execute(
            "SELECT status, attempt_number, receipt_id FROM delivery_outbox "
            "WHERE target_adapter='settler'"
        ).fetchall()
        assert len(rows) == history_rows
        assert all(
            status == "sent" and attempt == 1 and receipt
            for status, attempt, receipt in rows
        )
        if rows:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM delivery_receipts WHERE receipt_id=?",
                    (rows[0][2],),
                ).fetchone()[0]
                == 1
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "failure", ["seed", "seed_cancelled", "dispatcher_start", "active_hold"]
)
async def test_measurement_cleanup_covers_setup_and_active_hold(
    tmp_path, monkeypatch, failure
):
    cleanup: list[str] = []
    original_close = subject.SQLiteStorage.close
    original_runner_stop = subject.PipelineRunner.stop
    original_dispatcher_stop = subject.RetryWorker.stop
    original_probe_restore = subject._QueryProbe.restore

    async def close(store):
        cleanup.append("store")
        await original_close(store)

    async def runner_stop(runner):
        cleanup.append("runner")
        await original_runner_stop(runner)

    async def dispatcher_stop(worker):
        cleanup.append("dispatcher")
        await original_dispatcher_stop(worker)
        assert not worker.state.abandoned

    def restore(probe):
        cleanup.append("probe")
        original_probe_restore(probe)

    monkeypatch.setattr(subject.SQLiteStorage, "close", close)
    monkeypatch.setattr(subject.PipelineRunner, "stop", runner_stop)
    monkeypatch.setattr(subject.RetryWorker, "stop", dispatcher_stop)
    monkeypatch.setattr(subject._QueryProbe, "restore", restore)
    if failure in {"seed", "seed_cancelled"}:

        async def fail_seed(*args, **kwargs):
            if failure == "seed_cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("injected failure")

        monkeypatch.setattr(subject, "_seed_terminal_history", fail_seed)
        expected_cleanup = ["runner", "store"]
    else:
        if failure == "dispatcher_start":
            original_start = subject.RetryWorker.start

            async def fail_start(worker):
                await original_start(worker)
                raise RuntimeError("injected failure")

            monkeypatch.setattr(subject.RetryWorker, "start", fail_start)
        else:
            original_poll = subject._poll_until

            async def fail_after_hold(*args, **kwargs):
                await original_poll(*args, **kwargs)
                raise RuntimeError("injected failure")

            monkeypatch.setattr(subject, "_poll_until", fail_after_hold)
        expected_cleanup = ["dispatcher", "probe", "runner", "store"]
    error = asyncio.CancelledError if failure == "seed_cancelled" else RuntimeError
    match = None if failure == "seed_cancelled" else "injected failure"
    with pytest.raises(error, match=match):
        await subject._run_profile(tmp_path, _profile(), history_rows=0)
    assert cleanup == expected_cleanup


async def test_stage_rejects_unexpected_ingress_count(monkeypatch):
    class Runner:
        async def admit_ingress(self, event, provenance):
            return SimpleNamespace(created=True, event_id=event.event_id)

    class Ingress:
        def __init__(self, **kwargs):
            pass

        async def run_once(self):
            return 0

    monkeypatch.setattr(subject, "DurableIngressWorker", Ingress)
    with pytest.raises(RuntimeError, match="expected one ingress item"):
        await subject._stage(None, Runner(), "sample", "room")


def test_optimized_python_stages_history(tmp_path: Path):
    """Execute the real bounded profile with assertions disabled in the child."""
    program = """
import asyncio
import sys
from pathlib import Path
from scripts.measure_held_lane_workload import _Profile, _run_profile

profile = _Profile('optimized', 2, 2, 0.2, True, 1, 1)
result = asyncio.run(_run_profile(Path(sys.argv[1]), profile, history_rows=1))
if result['terminal_history_retained']['seeded_sent_rows'] != 1:
    raise RuntimeError('optimized execution lost seed obligations')
if result['healthy_vs_held']['healthy_rows_completed'] != 3:
    raise RuntimeError('optimized execution lost healthy obligations')
"""
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
    }
    child = subprocess.run(
        [sys.executable, "-O", "-c", program, str(tmp_path)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stdout + child.stderr
