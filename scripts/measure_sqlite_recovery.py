"""Bounded, single-host SQLite write and reopen measurement without transport I/O.

This utility reports observed figures, never passes or fails against arbitrary
throughput targets. The operator supplies a disposable output directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.sqlite.storage import SQLiteStorage


async def measure_sqlite_recovery(destination: Path, count: int) -> dict[str, object]:
    """Write exact synthetic events, reopen storage, and verify retained count."""
    if type(count) is not int or count < 1 or count > 10_000:
        raise ValueError("event count must be between 1 and 10000")
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    db_path = destination / "measurement.sqlite"
    if db_path.exists() or any(destination.glob("measurement.sqlite-*")):
        raise FileExistsError("measurement requires a fresh disposable database")
    storage = SQLiteStorage(db_path=str(db_path))
    latencies: list[float] = []
    initialized = False
    try:
        await storage.initialize()
        initialized = True
        start = time.perf_counter()
        for ordinal in range(count):
            event = CanonicalEvent(
                event_id=f"measure-{ordinal:08d}",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="synthetic",
                source_transport_id="measure",
                source_channel_id="0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "payload " * 8},
                metadata=EventMetadata(),
            )
            before = time.perf_counter()
            await storage.append(event)
            latencies.append((time.perf_counter() - before) * 1000)
        elapsed = time.perf_counter() - start
    finally:
        if initialized:
            await storage.close()
    footprint = sum(
        p.stat().st_size for p in destination.glob("measurement.sqlite*") if p.is_file()
    )
    reopened = SQLiteStorage(db_path=str(db_path))
    started = time.perf_counter()
    await reopened.initialize()
    restart_ms = (time.perf_counter() - started) * 1000
    try:
        recovered = await reopened.count_events()
    finally:
        await reopened.close()
    if recovered != count:
        raise AssertionError("stored event count changed after reopen")
    ordered = sorted(latencies)
    return {
        "events": count,
        "recovered_events": recovered,
        "events_per_second": round(count / elapsed, 2),
        "append_p50_ms": round(statistics.median(latencies), 3),
        "append_p95_ms": round(ordered[max(0, (95 * count + 99) // 100 - 1)], 3),
        "restart_ms": round(restart_ms, 3),
        "database_and_sidecars_bytes": footprint,
        "qualification": "synthetic_single_host_no_native_io",
    }


def main() -> int:
    """Measure an explicitly selected disposable workload and emit its observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--events", type=int, default=250)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(measure_sqlite_recovery(args.destination, args.events)),
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
