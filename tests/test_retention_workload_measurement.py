"""The retention workload measurement harness stays runnable and bounded.

The smoke tier runs the real script end to end with tiny parameters and
pins the JSON report shape, the two-run (collector disabled vs enabled)
contract, and the honest limitations block — so the measurement cannot rot
silently. It asserts cardinality and shape, never machine speed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.measure_retention_workload import main, measure_retention_workload

_DISABLED_KEYS = {
    "admission_seconds",
    "admissions_per_sec",
    "settlement_seconds",
    "settlements_per_sec",
    "total_seconds",
    "staged_events",
    "settled_events",
    "durable_receipts",
    "remaining_body_rows",
    "logical_bytes_removed",
    "rows_removed",
    "sqlite_busy_count",
    "sqlite_file_and_sidecar_bytes",
    "sqlite_file_and_sidecar_bytes_before_close",
}
_ENABLED_EXTRA_KEYS = {
    "collect_phase_seconds",
    "collect_sweeps",
    "collect_calls",
    "collect_p50_ms",
    "collect_p95_ms",
    "collect_max_ms",
    "collect_outcomes",
    "quiescent_cycles_confirmed",
}


async def test_retention_workload_measurement_smoke_shape(tmp_path: Path) -> None:
    result = await measure_retention_workload(
        tmp_path, events=12, seed=7, settle_percent=70
    )
    assert result["qualification"] == "synthetic_retention_workload_no_native_io"
    assert result["events"] == 12
    assert result["fanout"] == 2
    assert result["seed"] == 7
    assert len(result["limitations"]) >= 3
    assert any("physical space recovery" in line for line in result["limitations"])

    disabled = result["collector_disabled"]
    enabled = result["collector_enabled"]
    assert disabled["collector_enabled"] is False
    assert enabled["collector_enabled"] is True
    assert _DISABLED_KEYS <= disabled.keys()
    assert _DISABLED_KEYS <= enabled.keys()
    assert _ENABLED_EXTRA_KEYS <= enabled.keys()
    assert _ENABLED_EXTRA_KEYS.isdisjoint(disabled.keys())

    # The same seed produces the same workload shape in both runs.
    assert disabled["events"] == enabled["events"] == 12
    assert disabled["staged_events"] == enabled["staged_events"]
    assert disabled["settled_events"] == enabled["settled_events"]
    # The disabled run collects nothing; the enabled run collects the
    # eligible share and removes the reported logical bytes.
    assert disabled["durable_receipts"] == 0
    assert disabled["remaining_body_rows"] == 12
    assert enabled["durable_receipts"] >= 1
    assert enabled["remaining_body_rows"] == 12 - enabled["durable_receipts"]
    assert enabled["rows_removed"] >= enabled["durable_receipts"]
    assert enabled["logical_bytes_removed"] >= 0
    assert enabled["collect_calls"] >= enabled["durable_receipts"]
    assert enabled["collect_outcomes"]["collected"] == enabled["durable_receipts"]
    assert enabled["collect_p50_ms"] >= 0
    assert enabled["collect_max_ms"] >= enabled["collect_p50_ms"]
    assert enabled["sqlite_busy_count"] >= 0
    assert disabled["sqlite_file_and_sidecar_bytes"]
    assert any(
        name.endswith(".sqlite") for name in disabled["sqlite_file_and_sidecar_bytes"]
    )
    # Both databases are disposable, separate files.
    assert (
        disabled["sqlite_file_and_sidecar_bytes"].keys()
        != enabled["sqlite_file_and_sidecar_bytes"].keys()
    )


def test_retention_workload_measurement_main_prints_the_report(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    destination = tmp_path / "cli-run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure_retention_workload.py",
            str(destination),
            "--events",
            "10",
            "--seed",
            "11",
            "--settle-percent",
            "50",
        ],
    )
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["qualification"] == "synthetic_retention_workload_no_native_io"
    assert report["events"] == 10
    assert report["collector_enabled"]["collect_outcomes"]["collected"] >= 1
    assert report["collector_disabled"]["durable_receipts"] == 0


async def test_workload_counts_short_staging_batches(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from scripts import measure_retention_workload as workload

    monkeypatch.setattr(
        workload.DurableIngressWorker, "run_once", AsyncMock(side_effect=[2, 1, 0])
    )
    result = await workload._run_workload(
        tmp_path / "short.sqlite",
        events=10,
        seed=11,
        settle_percent=0,
        collector_enabled=False,
    )
    assert result["staged_events"] == 3
    assert result["settled_events"] == 0
    assert any(
        name.endswith("-wal")
        for name in result["sqlite_file_and_sidecar_bytes_before_close"]
    )


@pytest.mark.parametrize("cursor", [None, ("2026-11-01T00:00:00+00:00", "stalled")])
async def test_workload_quiescence_is_bounded_and_closes_storage(
    tmp_path, monkeypatch, cursor
):
    from scripts import measure_retention_workload as workload

    sweeps = 0
    closed = []
    original_close = workload.SQLiteStorage.close

    async def no_progress(worker):
        nonlocal sweeps
        sweeps += 1
        assert sweeps < 20, "quiescence sweep budget was not enforced"
        worker._cursor = cursor

    async def close(store):
        await original_close(store)
        closed.append(store)

    monkeypatch.setattr(workload.ContentCollectionWorker, "sweep", no_progress)
    monkeypatch.setattr(workload.SQLiteStorage, "close", close)
    with pytest.raises(RuntimeError, match="did not reach quiescence"):
        await workload._run_workload(
            tmp_path / "stalled.sqlite",
            events=10,
            seed=11,
            settle_percent=0,
            collector_enabled=True,
        )
    assert closed
