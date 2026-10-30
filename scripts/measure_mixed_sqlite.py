"""Local mixed-ingress/claim SQLite workload, not native-transport qualification.

Reports measurements, not performance pass/fail. Uses a fresh disposable
operator-selected directory, bounded task counts, and no external I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage


def _percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * pct + 99) // 100 - 1)], 3)


async def measure_mixed_sqlite(
    destination: Path,
    *,
    events: int = 200,
    fanout: int = 3,
    producers: int = 4,
    workers: int = 2,
) -> dict[str, object]:
    """Measure overlapping durable admission, target staging and outbox claims."""
    if not 1 <= events <= 5000 or not 1 <= fanout <= 16:
        raise ValueError("events must be 1..5000 and fanout 1..16")
    if not 1 <= producers <= 32 or not 1 <= workers <= 32:
        raise ValueError("producer/worker counts must be 1..32")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    db_path = destination / "mixed.sqlite"
    if any(destination.glob("mixed.sqlite*")):
        raise FileExistsError("measurement requires fresh disposable SQLite state")
    store = SQLiteStorage(str(db_path))
    await store.initialize()
    admissions: list[float] = []
    claims: list[float] = []
    empty_claims: list[float] = []
    claimed_ids: set[str] = set()
    claimed_by_worker: dict[str, int] = {f"worker-{i}": 0 for i in range(workers)}
    finished = asyncio.Event()
    work_changed = asyncio.Condition()
    staged_generation = 0
    utc_now = datetime.now(timezone.utc)
    started = time.perf_counter()

    async def produce(worker: int) -> None:
        nonlocal staged_generation
        for ordinal in range(worker, events, producers):
            event_id = f"mix-{ordinal:08d}"
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=utc_now,
                source_adapter="synthetic",
                source_transport_id="mixed-load",
                source_channel_id="synthetic-source",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "synthetic workload " * 8},
                metadata=EventMetadata(),
            )
            before = time.perf_counter()
            await store.admit_ingress(event, None, "live")
            admissions.append((time.perf_counter() - before) * 1000)
            for target in range(fanout):
                await store.create_outbox_item(
                    DeliveryOutboxItem(
                        outbox_id=f"outbox-{ordinal:08d}-{target}",
                        event_id=event_id,
                        route_id=f"route-{target}",
                        delivery_plan_id=f"plan-{event_id}",
                        target_adapter=f"synthetic-{target}",
                        target_channel="channel",
                        attempt_number=1,
                        status="pending",
                    )
                )
                async with work_changed:
                    staged_generation += 1
                    work_changed.notify_all()

    async def claim(worker: int) -> None:
        name = f"worker-{worker}"
        while True:
            async with work_changed:
                generation = staged_generation
                done = finished.is_set()
            before = time.perf_counter()
            batch = await store.claim_due_outbox_items(
                now=utc_now.isoformat(), worker_id=name, lease_seconds=300, limit=16
            )
            duration = (time.perf_counter() - before) * 1000
            (claims if batch else empty_claims).append(duration)
            for item in batch:
                if item.outbox_id in claimed_ids:
                    raise AssertionError("duplicate ownership of a claimed outbox row")
                claimed_ids.add(item.outbox_id)
                claimed_by_worker[name] += 1
            if not batch:
                if done:
                    return
                async with work_changed:
                    await work_changed.wait_for(
                        lambda observed=generation: staged_generation != observed
                        or finished.is_set()
                    )

    try:

        async def run_producers() -> None:
            try:
                async with asyncio.TaskGroup() as tasks:
                    for i in range(producers):
                        tasks.create_task(produce(i))
            finally:
                async with work_changed:
                    finished.set()
                    work_changed.notify_all()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(run_producers())
            for i in range(workers):
                tasks.create_task(claim(i))
        elapsed = time.perf_counter() - started
        expected = events * fanout
        if len(claimed_ids) != expected or await store.count_events() != events:
            raise AssertionError("mixed workload did not account for each event/target")
        footprint = {
            path.name: path.stat().st_size
            for path in destination.glob("mixed.sqlite*")
            if path.is_file()
        }
    finally:
        before_close = time.perf_counter()
        await store.close()
        storage_close_ms = (time.perf_counter() - before_close) * 1000
    reopened = SQLiteStorage(str(db_path))
    before = time.perf_counter()
    await reopened.initialize()
    restart_ms = (time.perf_counter() - before) * 1000
    try:
        if await reopened.count_events() != events:
            raise AssertionError("durable event count changed after restart")
    finally:
        await reopened.close()
    return {
        "qualification": "synthetic_mixed_core_sqlite_no_native_io",
        "sqlite_version": sqlite3.sqlite_version,
        "events": events,
        "fanout": fanout,
        "producer_count": producers,
        "worker_count": workers,
        "targets_claimed": len(claimed_ids),
        "claimed_by_worker": claimed_by_worker,
        "elapsed_seconds": round(elapsed, 3),
        "admission_p50_ms": round(statistics.median(admissions), 3),
        "admission_p95_ms": _percentile(admissions, 95),
        "admission_p99_ms": _percentile(admissions, 99),
        "claim_p95_ms": _percentile(claims, 95),
        "claim_p99_ms": _percentile(claims, 99),
        "claim_batch_count": len(claims),
        "empty_claim_poll_count": len(empty_claims),
        "empty_claim_p95_ms": _percentile(empty_claims, 95),
        "empty_claim_p99_ms": _percentile(empty_claims, 99),
        "restart_ms": round(restart_ms, 3),
        "storage_close_ms": round(storage_close_ms, 3),
        "sqlite_file_and_sidecar_bytes": footprint,
        "limitations": [
            "no transport SDK or actual native sender",
            "no delivery confirmation/receipt finalization",
            "no ingress route worker or native-worker shutdown measurement",
            "no crypto callback, link outage, retry, or realistic provider latency",
            "no startup resource limit or production-scale soak claim",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--events", type=int, default=200)
    parser.add_argument("--fanout", type=int, default=3)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                measure_mixed_sqlite(
                    args.destination,
                    events=args.events,
                    fanout=args.fanout,
                    producers=args.producers,
                    workers=args.workers,
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
