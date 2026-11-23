"""Content-ceiling sweep pressure measurement, not a capacity verdict.

Drives the production SQLite content-ceiling enforcement surface
(``SQLiteStorage.enforce_content_ceilings`` behind the opt-in
``retry.ceiling_sweep`` worker policy) under adversarial controlled loads
and reports per-run measurements against no built-in threshold: every
acceptance budget lives in the dated measurement record under ``docs/dev``,
not in this script. Each load runs the identical workload twice, differing
only in the sweep policy: disabled (baseline) and enabled. Loads:

* ``history-heavy`` — a large mostly-ineligible policy history (future
  policies plus elapsed-but-receipted policies) with a small set of elapsed
  unpurged targets, plus a late residual write behind the residual cursor.
* ``continuous-stream`` — new ceiling-bearing admissions through the real
  producer boundary arriving for the whole window while the sweep runs,
  plus a plain claimable trickle so native dispatch work continues.
* ``fanout-attachments`` — per-event route fanout across several parked
  outbox generations with dispatch snapshots, radio progress, shared
  attachment blobs referenced by several events, and neighbor quote
  excerpts quoting the targets.
* ``blocked-writer`` — an external raw connection holding ``BEGIN
  IMMEDIATE`` for repeating windows while the sweep and the dispatch
  trickle run, exercising the write-lock path and the worker's bounded
  ``SQLITE_BUSY`` failure mode.
* ``interrupt-restart`` — a child process is SIGKILLed mid-sweep and the
  parent reopens the store, asserts consistency, resumes the sweep to
  completion and verifies every receipt; a separate phase cancels the
  worker mid-tick and measures teardown, then resumes.

Sweep targets are seeded in bulk with direct row writes shaped exactly like
the producer boundary writes for the history loads, and through the real
``admit_ingress(content_deadline=...)`` boundary where admission latency
itself is measured. Every dispatch handoff uses a fake adapter only; no
transport SDK, no native send, no recipient delivery is exercised. This is
a measurement, not a pass/fail verdict, and a purge receipt is not a
physical-erasure certificate: freed pages and WAL frames survive logical
enforcement until the operator erasure procedure in
``docs/spec/content-privacy-ceiling.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import CeilingSweepConfig, RetryConfig, RuntimeLimits
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    StorageError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from medre.runtime.retry import RetryWorker

_POLL_SECONDS = 0.01
_TRICKLE_PERIOD_SECONDS = 0.5
_FINAL_READER_SAMPLES = 25
_MID_RUN_READER_SAMPLES = 5
_SWEEP_INTERVAL_SECONDS = 0.5
_SWEEP_LIMIT = 50

_CLAIM_LANE = "claim-fast"
_CLAIM_CHANNEL = "sweep-claim"
_STREAM_TARGET_CHANNEL = "stream-target"

_LOADS = (
    "history-heavy",
    "continuous-stream",
    "fanout-attachments",
    "blocked-writer",
    "interrupt-restart",
)

_LOAD_REPEATS: dict[str, int] = {
    "history-heavy": 3,
    "continuous-stream": 3,
    "fanout-attachments": 3,
    "blocked-writer": 2,
    "interrupt-restart": 1,
}

#: Fixed per-load scales; the doc's budgets refer to these exact numbers.
_HISTORY_HEAVY_FUTURE_POLICIES = 3000
_HISTORY_HEAVY_RECEIPTED_POLICIES = 2000
_HISTORY_HEAVY_TARGETS = 240
_HISTORY_HEAVY_ACTIVE_SECONDS = 30.0
_STREAM_HISTORY_RECEIPTS = 800
_STREAM_FUTURE_POLICIES = 200
_STREAM_SECONDS = 12.0
_STREAM_CEILING_DELAY_SECONDS = 2.0
_FANOUT_TARGETS = 300
_FANOUT_GENERATIONS = 6
_FANOUT_BLOB_GROUPS = 150
_BLOCKED_TARGETS = 300
_BLOCKED_HOLD_SECONDS = (1.5, 6.0, 1.5, 1.5)
_BLOCKED_RELEASE_SECONDS = 3.5
_INTERRUPT_TARGETS = 1200
_CANCEL_TARGETS = 800

_ENFORCEMENT_CURSOR_KEY = "content_ceiling_enforcement_cursor"
_RESIDUAL_CURSOR_KEY = "content_ceiling_residual_cursor"


def _utc_micro() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * pct + 99) // 100 - 1)], 3)


def _timings(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    return {
        "p50": _percentile(samples, 50),
        "p95": _percentile(samples, 95),
        "p99": _percentile(samples, 99),
        "max": round(max(samples), 3),
        "n": len(samples),
    }


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _poll_until(condition: Any, *, timeout: float, description: str) -> None:
    """Bounded condition polling; a timeout is a measurement-setup failure."""
    deadline = time.perf_counter() + timeout
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.perf_counter() >= deadline:
            raise AssertionError(f"measurement setup did not settle: {description}")
        await asyncio.sleep(_POLL_SECONDS)


class _CeilingProbe:
    """Times the storage ceiling-enforcement surface the worker captured.

    Wrap ``store.enforce_content_ceilings`` after seeding and offline
    receipting but before the RetryWorker is constructed: the poll cycle
    captures the sweep surface once.
    """

    def __init__(self, store: SQLiteStorage) -> None:
        self._store = store
        self._original = store.enforce_content_ceilings
        self.tick_ms: list[float] = []
        self.enforced_total = 0
        self.intervals: list[tuple[float, float]] = []
        self.errors: list[dict[str, Any]] = []
        self.heartbeat_path: Path | None = None

    def install(self) -> None:
        probe = self

        async def timed_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            started = time.perf_counter()
            probe._heartbeat("tick_start", len(probe.tick_ms))
            probe.intervals.append((started, 0.0))
            try:
                enforced = await probe._original(*args, **kwargs)
            except BaseException as exc:
                ended = time.perf_counter()
                probe.intervals[-1] = (started, ended)
                probe.tick_ms.append((ended - started) * 1000)
                probe.errors.append(
                    {"at": ended, "type": type(exc).__name__, "detail": str(exc)[:120]}
                )
                probe._heartbeat("tick_error", probe.enforced_total)
                raise
            ended = time.perf_counter()
            probe.intervals[-1] = (started, ended)
            probe.tick_ms.append((ended - started) * 1000)
            probe.enforced_total += len(enforced)
            probe._heartbeat("tick_end", probe.enforced_total)
            return enforced

        self._store.enforce_content_ceilings = timed_sweep  # type: ignore[method-assign]

    def restore(self) -> None:
        self._store.enforce_content_ceilings = self._original  # type: ignore[method-assign]

    def _heartbeat(self, phase: str, count: int) -> None:
        if self.heartbeat_path is None:
            return
        record = {"phase": phase, "count": count, "t": time.time()}
        with self.heartbeat_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def during_tick_ms(self, started: float, ended: float) -> float:
        """Fraction of the [started, ended] span overlapped by a sweep tick."""
        overlap = 0.0
        for tick_start, tick_end in self.intervals:
            if tick_end <= 0.0:
                continue
            low = max(started, tick_start)
            high = min(ended, tick_end)
            if high > low:
                overlap += high - low
        span = max(ended - started, 1e-9)
        return overlap / span

    def tick_active(self) -> bool:
        if not self.intervals:
            return False
        return self.intervals[-1][1] <= 0.0


class _ClaimProbe:
    """Times dispatcher claim calls against the store."""

    def __init__(self, store: SQLiteStorage) -> None:
        self._store = store
        self._original = store.claim_due_outbox_items
        self.claim_nonempty_ms: list[float] = []
        self.claim_empty_ms: list[float] = []
        self.starts: list[float] = []
        self.ends: list[float] = []
        self.calls: list[dict[str, Any]] = []

    def install(self) -> None:
        probe = self

        async def timed_claim(*args: Any, **kwargs: Any) -> list[Any]:
            started = time.perf_counter()
            outcome = "error"
            try:
                batch = await probe._original(*args, **kwargs)
                outcome = "nonempty" if batch else "empty"
                return batch
            finally:
                ended = time.perf_counter()
                probe.starts.append(started)
                probe.ends.append(ended)
                probe.calls.append(
                    {"started": started, "ended": ended, "outcome": outcome}
                )
                if outcome == "nonempty":
                    probe.claim_nonempty_ms.append((ended - started) * 1000)
                elif outcome == "empty":
                    probe.claim_empty_ms.append((ended - started) * 1000)

        self._store.claim_due_outbox_items = timed_claim  # type: ignore[method-assign]

    def restore(self) -> None:
        self._store.claim_due_outbox_items = self._original  # type: ignore[method-assign]

    def max_claim_gap_seconds(self) -> float:
        """Largest stall between consecutive claim completions."""
        if len(self.ends) < 2:
            return 0.0
        return max(
            later_end - earlier_end
            for earlier_end, later_end in zip(self.ends, self.ends[1:], strict=False)
        )


def _event(event_id: str, channel: str, *, body_size: int = 64) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="synthetic",
        source_transport_id="measure",
        source_channel_id=channel,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": f"ceiling-sweep measurement {event_id} " + "x" * body_size},
        metadata=EventMetadata(),
        source_native_ref=NativeRef(
            adapter="synthetic",
            native_channel_id=channel,
            native_message_id=event_id,
        ),
    )


def _pipeline(store: SQLiteStorage, adapters: dict[str, Any]) -> PipelineRunner:
    """Production-shaped pipeline assembly with one fast claim lane."""
    claim = Route(
        id="measure-ceiling-claim",
        source=RouteSource(
            adapter="synthetic",
            channel=_CLAIM_CHANNEL,
            event_kinds=("message.created",),
        ),
        targets=[RouteTarget(adapter=_CLAIM_LANE, channel="out")],
    )
    config = PipelineConfig(
        storage=store,  # type: ignore[arg-type]
        router=Router(routes=[claim]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=EventBus(),
    )
    config.route_retry_policies[claim.id] = RetryPolicy(jitter=False)
    return PipelineRunner(config)


async def _stage_claimable(
    store: SQLiteStorage,
    runner: PipelineRunner,
    event_id: str,
    *,
    admission_ms: list[float],
) -> float:
    """Admit one plain event durably and stage its claimable obligation."""
    started = time.perf_counter()
    admitted = await runner.admit_ingress(_event(event_id, _CLAIM_CHANNEL), "live")
    if not admitted.created or admitted.event_id != event_id:
        raise RuntimeError(f"{event_id}: expected a new durable admission")
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    processed = await ingress.run_once()
    admission_ms.append((time.perf_counter() - started) * 1000)
    if processed != 1:
        raise RuntimeError(f"{event_id}: expected one ingress item, got {processed}")
    return time.perf_counter()


async def _trickle_claimable(
    context: _RunContext,
    *,
    seconds: float,
    stop: asyncio.Event,
) -> dict[str, Any]:
    """Stage one plain claimable event per period; poll each until sent.

    Queue latency is observed at the trickle cadence, so each sample
    includes up to one poll period of observation delay.
    """
    admission_ms: list[float] = []
    queue_latency_ms: list[float] = []
    outstanding: dict[str, float] = {}
    sent_total = 0
    index = 0
    failed_admissions = 0
    deadline = time.perf_counter() + seconds
    while not stop.is_set() and time.perf_counter() < deadline:
        for event_id, staged_at in list(outstanding.items()):
            rows = await context.store.list_outbox_items_for_event(
                event_id, metadata_only=True
            )
            if rows and rows[0].status == "sent":
                queue_latency_ms.append((time.perf_counter() - staged_at) * 1000)
                del outstanding[event_id]
        event_id = f"trickle-{index:05d}"
        try:
            outstanding[event_id] = await _stage_claimable(
                context.store, context.runner, event_id, admission_ms=admission_ms
            )
        except StorageError:
            failed_admissions += 1
        index += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TRICKLE_PERIOD_SECONDS)
        except TimeoutError:
            pass
    stop.set()
    for event_id, staged_at in list(outstanding.items()):
        rows = await context.store.list_outbox_items_for_event(
            event_id, metadata_only=True
        )
        if rows and rows[0].status == "sent":
            queue_latency_ms.append((time.perf_counter() - staged_at) * 1000)
            del outstanding[event_id]
    sent_total = len(queue_latency_ms)
    return {
        "trickle_admission_ms": _timings(admission_ms),
        "queue_latency_ms": _timings(queue_latency_ms),
        "trickle_staged_total": index - failed_admissions,
        "trickle_admission_failures": failed_admissions,
        "trickle_sent_total": sent_total,
        "trickle_unsent_at_close": len(outstanding),
    }


@dataclass
class _RunContext:
    """Shared per-run assembly for one load condition."""

    store: SQLiteStorage
    runner: PipelineRunner
    claim_adapter: FakePresentationAdapter
    capacity: CapacityController
    db_path: Path
    ceiling_probe: _CeilingProbe
    claim_probe: _ClaimProbe

    def install_probes(self) -> None:
        self.ceiling_probe.install()
        self.claim_probe.install()

    def restore_probes(self) -> None:
        self.ceiling_probe.restore()
        self.claim_probe.restore()


def _open_run(destination: Path, name: str) -> _RunContext:
    db_path = destination / f"{name}.sqlite"
    if any(destination.glob(db_path.name + "*")):
        raise FileExistsError("measurement requires fresh disposable SQLite state")
    store = SQLiteStorage(str(db_path))
    claim_adapter = FakePresentationAdapter(adapter_id=_CLAIM_LANE)
    runner = _pipeline(store, {_CLAIM_LANE: claim_adapter})
    limits = RuntimeLimits(
        max_inflight_deliveries=50,
        delivery_acquire_timeout_seconds=0.05,
    )
    capacity = CapacityController(limits)
    runner.set_capacity_controller(capacity)
    return _RunContext(
        store=store,
        runner=runner,
        claim_adapter=claim_adapter,
        capacity=capacity,
        db_path=db_path,
        ceiling_probe=_CeilingProbe(store),
        claim_probe=_ClaimProbe(store),
    )


def _make_dispatcher(
    context: _RunContext,
    *,
    sweep_enabled: bool,
    interval_seconds: float = _SWEEP_INTERVAL_SECONDS,
) -> RetryWorker:
    dispatcher = RetryWorker(
        context.store,
        context.runner,
        context.capacity,
        initial_dispatch=True,
        enabled=True,
        interval_seconds=interval_seconds,
        batch_size=20,
        max_attempts=4,
        stop_timeout_seconds=0.5,
        retry_config=RetryConfig(
            ceiling_sweep=CeilingSweepConfig(
                enabled=sweep_enabled,
                limit=_SWEEP_LIMIT,
                interval_seconds=_SWEEP_INTERVAL_SECONDS,
            )
        ),
    )
    dispatcher.set_available_target_adapters([_CLAIM_LANE])
    return dispatcher


async def _bulk_seed_events(
    store: SQLiteStorage,
    *,
    prefix: str,
    count: int,
    hard_ceiling: datetime | None,
    batch_size: int = 400,
) -> None:
    """Seed canonical identity, separated body and optional policy rows.

    Direct row writes shaped exactly like the producer boundary writes;
    used only for bulk history where admission latency is not measured.
    Each event carries a non-empty envelope payload so the purge also
    exercises the envelope-value clearing path.
    """
    now = _utc_micro()
    for start in range(0, count, batch_size):
        stop = min(start + batch_size, count)
        ops: list[tuple[str, tuple[Any, ...]]] = []
        for index in range(start, stop):
            event_id = f"{prefix}-{index:06d}"
            body = f"bulk history body for {event_id} " + "y" * 96
            ops.append(
                (
                    "INSERT INTO canonical_events (event_id, event_kind, "
                    "schema_version, timestamp, source_adapter, "
                    "source_transport_id, source_channel_id, lineage, payload, "
                    "metadata, created_at) VALUES (?, 'message.created', 1, ?, "
                    "'synthetic', 'measure', ?, '[]', ?, '{}', ?)",
                    (event_id, now, f"{prefix}-ch", json.dumps({"body": body}), now),
                )
            )
            ops.append(
                (
                    "INSERT INTO event_body_content (event_id, field, body, "
                    "created_at) VALUES (?, 'body', ?, ?)",
                    (event_id, body, now),
                )
            )
            if hard_ceiling is not None:
                ops.append(
                    (
                        "INSERT INTO event_content_access_policies "
                        "(event_id, version, hard_ceiling, allowed_consumers, "
                        "max_optional_hold_microseconds, created_at) "
                        "VALUES (?, 1, ?, '[]', 1, ?)",
                        (
                            event_id,
                            hard_ceiling.isoformat(timespec="microseconds"),
                            now,
                        ),
                    )
                )
        await store._write_batch(ops)


async def _receipt_history_offline(
    store: SQLiteStorage, *, expected_receipts: int
) -> int:
    """Receipt every elapsed seeded policy before the measured phase.

    Runs the real enforcement surface to completion (bounded calls at the
    maximum limit) so the measured phase starts from a realistic enforced
    history, then resets both durable cursors so the measured window starts
    a fresh cycle from the store's first key.
    """
    now = datetime.now(timezone.utc)
    for _ in range(200):
        enforced = await store.enforce_content_ceilings(now=now, limit=500)
        if not enforced:
            break
    audit = await store.audit_content_ceilings(limit=1)
    if audit["elapsed_pending"] or audit["elapsed_pending_has_more"]:
        raise AssertionError("offline receipting left elapsed pending policies")
    receipts = int(audit["receipts"])
    if receipts < expected_receipts:
        raise AssertionError(f"offline receipting got {receipts} < {expected_receipts}")
    for key in (_ENFORCEMENT_CURSOR_KEY, _RESIDUAL_CURSOR_KEY):
        await store._write(
            "INSERT INTO _medre_schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(["", ""])),
        )
    return receipts


def _footprint(db_path: Path) -> dict[str, int]:
    return {
        path.name: path.stat().st_size
        for path in sorted(db_path.parent.glob(db_path.name + "*"))
        if path.is_file()
    }


async def _reader_probe(db_path: Path, *, samples: int) -> dict[str, Any]:
    """Time read-only operator-view queries from an independent connection."""
    readonly = await SQLiteStorage.open_readonly(str(db_path))
    query_ms: list[float] = []
    try:
        for _ in range(samples):
            started = time.perf_counter()
            await readonly.query_work_status_summary()
            query_ms.append((time.perf_counter() - started) * 1000)
    finally:
        await readonly.close()
    return {
        "work_status_ms": _timings(query_ms),
        "sample_count": len(query_ms),
    }


async def _ceiling_audit_counts(store: SQLiteStorage) -> dict[str, int]:
    """First-page read-only audit counts; pagination flags included."""
    audit = await store.audit_content_ceilings(limit=100)
    return {
        "elapsed_pending_first_page": len(audit["elapsed_pending"]),
        "elapsed_pending_has_more": int(audit["elapsed_pending_has_more"]),
        "residual_enforced_first_page": len(audit["residual_enforced"]),
        "residual_enforced_has_more": int(audit["residual_enforced_has_more"]),
        "receipts": int(audit["receipts"]),
    }


async def _elapsed_unreceipted_age_raw(db_path: Path) -> dict[str, float]:
    """Age of the oldest elapsed-but-unpurged policy at observation time."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT p.hard_ceiling FROM event_content_access_policies p "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=p.event_id"
            ")"
        ).fetchall()
    finally:
        connection.close()
    now = datetime.now(timezone.utc)
    ages: list[float] = []
    for row in rows:
        ceiling = datetime.fromisoformat(str(row["hard_ceiling"]))
        if ceiling <= now:
            ages.append((now - ceiling).total_seconds())
    if not ages:
        return {"elapsed_unreceipted_max_age_s": 0.0, "elapsed_unreceipted_n": 0}
    return {
        "elapsed_unreceipted_max_age_s": round(max(ages), 3),
        "elapsed_unreceipted_n": len(ages),
    }


