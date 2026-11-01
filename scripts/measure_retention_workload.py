"""Bounded mixed-workload SQLite measurement for the retention vertical.

Exercises the real production-path sequence on a fresh disposable store:
durable admission through the pipeline runner, staging through the durable
ingress worker (frozen outbox snapshots included), guarded terminal
transitions for a configurable settled share, and the real
``ContentCollectionWorker`` driven to quiescence with an injected fast
clock. The mix covers plain text bodies, reply-relating events (whose
excerpts block the quoted event), attachment-associated events, and
bodyless events.

Two runs execute with the same seed and mix — one with the collector
disabled, one with it enabled — and the report compares phase wall times,
throughput, per-event collect duration percentiles, transient (SQLITE_BUSY)
occurrences, database/sidecar footprints before and after close, and the
logical bytes removed.
The report is a measurement, not a performance pass/fail verdict, and makes
no physical space recovery claim: logical byte counters are not erased
bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterContract
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation
from medre.core.events.bus import EventBus
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    ContentCollectionTransientError,
    StorageBackend,
)
from medre.core.storage.content_retention import ContentRetentionPolicy
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import ContentCollectionWorker

_MAX_EVENTS = 5000
_SWEEP_EVERY = 100
_MAX_QUIESCENT_CYCLES = 2
_CLAIM_BATCH = 200


def _percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * pct + 99) // 100 - 1)], 3)


class _AdvancingClock:
    """Deterministic injected worker clock advanced one second per sweep."""

    def __init__(self, start: datetime) -> None:
        self._value = start

    def __call__(self) -> datetime:
        return self._value

    def advance_one_sweep(self) -> None:
        self._value = self._value + timedelta(seconds=1)


def _event_body(seed: int, index: int) -> str:
    """Deterministic body content, sized like a short operator message."""
    return f"measurement body {seed:04d}-{index:06d} " + "x" * 96


class _CollectProbe:
    """Times every guarded collector call and counts transient conditions."""

    def __init__(self, store: SQLiteStorage) -> None:
        self._original = store.collect_event_content
        self.durations_ms: list[float] = []
        self.statuses: list[str] = []
        self.transient_count = 0

    def install(self, store: SQLiteStorage) -> None:
        probe = self

        async def timed_collect(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                result = await probe._original(
                    event_id, dry_run=dry_run, retention_epoch=retention_epoch
                )
            except ContentCollectionTransientError:
                probe.transient_count += 1
                raise
            finally:
                probe.durations_ms.append((time.perf_counter() - started) * 1000)
            probe.statuses.append(str(result.get("status")))
            return result

        store.collect_event_content = timed_collect  # type: ignore[method-assign]


async def _run_workload(
    db_path: Path,
    *,
    events: int,
    seed: int,
    settle_percent: int,
    collector_enabled: bool,
) -> dict[str, Any]:
    """One full admit → stage → settle (→ collect) pass on a fresh store."""
    store = SQLiteStorage(str(db_path))
    await store.initialize()
    try:
        adapters = {
            name: FakePresentationAdapter(adapter_id=name)
            for name in ("radio", "matrix")
        }
        route = Route(
            id="measure",
            source=RouteSource("synthetic", ("message.created",), None),
            targets=[RouteTarget(name, "out") for name in adapters],
        )
        adapter_contracts: dict[str, AdapterContract] = dict(adapters)
        config = PipelineConfig(
            storage=cast("StorageBackend", store),
            router=Router([route]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=store),
            adapters=adapter_contracts,
            event_bus=EventBus(),
        )
        config.route_retry_policies[route.id] = RetryPolicy(
            max_attempts=1, jitter=False
        )
        runner = PipelineRunner(config)
        now = datetime.now(timezone.utc)
        staged_events = 0

        clock = _AdvancingClock(now)
        policy = ContentRetentionPolicy(
            mode="relay-only", interval_seconds=1, batch_limit=_SWEEP_EVERY
        )
        worker: ContentCollectionWorker | None = None
        probe: _CollectProbe | None = None
        sweeps = 0
        if collector_enabled:
            probe = _CollectProbe(store)
            probe.install(store)
            worker = ContentCollectionWorker(
                store,
                store,
                policy,
                now=clock,
            )
            # Open the activation epoch on the empty store: every event is then
            # inside the epoch scope (the empty-store watermark semantics).
            await worker.sweep()
            clock.advance_one_sweep()
            sweeps += 1

        admission_started = time.perf_counter()
        for index in range(events):
            event_id = f"wl-{index:06d}"
            if index % 50 == 30:
                payload: dict[str, Any] = {}
            else:
                payload = {"body": _event_body(seed, index)}
            relations: tuple[EventRelation, ...] = ()
            if index % 10 == 7 and index > 0:
                relations = (
                    EventRelation(
                        "reply",
                        f"wl-{index - 1:06d}",
                        None,
                        None,
                        _event_body(seed, index),
                    ),
                )
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=now,
                source_adapter="synthetic",
                source_transport_id="measure",
                source_channel_id="synthetic-source",
                parent_event_id=None,
                lineage=(),
                relations=relations,
                payload=payload,
                metadata=EventMetadata(),
            )
            await runner.admit_ingress(event, "live")
            if index % 25 == 11:
                content_ref = "sha256:" + f"{index:064x}"
                await store._write(
                    "INSERT INTO attachment_blobs "
                    "(content_ref,size_bytes,media_kind,mime_type,created_at,data) "
                    "VALUES (?, 4, NULL, NULL, ?, ?)",
                    (content_ref, now.isoformat(), b"blob"),
                )
                await store._write(
                    "INSERT INTO event_attachment_associations "
                    "(event_id,content_ref,created_at) VALUES (?, ?, ?)",
                    (event_id, content_ref, now.isoformat()),
                )
        admission_seconds = time.perf_counter() - admission_started

        settlement_started = time.perf_counter()
        ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=64)
        while (processed := await ingress.run_once()) > 0:
            staged_events += processed
        # The settle share terminates the oldest staged rows through the real
        # guarded claim and terminal transitions; the remainder stays pending
        # (blocked live work).
        rows_to_settle = (events * settle_percent // 100) * 2
        settled_rows = 0
        while settled_rows < rows_to_settle:
            claimed = await store.claim_due_outbox_items(
                now.isoformat(),
                "settler",
                limit=min(_CLAIM_BATCH, rows_to_settle - settled_rows),
            )
            if not claimed:
                break
            for item in claimed:
                if not await store.mark_outbox_sent(item.outbox_id):
                    raise AssertionError(
                        f"terminal transition refused {item.outbox_id}"
                    )
                settled_rows += 1
            if collector_enabled and worker is not None:
                # Interleave one collector sweep per settle batch: the mixed
                # workload shares the storage executor with the collector.
                await worker.sweep()
                clock.advance_one_sweep()
                sweeps += 1
        settlement_seconds = time.perf_counter() - settlement_started

        collect_seconds = 0.0
        quiescent_cycles = 0
        if collector_enabled and worker is not None:
            assert probe is not None
            collect_started = time.perf_counter()
            receipts_before = await store.count_collected_events()
            max_sweeps = (_MAX_QUIESCENT_CYCLES + 2) * (
                1 + (events + policy.batch_limit - 1) // policy.batch_limit
            )
            quiescence_sweeps = 0
            while quiescent_cycles < _MAX_QUIESCENT_CYCLES:
                if quiescence_sweeps >= max_sweeps:
                    raise RuntimeError(
                        f"collector did not reach quiescence within {max_sweeps} sweeps"
                    )
                last_sweep = worker.state.last_sweep_at
                await worker.sweep()
                quiescence_sweeps += 1
                clock.advance_one_sweep()
                sweeps += 1
                if (
                    worker.state.restricted
                    or worker.state.awaiting_activation
                    or worker.state.abandoned
                ):
                    raise RuntimeError(
                        "collector cannot reach quiescence while suspended"
                    )
                if worker._cursor is None and worker.state.last_sweep_at != last_sweep:
                    receipts_now = await store.count_collected_events()
                    if receipts_now == receipts_before:
                        quiescent_cycles += 1
                    else:
                        quiescent_cycles = 0
                    receipts_before = receipts_now
            collect_seconds = time.perf_counter() - collect_started

        collected = await store.count_collected_events()
        bodies = await store._read_one("SELECT COUNT(*) AS n FROM event_body_content")
        sent_rows_row = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox WHERE status='sent'"
        )
        sent_rows = int(sent_rows_row["n"]) if sent_rows_row is not None else 0
        logical_bytes = worker.state.logical_bytes_removed if worker is not None else 0
        rows_removed = worker.state.rows_removed if worker is not None else 0
        footprint_before_close = {
            path.name: path.stat().st_size
            for path in db_path.parent.glob(db_path.name + "*")
            if path.is_file()
        }
    finally:
        await store.close()

    footprint = {
        path.name: path.stat().st_size
        for path in db_path.parent.glob(db_path.name + "*")
        if path.is_file()
    }
    result: dict[str, Any] = {
        "collector_enabled": collector_enabled,
        "events": events,
        "staged_events": staged_events,
        "settled_events": sent_rows // 2,
        "admission_seconds": round(admission_seconds, 3),
        "admissions_per_sec": round(events / max(admission_seconds, 1e-9)),
        "settlement_seconds": round(settlement_seconds, 3),
        "settlements_per_sec": round(sent_rows // 2 / max(settlement_seconds, 1e-9)),
        "total_seconds": round(
            admission_seconds + settlement_seconds + collect_seconds, 3
        ),
        "durable_receipts": collected,
        "remaining_body_rows": int(bodies["n"]) if bodies is not None else None,
        "logical_bytes_removed": logical_bytes,
        "rows_removed": rows_removed,
        "sqlite_busy_count": probe.transient_count if probe else 0,
        "sqlite_file_and_sidecar_bytes": footprint,
        "sqlite_file_and_sidecar_bytes_before_close": footprint_before_close,
    }
    if collector_enabled and worker is not None:
        assert probe is not None
        statuses: dict[str, int] = {}
        for status in probe.statuses:
            statuses[status] = statuses.get(status, 0) + 1
        result["collect_phase_seconds"] = round(collect_seconds, 3)
        result["collect_sweeps"] = sweeps
        result["collect_calls"] = len(probe.durations_ms)
        result["collect_p50_ms"] = _percentile(probe.durations_ms, 50)
        result["collect_p95_ms"] = _percentile(probe.durations_ms, 95)
        result["collect_max_ms"] = _percentile(probe.durations_ms, 100)
        result["collect_outcomes"] = statuses
        result["quiescent_cycles_confirmed"] = quiescent_cycles
    return result


async def measure_retention_workload(
    destination: Path,
    *,
    events: int = 1000,
    seed: int = 20261101,
    settle_percent: int = 70,
) -> dict[str, Any]:
    """Run the bounded mixed workload twice (collector off/on) and report."""
    if not 1 <= events <= _MAX_EVENTS:
        raise ValueError(f"events must be in [1, {_MAX_EVENTS}]")
    if not 0 <= settle_percent <= 100:
        raise ValueError("settle-percent must be in [0, 100]")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    disabled_path = destination / "retention-disabled.sqlite"
    enabled_path = destination / "retention-enabled.sqlite"
    for path in (disabled_path, enabled_path):
        if any(destination.glob(path.name + "*")):
            raise FileExistsError("measurement requires fresh disposable SQLite state")
    disabled = await _run_workload(
        disabled_path,
        events=events,
        seed=seed,
        settle_percent=settle_percent,
        collector_enabled=False,
    )
    enabled = await _run_workload(
        enabled_path,
        events=events,
        seed=seed,
        settle_percent=settle_percent,
        collector_enabled=True,
    )
    return {
        "qualification": "synthetic_retention_workload_no_native_io",
        "sqlite_version": sqlite3.sqlite_version,
        "seed": seed,
        "events": events,
        "fanout": 2,
        "settle_percent": settle_percent,
        "mix": {
            "reply_relating": "every 10th (quotes the previous event)",
            "attachment_associated": "every 25th",
            "bodyless": "every 50th",
        },
        "collector_disabled": disabled,
        "collector_enabled": enabled,
        "overhead_seconds": round(
            enabled["total_seconds"] - disabled["total_seconds"], 3
        ),
        "limitations": [
            "no transport SDK or actual native sender",
            "settlement uses the guarded claim/terminal transitions without "
            "adapter invocation",
            "no concurrent ingress worker or retry worker contention beyond "
            "the interleaved collector sweeps",
            "no physical space recovery claim: logical byte counters are not "
            "erased bytes; SQLite free pages and WAL growth between snapshots are unmeasured",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--settle-percent", type=int, default=70)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                measure_retention_workload(
                    args.destination,
                    events=args.events,
                    seed=args.seed,
                    settle_percent=args.settle_percent,
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