def _split_admissions(
    admissions: list[dict[str, Any]], probe: _CeilingProbe
) -> dict[str, Any]:
    """Split admission latencies into during-tick and off-tick samples."""
    during: list[float] = []
    off: list[float] = []
    for record in admissions:
        if probe.during_tick_ms(record["started"], record["ended"]) > 0.0:
            during.append(record["duration_ms"])
        else:
            off.append(record["duration_ms"])
    return {
        "admission_ms_during_tick": _timings(during),
        "admission_ms_off_tick": _timings(off),
    }


async def _admit_stream_target(
    store: SQLiteStorage,
    event_id: str,
    *,
    admission_ms: list[float],
    admissions: list[dict[str, Any]],
) -> None:
    """Admit one ceiling-bearing event through the real producer boundary."""
    started = time.perf_counter()
    ceiling_at = datetime.now(timezone.utc) + timedelta(
        seconds=_STREAM_CEILING_DELAY_SECONDS
    )
    admitted = await store.admit_ingress(
        _event(event_id, _STREAM_TARGET_CHANNEL),
        None,
        "history",
        content_deadline=ceiling_at,
    )
    duration_ms = (time.perf_counter() - started) * 1000
    if not admitted.created or admitted.event_id != event_id:
        raise RuntimeError(f"{event_id}: expected a new durable admission")
    admission_ms.append(duration_ms)
    admissions.append(
        {
            "event_id": event_id,
            "started": started,
            "ended": time.perf_counter(),
            "duration_ms": duration_ms,
            "ceiling_unix": ceiling_at.timestamp(),
        }
    )


async def _first_target_receipt_lag(
    store: SQLiteStorage, *, prefix: str, dispatcher_started_unix: float
) -> float:
    """Seconds from dispatcher start to the first target enforcement receipt."""
    rows = await store._read_all(
        "SELECT MIN(enforced_at) AS first FROM event_content_ceiling_log "
        "WHERE event_id LIKE ?",
        (f"{prefix}-%",),
    )
    first = rows[0]["first"] if rows else None
    if first is None:
        return -1.0
    first_unix = datetime.fromisoformat(str(first)).timestamp()
    return round(first_unix - dispatcher_started_unix, 3)


async def _run_history_heavy(
    destination: Path, *, sweep_enabled: bool, repeat: int
) -> dict[str, Any]:
    """Mostly-ineligible policy history with a small elapsed target set."""
    condition = "enabled" if sweep_enabled else "disabled"
    context = _open_run(destination, f"history-heavy-{condition}-r{repeat}")
    store = context.store
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        resources.callback(context.restore_probes)
        await store.initialize()
        await _bulk_seed_events(
            store,
            prefix="hh-future",
            count=_HISTORY_HEAVY_FUTURE_POLICIES,
            hard_ceiling=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        await _bulk_seed_events(
            store,
            prefix="hh-history",
            count=_HISTORY_HEAVY_RECEIPTED_POLICIES,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        await _receipt_history_offline(
            store, expected_receipts=_HISTORY_HEAVY_RECEIPTED_POLICIES
        )
        await _bulk_seed_events(
            store,
            prefix="hh-target",
            count=_HISTORY_HEAVY_TARGETS,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        seeded_footprint = _footprint(context.db_path)
        context.install_probes()
        dispatcher_started_unix = time.time()
        dispatcher = _make_dispatcher(context, sweep_enabled=sweep_enabled)
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()
        reader = asyncio.create_task(
            _reader_probe(context.db_path, samples=_MID_RUN_READER_SAMPLES)
        )
        resources.push_async_callback(_cancel_task, reader)
        trickle = await _trickle_claimable(
            context, seconds=_HISTORY_HEAVY_ACTIVE_SECONDS, stop=asyncio.Event()
        )
        mid_run_reader = await reader
        purge_wait_started = time.perf_counter()
        if sweep_enabled:
            await _poll_until(
                lambda: context.ceiling_probe.enforced_total >= _HISTORY_HEAVY_TARGETS,
                timeout=300.0,
                description="sweep did not receipt every elapsed target",
            )
        purge_wait_seconds = time.perf_counter() - purge_wait_started

        # Late residual write on an already-receipted event: the residual
        # cursor must wrap and repair it without rewriting the receipt.
        residual_repair: dict[str, Any] = {"seeded": False}
        if sweep_enabled:
            target = "hh-history-000000"
            await store._write(
                'UPDATE canonical_events SET metadata=\'{"late":"residual"}\' '
                "WHERE event_id=?",
                (target,),
            )
            residual_repair = {"seeded": True, "event_id": target}
            repair_started = time.perf_counter()
            ticks_before = len(context.ceiling_probe.tick_ms)
            await _poll_until(
                lambda: _residual_repaired(store, target),
                timeout=300.0,
                description="residual cursor never repaired the late write",
            )
            residual_repair["repair_seconds"] = round(
                time.perf_counter() - repair_started, 3
            )
            residual_repair["ticks_during_repair"] = (
                len(context.ceiling_probe.tick_ms) - ticks_before
            )
        if dispatcher.state.ceiling_sweep_errors:
            raise AssertionError(
                "ceiling sweep recorded errors outside the blocked load"
            )
        audit_counts = await _ceiling_audit_counts(store)
        worker_state = {
            "ceiling_sweep_runs": dispatcher.state.ceiling_sweep_runs,
            "ceiling_sweep_errors": dispatcher.state.ceiling_sweep_errors,
            "ceiling_enforced": dispatcher.state.ceiling_enforced,
            "processed": dispatcher.state.processed,
            "succeeded": dispatcher.state.succeeded,
        }
        footprint_before_close = _footprint(context.db_path)
        first_receipt_lag = -1.0
        if sweep_enabled:
            first_receipt_lag = await _first_target_receipt_lag(
                store,
                prefix="hh-target",
                dispatcher_started_unix=dispatcher_started_unix,
            )
    footprint_after_close = _footprint(context.db_path)
    at_rest_reader = await _reader_probe(context.db_path, samples=_FINAL_READER_SAMPLES)
    ages_raw = await _elapsed_unreceipted_age_raw(context.db_path)
    claim_gap = context.claim_probe.max_claim_gap_seconds()
    return {
        "load": "history-heavy",
        "condition": condition,
        "repeat_index": repeat,
        "sweep_enabled": sweep_enabled,
        "sweep_tick_ms": _timings(context.ceiling_probe.tick_ms),
        "sweep_enforced_total": context.ceiling_probe.enforced_total,
        "first_target_receipt_lag_s": first_receipt_lag,
        "purge_completion_wait_s": round(purge_wait_seconds, 3),
        "residual_repair": residual_repair,
        **trickle,
        "claim_nonempty_ms": _timings(context.claim_probe.claim_nonempty_ms),
        "claim_empty_ms": _timings(context.claim_probe.claim_empty_ms),
        "max_claim_gap_s": round(claim_gap, 3),
        "reader_mid_run": mid_run_reader,
        "reader_at_rest": at_rest_reader,
        "audit_counts": audit_counts,
        "elapsed_age_at_close": ages_raw,
        "worker_state": worker_state,
        "footprint_seeded": seeded_footprint,
        "footprint_before_close": footprint_before_close,
        "footprint_after_close": footprint_after_close,
    }


async def _residual_repaired(store: SQLiteStorage, event_id: str) -> bool:
    """True once the per-event residual battery observes no content.

    Uses the read-only single-event audit, the same battery enforcement
    verifies with; the harness never writes through this path.
    """
    audit = await store.audit_content_ceilings(limit=1, event_id=event_id)
    return str(audit.get("state")) == "enforced_clean"


async def _run_continuous_stream(
    destination: Path, *, sweep_enabled: bool, repeat: int
) -> dict[str, Any]:
    """Continuous ceiling-bearing admissions while the sweep runs."""
    condition = "enabled" if sweep_enabled else "disabled"
    context = _open_run(destination, f"continuous-stream-{condition}-r{repeat}")
    store = context.store
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        resources.callback(context.restore_probes)
        await store.initialize()
        await _bulk_seed_events(
            store,
            prefix="cs-future",
            count=_STREAM_FUTURE_POLICIES,
            hard_ceiling=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        await _bulk_seed_events(
            store,
            prefix="cs-history",
            count=_STREAM_HISTORY_RECEIPTS,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        await _receipt_history_offline(
            store, expected_receipts=_STREAM_HISTORY_RECEIPTS
        )
        context.install_probes()
        dispatcher = _make_dispatcher(context, sweep_enabled=sweep_enabled)
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()
        trickle = asyncio.create_task(
            _trickle_claimable(context, seconds=_STREAM_SECONDS, stop=asyncio.Event())
        )
        resources.push_async_callback(_cancel_task, trickle)
        reader = asyncio.create_task(
            _reader_probe(context.db_path, samples=_MID_RUN_READER_SAMPLES)
        )
        resources.push_async_callback(_cancel_task, reader)
        stream_admission_ms: list[float] = []
        admissions: list[dict[str, Any]] = []
        stream_started = time.perf_counter()
        index = 0
        while time.perf_counter() - stream_started < _STREAM_SECONDS:
            await _admit_stream_target(
                store,
                f"cs-target-{index:05d}",
                admission_ms=stream_admission_ms,
                admissions=admissions,
            )
            index += 1
            await asyncio.sleep(0.05)
        trickle_result = await trickle
        mid_run_reader = await reader
        purge_wait_started = time.perf_counter()
        if sweep_enabled:
            await _poll_until(
                lambda: context.ceiling_probe.enforced_total >= index,
                timeout=240.0,
                description="sweep did not receipt every streamed target",
            )
            receipt_tail_s = await _receipt_tails(store)
        else:
            receipt_tail_s = {}
        purge_wait_seconds = time.perf_counter() - purge_wait_started
        if dispatcher.state.ceiling_sweep_errors:
            raise AssertionError("ceiling sweep recorded errors in the stream load")
        audit_counts = await _ceiling_audit_counts(store)
        worker_state = {
            "ceiling_sweep_runs": dispatcher.state.ceiling_sweep_runs,
            "ceiling_sweep_errors": dispatcher.state.ceiling_sweep_errors,
            "ceiling_enforced": dispatcher.state.ceiling_enforced,
            "processed": dispatcher.state.processed,
            "succeeded": dispatcher.state.succeeded,
        }
        footprint_before_close = _footprint(context.db_path)
    footprint_after_close = _footprint(context.db_path)
    at_rest_reader = await _reader_probe(context.db_path, samples=_FINAL_READER_SAMPLES)
    claim_gap = context.claim_probe.max_claim_gap_seconds()
    result = {
        "load": "continuous-stream",
        "condition": condition,
        "repeat_index": repeat,
        "sweep_enabled": sweep_enabled,
        "streamed_targets": index,
        "sweep_tick_ms": _timings(context.ceiling_probe.tick_ms),
        "sweep_enforced_total": context.ceiling_probe.enforced_total,
        "purge_completion_wait_s": round(purge_wait_seconds, 3),
        "stream_admission_ms": _timings(stream_admission_ms),
        **trickle_result,
        "claim_nonempty_ms": _timings(context.claim_probe.claim_nonempty_ms),
        "claim_empty_ms": _timings(context.claim_probe.claim_empty_ms),
        "max_claim_gap_s": round(claim_gap, 3),
        "reader_mid_run": mid_run_reader,
        "reader_at_rest": at_rest_reader,
        "audit_counts": audit_counts,
        "worker_state": worker_state,
        "footprint_before_close": footprint_before_close,
        "footprint_after_close": footprint_after_close,
    }
    if sweep_enabled:
        result["receipt_tail"] = receipt_tail_s
        result["admission_overlap"] = _split_admissions(
            admissions, context.ceiling_probe
        )
    return result


async def _receipt_tails(store: SQLiteStorage) -> dict[str, Any]:
    """Receipt tail latency: enforcement time minus each ceiling instant."""
    rows = await store._read_all(
        "SELECT l.event_id, l.enforced_at, p.hard_ceiling "
        "FROM event_content_ceiling_log l "
        "JOIN event_content_access_policies p ON p.event_id=l.event_id "
        "WHERE l.event_id LIKE 'cs-target-%'",
        (),
    )
    tails: list[float] = []
    for row in rows:
        enforced = datetime.fromisoformat(str(row["enforced_at"])).timestamp()
        ceiling = datetime.fromisoformat(str(row["hard_ceiling"])).timestamp()
        tails.append(max(0.0, enforced - ceiling))
    if not tails:
        return {"receipt_tail_after_elapse_s_max": 0.0, "n": 0}
    return {
        "receipt_tail_after_elapse_s_max": round(max(tails), 3),
        "receipt_tail_after_elapse_s_p50": _percentile(tails, 50),
        "n": len(tails),
    }


def _blob_ref(group: int) -> str:
    return "sha256:" + f"{group:06d}" * 10 + "ef" * 2


async def _seed_fanout_event(
    store: SQLiteStorage, event_id: str, content_ref: str
) -> None:
    """Seed one fanout target: generations, copies, shared blob, quotes."""
    now = _utc_micro()
    await store.admit_ingress(
        _event(event_id, _STREAM_TARGET_CHANNEL),
        None,
        "history",
        content_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    # Backdate the immutable policy to an elapsed ceiling: measurement setup
    # only, exactly the backdated-admission shape the cursor wrap revisits.
    await store._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(
                timespec="microseconds"
            ),
            event_id,
        ),
    )
    await store._write(
        "UPDATE canonical_events SET payload=?, metadata=? WHERE event_id=?",
        (
            json.dumps({"opaque": f"envelope-{event_id}"}),
            '{"custom":{"n":1}}',
            event_id,
        ),
    )
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', NULL, 'opaque-key', "
        '\'{"relation":"opaque"}\', ?)',
        (event_id, now),
    )
    relation = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=?", (event_id,)
    )
    await store._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) VALUES (?, ?)",
        (relation["id"], f"own excerpt for {event_id}"),
    )
    await store._write(
        "INSERT INTO native_message_refs (id, event_id, adapter, native_message_id, "
        "direction, metadata, created_at) VALUES (?, ?, 'source', ?, 'inbound', "
        '\'{"native":"opaque"}\', ?)',
        (f"nref-{event_id}", event_id, f"nm-{event_id}", now),
    )
    for generation in range(_FANOUT_GENERATIONS):
        outbox_id = f"obox-{event_id}-{generation:02d}"
        created = await store.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id=outbox_id,
                event_id=event_id,
                delivery_plan_id=f"plan-{event_id}-{generation:02d}",
                route_id=f"route-{generation:02d}",
                target_adapter=f"fanout-lane-{generation:02d}",
                target_channel="out",
                status="pending",
                attempt_number=0,
                next_attempt_at=(
                    datetime.now(timezone.utc) + timedelta(hours=12)
                ).isoformat(),
            ),
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct",
                context={
                    "delivery_strategy": "direct",
                    "capability_level": "native",
                    "capability_field": None,
                    "capability_reason": None,
                    "deadline": (
                        datetime.now(timezone.utc) + timedelta(hours=2)
                    ).isoformat(),
                },
            ),
        )
        if created.outbox_id != outbox_id:
            raise AssertionError(f"{outbox_id}: unexpected outbox identity")
        await store._write(
            "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
            (json.dumps({"inline": f"dispatch-context-{event_id}"}), outbox_id),
        )
        await store._write(
            "INSERT INTO radio_attempt_progress (outbox_id, attempt_number, "
            "plan_fingerprint, plan_json, provenance_json, parts_json, revision, "
            "created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?)",
            (
                outbox_id,
                "ab" * 32,
                '{"plan":"synthetic"}',
                '{"provenance":"synthetic"}',
                json.dumps({"parts": [f"radio part {event_id}"]}),
                now,
                now,
            ),
        )
    await store._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES (?, ?, ?, 'fanout-lane-00', ?, 1, "
        "'delivered', '{\"provider\":\"opaque\"}', ?)",
        (
            f"obs-{event_id}",
            event_id,
            f"plan-{event_id}-00",
            f"obox-{event_id}-00",
            now,
        ),
    )
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('observation', ?, 1, 'error', ?, ?)",
        (f"obs-{event_id}", f"failure detail for {event_id}", now),
    )
    await store._write(
        "INSERT INTO event_attachment_associations (event_id, content_ref, "
        "created_at) VALUES (?, ?, ?)",
        (event_id, content_ref, now),
    )


async def _seed_fanout_neighbors(store: SQLiteStorage, event_id: str) -> None:
    """Two neighbors quote the target so incoming excerpts must be purged."""
    now = _utc_micro()
    for neighbor_index in range(2):
        neighbor_id = f"fn-neighbor-{event_id}-{neighbor_index}"
        await store.admit_ingress(
            _event(neighbor_id, _STREAM_TARGET_CHANNEL, body_size=16),
            None,
            "history",
        )
        await store._write(
            "INSERT INTO event_relations (event_id, relation_type, "
            "target_event_id, key, metadata, created_at) "
            "VALUES (?, 'reply', ?, NULL, '{}', ?)",
            (neighbor_id, event_id, now),
        )
        relation = await store._read_one(
            "SELECT id FROM event_relations WHERE event_id=?", (neighbor_id,)
        )
        await store._write(
            "INSERT INTO event_relation_excerpts (relation_id, excerpt) "
            "VALUES (?, ?)",
            (relation["id"], f"quoted excerpt from {event_id}"),
        )


async def _run_fanout_attachments(
    destination: Path, *, sweep_enabled: bool, repeat: int
) -> dict[str, Any]:
    """Route fanout, shared blobs and neighbor quotes across many targets."""
    condition = "enabled" if sweep_enabled else "disabled"
    context = _open_run(destination, f"fanout-attachments-{condition}-r{repeat}")
    store = context.store
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        resources.callback(context.restore_probes)
        await store.initialize()
        now = _utc_micro()
        for group in range(_FANOUT_BLOB_GROUPS):
            await store._write(
                "INSERT INTO attachment_blobs (content_ref, size_bytes, "
                "media_kind, mime_type, created_at, data) "
                "VALUES (?, 4096, 'image', 'image/png', ?, ?)",
                (_blob_ref(group), now, b"FANOUTBLOB" * 409),
            )
        seeding_started = time.perf_counter()
        for index in range(_FANOUT_TARGETS):
            event_id = f"fn-target-{index:05d}"
            await _seed_fanout_event(
                store, event_id, _blob_ref(index % _FANOUT_BLOB_GROUPS)
            )
            await _seed_fanout_neighbors(store, event_id)
        seeding_seconds = time.perf_counter() - seeding_started
        seeded_footprint = _footprint(context.db_path)
        context.install_probes()
        dispatcher = _make_dispatcher(context, sweep_enabled=sweep_enabled)
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()
        reader = asyncio.create_task(
            _reader_probe(context.db_path, samples=_MID_RUN_READER_SAMPLES)
        )
        resources.push_async_callback(_cancel_task, reader)
        trickle = await _trickle_claimable(context, seconds=20.0, stop=asyncio.Event())
        mid_run_reader = await reader
        purge_wait_started = time.perf_counter()
        if sweep_enabled:
            await _poll_until(
                lambda: context.ceiling_probe.enforced_total >= _FANOUT_TARGETS,
                timeout=300.0,
                description="sweep did not receipt every fanout target",
            )
        purge_wait_seconds = time.perf_counter() - purge_wait_started
        blobs = await store._read_one("SELECT COUNT(*) AS n FROM attachment_blobs", ())
        blob_survivors = int(blobs["n"])
        if sweep_enabled and blob_survivors != 0:
            raise AssertionError(
                f"every shared blob must lose its last reference after all "
                f"fanout purges; {blob_survivors} survive"
            )
        if dispatcher.state.ceiling_sweep_errors:
            raise AssertionError("ceiling sweep recorded errors in the fanout load")
        audit_counts = await _ceiling_audit_counts(store)
        worker_state = {
            "ceiling_sweep_runs": dispatcher.state.ceiling_sweep_runs,
            "ceiling_sweep_errors": dispatcher.state.ceiling_sweep_errors,
            "ceiling_enforced": dispatcher.state.ceiling_enforced,
            "processed": dispatcher.state.processed,
            "succeeded": dispatcher.state.succeeded,
        }
        footprint_before_close = _footprint(context.db_path)
    footprint_after_close = _footprint(context.db_path)
    at_rest_reader = await _reader_probe(context.db_path, samples=_FINAL_READER_SAMPLES)
    claim_gap = context.claim_probe.max_claim_gap_seconds()
    return {
        "load": "fanout-attachments",
        "condition": condition,
        "repeat_index": repeat,
        "sweep_enabled": sweep_enabled,
        "seeding_seconds": round(seeding_seconds, 3),
        "blob_survivors_after_wait": blob_survivors,
        "sweep_tick_ms": _timings(context.ceiling_probe.tick_ms),
        "sweep_enforced_total": context.ceiling_probe.enforced_total,
        "purge_completion_wait_s": round(purge_wait_seconds, 3),
        **trickle,
        "claim_nonempty_ms": _timings(context.claim_probe.claim_nonempty_ms),
        "claim_empty_ms": _timings(context.claim_probe.claim_empty_ms),
        "max_claim_gap_s": round(claim_gap, 3),
        "reader_mid_run": mid_run_reader,
        "reader_at_rest": at_rest_reader,
        "audit_counts": audit_counts,
        "worker_state": worker_state,
        "footprint_seeded": seeded_footprint,
        "footprint_before_close": footprint_before_close,
        "footprint_after_close": footprint_after_close,
    }


class _BlockedWriter:
    """Run SQLite lock-holder calls in their own thread, never the event loop."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self.holds = 0
        self.rejected = 0
        self.windows: list[dict[str, Any]] = []
        self.active = threading.Event()
        self.stop_requested = threading.Event()

    def _cycles(self, durations: tuple[float, ...], release: float) -> None:
        connection = sqlite3.connect(str(self._db_path), timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        try:
            for duration in durations:
                if self.stop_requested.wait(release):
                    break
                window: dict[str, Any] = {"scheduled_seconds": duration}
                window["begin_at"] = time.perf_counter()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    self.rejected += 1
                    window.update(acquired=False, error_type=type(exc).__name__)
                    self.windows.append(window)
                    continue
                self.holds += 1
                window.update(acquired=True, acquired_at=time.perf_counter())
                self.windows.append(window)
                self.active.set()
                try:
                    self.stop_requested.wait(duration)
                    connection.execute("COMMIT")
                finally:
                    window["released_at"] = time.perf_counter()
                    self.active.clear()
                    if connection.in_transaction:
                        connection.rollback()
        finally:
            self.active.clear()
            connection.close()

    async def run_cycles(
        self, *, hold_seconds: tuple[float, ...], release_seconds: float
    ) -> None:
        task = asyncio.create_task(
            asyncio.to_thread(self._cycles, hold_seconds, release_seconds)
        )
        try:
            await asyncio.shield(task)
        finally:
            if not task.done():
                self.stop_requested.set()
                await asyncio.shield(task)


async def _run_blocked_writer(
    destination: Path, *, sweep_enabled: bool, repeat: int
) -> dict[str, Any]:
    """Repeating external write-lock holds against sweep and dispatch work."""
    condition = "enabled" if sweep_enabled else "disabled"
    context = _open_run(destination, f"blocked-writer-{condition}-r{repeat}")
    store = context.store
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        resources.callback(context.restore_probes)
        await store.initialize()
        await _bulk_seed_events(
            store,
            prefix="bw-target",
            count=_BLOCKED_TARGETS,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        context.install_probes()
        dispatcher = _make_dispatcher(context, sweep_enabled=sweep_enabled)
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()
        stop_trickle = asyncio.Event()
        trickle = asyncio.create_task(
            _trickle_claimable(context, seconds=40.0, stop=stop_trickle)
        )

        async def _join_trickle() -> None:
            stop_trickle.set()
            await _cancel_task(trickle)

        resources.push_async_callback(_join_trickle)
        blocker = _BlockedWriter(context.db_path)
        blocking = asyncio.create_task(
            blocker.run_cycles(
                hold_seconds=_BLOCKED_HOLD_SECONDS,
                release_seconds=_BLOCKED_RELEASE_SECONDS,
            )
        )

        async def _join_blocker() -> None:
            blocker.stop_requested.set()
            await asyncio.gather(blocking, return_exceptions=True)

        resources.push_async_callback(_join_blocker)
        acquired = await asyncio.to_thread(blocker.active.wait, 20.0)
        assert acquired, "no writer hold was acquired"
        mid_run_reader = await _reader_probe(
            context.db_path, samples=_MID_RUN_READER_SAMPLES
        )
        await blocking
        stop_trickle.set()
        trickle_result = await trickle
        purge_wait_started = time.perf_counter()
        if sweep_enabled:
            await _poll_until(
                lambda: context.ceiling_probe.enforced_total >= _BLOCKED_TARGETS,
                timeout=300.0,
                description="blocked sweep never completed every target purge",
            )
        purge_wait_seconds = time.perf_counter() - purge_wait_started
        audit_counts = await _ceiling_audit_counts(store)
        worker_state = {
            "ceiling_sweep_runs": dispatcher.state.ceiling_sweep_runs,
            "ceiling_sweep_errors": dispatcher.state.ceiling_sweep_errors,
            "ceiling_enforced": dispatcher.state.ceiling_enforced,
            "processed": dispatcher.state.processed,
            "succeeded": dispatcher.state.succeeded,
            "failed": dispatcher.state.failed,
        }
        footprint_before_close = _footprint(context.db_path)
    footprint_after_close = _footprint(context.db_path)
    at_rest_reader = await _reader_probe(context.db_path, samples=_FINAL_READER_SAMPLES)
    claim_gap = context.claim_probe.max_claim_gap_seconds()
    return {
        "load": "blocked-writer",
        "condition": condition,
        "repeat_index": repeat,
        "sweep_enabled": sweep_enabled,
        "blocker_holds": blocker.holds,
        "blocker_rejected_begins": blocker.rejected,
        "blocker_windows": blocker.windows,
        "sweep_errors": context.ceiling_probe.errors,
        "sweep_errors_during_hold": sum(
            any(
                hold.get("acquired")
                and hold["acquired_at"] <= error["at"] <= hold["released_at"]
                for hold in blocker.windows
                if "released_at" in hold
            )
            for error in context.ceiling_probe.errors
        ),
        "sweep_tick_ms": _timings(context.ceiling_probe.tick_ms),
        "sweep_enforced_total": context.ceiling_probe.enforced_total,
        "purge_completion_wait_s": round(purge_wait_seconds, 3),
        **trickle_result,
        "claim_nonempty_ms": _timings(context.claim_probe.claim_nonempty_ms),
        "claim_empty_ms": _timings(context.claim_probe.claim_empty_ms),
        "max_claim_gap_s": round(claim_gap, 3),
        "reader_mid_run": mid_run_reader,
        "reader_at_rest": at_rest_reader,
        "audit_counts": audit_counts,
        "worker_state": worker_state,
        "footprint_before_close": footprint_before_close,
        "footprint_after_close": footprint_after_close,
    }


def _interrupt_child(child_dest: Path) -> None:
    """Child mode: seed a purge backlog, run the enabled sweep, never stop.

    The parent SIGKILLs this process mid-sweep; heartbeats let the parent
    determine whether the kill landed inside a tick.
    """
    heartbeat_path = child_dest / "child-heartbeat.jsonl"
    store = SQLiteStorage(str(child_dest / "interrupt-child.sqlite"))
    probe = _CeilingProbe(store)
    probe.heartbeat_path = heartbeat_path
    probe.install()

    async def child_main() -> None:
        with heartbeat_path.open("a") as heartbeat:
            heartbeat.write(
                json.dumps(
                    {"phase": "source", "root": _source_identity()["package_root"]}
                )
                + "\n"
            )
        await store.initialize()
        await _bulk_seed_events(
            store,
            prefix="ir-target",
            count=_INTERRUPT_TARGETS,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        claim_adapter = FakePresentationAdapter(adapter_id=_CLAIM_LANE)
        runner = _pipeline(store, {_CLAIM_LANE: claim_adapter})
        dispatcher = RetryWorker(
            store,
            runner,
            None,
            initial_dispatch=True,
            enabled=True,
            interval_seconds=0.2,
            batch_size=20,
            max_attempts=4,
            stop_timeout_seconds=0.5,
            retry_config=RetryConfig(
                ceiling_sweep=CeilingSweepConfig(
                    enabled=True,
                    limit=_SWEEP_LIMIT,
                    interval_seconds=0.2,
                )
            ),
        )
        dispatcher.set_available_target_adapters([_CLAIM_LANE])
        await dispatcher.start()
        await asyncio.Event().wait()

    asyncio.run(child_main())


def _raw_counts(db_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        policies = connection.execute(
            "SELECT COUNT(*) AS n FROM event_content_access_policies"
        ).fetchone()[0]
        receipts = connection.execute(
            "SELECT COUNT(*) AS n FROM event_content_ceiling_log"
        ).fetchone()[0]
        partial_with_receipt = connection.execute(
            "SELECT COUNT(*) AS n FROM event_content_ceiling_log l "
            "WHERE EXISTS (SELECT 1 FROM event_body_content b "
            "WHERE b.event_id=l.event_id)"
        ).fetchone()[0]
        cleared_without_receipt = connection.execute(
            "SELECT COUNT(*) AS n FROM event_content_access_policies p "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=p.event_id"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM event_body_content b WHERE b.event_id=p.event_id"
            ")"
        ).fetchone()[0]
        cursor_row = connection.execute(
            "SELECT value FROM _medre_schema_meta WHERE key=?",
            (_ENFORCEMENT_CURSOR_KEY,),
        ).fetchone()
        residual_row = connection.execute(
            "SELECT value FROM _medre_schema_meta WHERE key=?",
            (_RESIDUAL_CURSOR_KEY,),
        ).fetchone()
    finally:
        connection.close()
    return {
        "integrity_check": str(integrity),
        "policies": int(policies),
        "receipts": int(receipts),
        "receipt_with_body_rows": int(partial_with_receipt),
        "cleared_envelope_without_receipt": int(cleared_without_receipt),
        "enforcement_cursor": (
            str(cursor_row[0]) if cursor_row is not None else "absent"
        ),
        "residual_cursor": (
            str(residual_row[0]) if residual_row is not None else "absent"
        ),
    }


async def _verify_all_receipts(store: SQLiteStorage) -> dict[str, Any]:
    """Re-run the residual battery over every receipt, paging to the end."""
    examined = 0
    failed = 0
    after_event: str | None = None
    verdicts: set[str] = set()
    complete = False
    for _ in range(200):
        report = await store.verify_content_ceiling_receipts(
            limit=500, after_event=after_event
        )
        examined += int(report["receipts_examined"])
        failed += int(report["receipts_failed"])
        verdicts.add(str(report["verdict"]))
        if not report["receipts_has_more"]:
            complete = True
            break
        after_event = str(report["receipts_next_after_event"])
    assert complete, "receipt verifier pagination incomplete"
    return {
        "receipts_examined": examined,
        "receipts_failed": failed,
        "verdicts": sorted(verdicts),
    }


async def _resume_to_completion(
    child_db: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Reopen the killed store, assert consistency, finish and verify."""
    reopened = SQLiteStorage(str(child_db))
    consistency: dict[str, Any] = {}
    async with AsyncExitStack() as resources:
        resources.push_async_callback(reopened.close)
        await reopened.initialize()
        raw = _raw_counts(child_db)
        consistency["raw_after_restart"] = raw
        if raw["integrity_check"] != "ok":
            raise AssertionError(f"integrity check failed after restart: {raw}")
        if raw["receipt_with_body_rows"] or raw["cleared_envelope_without_receipt"]:
            raise AssertionError(f"partial purge state after restart: {raw}")
        started = time.perf_counter()
        for _ in range(400):
            enforced = await reopened.enforce_content_ceilings(limit=500)
            if not enforced:
                break
        consistency["resume_seconds"] = round(time.perf_counter() - started, 3)
        audit = await reopened.audit_content_ceilings(limit=25)
        if audit["elapsed_pending"] or audit["residual_enforced"]:
            raise AssertionError(
                f"resumed sweep left findings: {len(audit['elapsed_pending'])} "
                f"pending, {len(audit['residual_enforced'])} residual"
            )
        consistency["verification"] = await _verify_all_receipts(reopened)
        assert consistency["verification"]["receipts_failed"] == 0
        assert consistency["verification"]["receipts_examined"] == _INTERRUPT_TARGETS
    return consistency, _footprint(child_db)


async def _run_interrupt_restart(destination: Path) -> dict[str, Any]:
    """SIGKILL a sweeping child mid-sweep, restart, resume, verify receipts."""
    child_dest = destination / "interrupt-child-state"
    child_dest.mkdir(parents=True, exist_ok=True)
    heartbeat_path = child_dest / "child-heartbeat.jsonl"
    if heartbeat_path.exists():
        heartbeat_path.unlink()
    child_db = child_dest / "interrupt-child.sqlite"
    for leftover in child_dest.glob("interrupt-child.sqlite*"):
        leftover.unlink()
    script = Path(__file__).resolve()
    environment = dict(os.environ)
    package_root = str(_source_identity()["package_root"])
    environment["PYTHONPATH"] = (
        package_root + os.pathsep + environment["PYTHONPATH"]
        if environment.get("PYTHONPATH")
        else package_root
    )

    def _spawn() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(script), str(destination), "--interrupt-child"],
            cwd=str(destination),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    child = await asyncio.to_thread(_spawn)

    def _reap(process: subprocess.Popen[str]) -> None:
        process.kill()
        process.wait(timeout=30)

    receipts_at_kill = -1
    try:
        watch: sqlite3.Connection | None = None
        deadline = time.perf_counter() + 120.0
        while time.perf_counter() < deadline:
            if child.poll() is not None:
                break
            try:
                # Never pre-create the file here: an idle foreign connection
                # on the not-yet-initialized store blocks the child's WAL
                # setup indefinitely. Watch only what the child created.
                if watch is None:
                    if not child_db.exists():
                        await asyncio.sleep(_POLL_SECONDS)
                        continue
                    watch = sqlite3.connect(str(child_db), timeout=5.0)
                    watch.execute("PRAGMA busy_timeout=2000")
                row = watch.execute(
                    "SELECT COUNT(*) FROM event_content_ceiling_log"
                ).fetchone()
                if row is not None and int(row[0]) >= _SWEEP_LIMIT:
                    receipts_at_kill = int(row[0])
                    break
            except sqlite3.Error:
                pass
            await asyncio.sleep(_POLL_SECONDS)
        if watch is not None:
            watch.close()
        if receipts_at_kill < 0:
            await asyncio.to_thread(_reap, child)
            raise AssertionError("child never produced its first purge tick")
        child.send_signal(signal.SIGKILL)
        await asyncio.to_thread(child.wait, 30)
    finally:
        if child.poll() is None:
            await asyncio.to_thread(_reap, child)
    kill_phase = "unknown"
    if heartbeat_path.exists():
        heartbeats = [
            json.loads(line)
            for line in heartbeat_path.read_text().splitlines()
            if line.strip()
        ]
        if heartbeats:
            child_root = next(
                (row.get("root") for row in heartbeats if row.get("phase") == "source"),
                None,
            )
            assert (
                child_root == package_root
            ), "interrupt child imported a different MEDRE revision"
            if heartbeats[-1]["phase"] == "tick_start":
                kill_phase = "mid_tick"
            else:
                kill_phase = "between_ticks"
    consistency, footprint = await _resume_to_completion(child_db)
    cancelled_phase = await _cancel_mid_tick(destination)
    return {
        "load": "interrupt-restart",
        "condition": "enabled_only",
        "receipts_at_kill": receipts_at_kill,
        "kill_phase": kill_phase,
        "consistency": consistency,
        "footprint_after_resume": footprint,
        "cancel_phase": cancelled_phase,
    }


async def _cancel_mid_tick(destination: Path) -> dict[str, Any]:
    """Cancel the worker mid-tick and measure teardown, then resume."""
    context = _open_run(destination, "cancel-mid-tick")
    store = context.store
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        resources.callback(context.restore_probes)
        await store.initialize()
        await _bulk_seed_events(
            store,
            prefix="cm-target",
            count=_CANCEL_TARGETS,
            hard_ceiling=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        context.install_probes()
        dispatcher = _make_dispatcher(context, sweep_enabled=True)
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()
        await _poll_until(
            context.ceiling_probe.tick_active,
            timeout=60.0,
            description="no sweep tick started before cancellation",
        )
        stop_started = time.perf_counter()
        await dispatcher.stop()
        stop_seconds = time.perf_counter() - stop_started
        cancelled_in_tick = any(
            row["type"] == "CancelledError" for row in context.ceiling_probe.errors
        )
        enforced_before_cancel = context.ceiling_probe.enforced_total
        context.restore_probes()
        raw = _raw_counts(context.db_path)
        if raw["integrity_check"] != "ok":
            raise AssertionError(f"integrity check failed after cancel: {raw}")
        if raw["receipt_with_body_rows"] or raw["cleared_envelope_without_receipt"]:
            raise AssertionError(f"partial purge state after cancel: {raw}")
        # Reopen a genuinely new SQLiteStorage instance to test persisted
        # cursor loading, not only replay on the original in-memory store.
        await store.close()
        reopened = SQLiteStorage(str(context.db_path))
        resources.push_async_callback(reopened.close)
        await reopened.initialize()
        for _ in range(400):
            enforced = await reopened.enforce_content_ceilings(limit=500)
            if not enforced:
                break
        audit = await reopened.audit_content_ceilings(limit=25)
        if audit["elapsed_pending"] or audit["residual_enforced"]:
            raise AssertionError("reopened sweep after cancel left findings")
        verification = await _verify_all_receipts(reopened)
        assert verification["receipts_failed"] == 0
        assert verification["receipts_examined"] == _CANCEL_TARGETS
        return {
            "stop_seconds_with_tick_active": round(stop_seconds, 3),
            "cancelled_in_tick_observed": cancelled_in_tick,
            "ticks_observed_before_cancel": len(context.ceiling_probe.tick_ms),
            "enforced_before_cancel": enforced_before_cancel,
            "raw_after_cancel": raw,
            "verification": verification,
            "footprint_after_resume": _footprint(context.db_path),
        }


def _source_identity() -> dict[str, Any]:
    """Record which source tree and revision produced the numbers."""
    import medre

    package_root = Path(medre.__file__).resolve().parent.parent
    commit = None
    dirty = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=package_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        commit = result.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=package_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        dirty = bool(status.stdout.strip())
    except Exception:
        commit = None
        dirty = None
    return {
        "package_root": str(package_root),
        "git_commit": commit,
        "git_dirty": dirty,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
    }


async def _run_condition(
    load: str, destination: Path, *, sweep_enabled: bool, repeat: int
) -> dict[str, Any]:
    if load == "history-heavy":
        return await _run_history_heavy(
            destination, sweep_enabled=sweep_enabled, repeat=repeat
        )
    if load == "continuous-stream":
        return await _run_continuous_stream(
            destination, sweep_enabled=sweep_enabled, repeat=repeat
        )
    if load == "fanout-attachments":
        return await _run_fanout_attachments(
            destination, sweep_enabled=sweep_enabled, repeat=repeat
        )
    if load == "blocked-writer":
        return await _run_blocked_writer(
            destination, sweep_enabled=sweep_enabled, repeat=repeat
        )
    raise ValueError(f"unsupported load: {load}")


async def measure_ceiling_sweep(
    destination: Path,
    *,
    loads: list[str],
    repeats: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run each requested load's conditions and return per-run results."""
    repeat_counts = dict(_LOAD_REPEATS)
    if repeats:
        repeat_counts.update(repeats)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for load in loads:
        if load == "interrupt-restart":
            results[load] = await _run_interrupt_restart(destination)
            continue
        runs: list[dict[str, Any]] = []
        for repeat in range(repeat_counts[load]):
            runs.append(
                await _run_condition(
                    load, destination, sweep_enabled=False, repeat=repeat
                )
            )
            runs.append(
                await _run_condition(
                    load, destination, sweep_enabled=True, repeat=repeat
                )
            )
        results[load] = {"repeats": repeat_counts[load], "runs": runs}
    return {
        "qualification": "synthetic_ceiling_sweep_pressure_no_native_io",
        "source_identity": _source_identity(),
        "loads": results,
        "limitations": [
            "fake handoffs only; no transport SDK, native send or recipient "
            "delivery participates in any load",
            "history rows are bulk direct writes shaped like producer rows; "
            "stream and trickle admissions use the real producer boundary",
            "the blocked writer is an external raw connection, not a real "
            "slow disk or network filesystem",
            "the SIGKILL restart models process death, not power loss or "
            "disk corruption",
            "logical purge is not physical erasure; a receipt is not a "
            "WAL-erasure certificate; WAL and DB byte footprints only grow "
            "in these runs",
            "timings are measurement data on one development machine; no "
            "machine-speed gate and no capacity promise",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--loads",
        nargs="+",
        choices=_LOADS,
        default=list(_LOADS),
        help="subset of loads to run",
    )
    parser.add_argument(
        "--interrupt-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.interrupt_child:
        _interrupt_child(args.destination.resolve() / "interrupt-child-state")
        return 0
    print(
        json.dumps(
            asyncio.run(measure_ceiling_sweep(args.destination, loads=args.loads)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
