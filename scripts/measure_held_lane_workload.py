"""Held-lane durable dispatch measurement, not native-transport qualification.

Runs the production durable-runtime assembly (real SQLite, durable ingress
admission, background RetryWorker with its lifetime task group, capacity
controller) twice on fresh disposable state:

* ``retry_enabled_default_cadence`` — default capacity and polling cadence,
  with retries enabled and the explicit overrides reported below.
* ``constrained`` — one deliberately constrained profile where the global
  delivery limit leaves exactly one spare slot beside the held operation.

``--sweep-comparison`` instead runs one fixed held-lane workload twice,
differing only in the opt-in ``retry.deadline_sweep`` policy: the fixed
workload carries parked pending obligations whose content ceiling elapses
mid-run, created through the real producer boundary (admission installs the
ceiling and adoption stages the obligation), unclaimable behind a future
``next_attempt_at`` so no dispatcher races the sweep. The comparison records
claim/sweep elapsed timings, durable-admission writer latency during active
work, healthy-lane progress, rejected capacity starts, sweep retirement
counts and DB/WAL footprints at matching phases.

One destination handoff parks behind an explicit barrier at the fake adapter
boundary for the whole measurement. Fresh healthy work admitted during the
hold must complete; the held row must never reach a terminal state while the
barrier is held, and a before-start refusal must recover through its durable
cooldown. The report compares retained terminal-history sizes and healthy
versus held lanes, and records claim/count query timings, round
progress, observed healthy-lane completion latency, worker bookkeeping, and DB/WAL
footprints. It is a measurement, not a performance pass/fail verdict, and
fake handoff observations prove local acceptance only — never recipient
delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import subprocess
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import DeadlineSweepConfig, RetryConfig, RuntimeLimits
from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from medre.runtime.retry import RetryWorker

_MAX_HISTORY_ROWS = 5000
_SEED_CLAIM_BATCH = 50
_FINAL_QUERY_SAMPLES = 25
_POLL_SECONDS = 0.005

_HEALTHY_LANES = ("lane-a", "lane-b", "lane-c")
_HELD_LANE = "held"
_SETTLER_LANE = "settler"
_REFUSER_LANE = "refuser"
_ROOM_CHANNEL = "room"
_SEED_CHANNEL = "seed"
_SOLO_CHANNEL = "solo"
_EXPIRED_CHANNEL = "expired"
_EXPIRED_LANE = "expired-pending"
#: Expired obligations park one hour past the run so no claim path, which
#: always requires ``next_attempt_at IS NULL OR <= now``, can race the sweep.
_EXPIRED_PARK_SECONDS = 3600
#: The parked obligations' content ceiling elapses this many seconds into the
#: run, so the sweep observes a real durable expiry during active work.
_EXPIRED_CEILING_SECONDS = 2
_SWEEP_INTERVAL_SECONDS = 0.5
_SWEEP_LIMIT = 50
_SWEEP_EXPIRED_ROWS = 60


class _SweepProbe:
    """Times the storage deadline sweep the worker captured at construction."""

    def __init__(self, store: SQLiteStorage) -> None:
        self._store = store
        self._original = store.retire_expired_unstarted_outbox
        self.elapsed_ms: list[float] = []
        self.retired_total = 0

    def install(self) -> None:
        probe = self

        async def timed_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            started = time.perf_counter()
            retired = await probe._original(*args, **kwargs)
            probe.elapsed_ms.append((time.perf_counter() - started) * 1000)
            probe.retired_total += len(retired)
            return retired

        self._store.retire_expired_unstarted_outbox = timed_sweep  # type: ignore[method-assign]


def _percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * pct + 99) // 100 - 1)], 3)


def _timings(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    return {
        "p50_ms": _percentile(samples, 50),
        "p95_ms": _percentile(samples, 95),
        "p99_ms": _percentile(samples, 99),
        "max_ms": _percentile(samples, 100),
    }


class _LaneTracker:
    """Sampler for fake native handoff concurrency."""

    def __init__(self) -> None:
        self._current = 0
        self.max_concurrent = 0
        self._current_by_adapter: dict[str, int] = {}
        self.max_by_adapter: dict[str, int] = {}

    @property
    def current(self) -> int:
        """Number of synthetic handoffs currently at the adapter boundary."""
        return self._current

    def enter(self, adapter_id: str) -> None:
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        current = self._current_by_adapter.get(adapter_id, 0) + 1
        self._current_by_adapter[adapter_id] = current
        self.max_by_adapter[adapter_id] = max(
            self.max_by_adapter.get(adapter_id, 0), current
        )

    def exit(self, adapter_id: str) -> None:
        self._current -= 1
        self._current_by_adapter[adapter_id] -= 1


class _HeldAdapter(FakePresentationAdapter):
    """Fake destination whose native handoff parks on an explicit barrier."""

    def __init__(
        self,
        adapter_id: str,
        tracker: _LaneTracker,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker
        self.entered = entered
        self._release = release

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.entered.set()
        self._tracker.enter(self.adapter_id)
        try:
            await self._release.wait()
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


class _TrackedAdapter(FakePresentationAdapter):
    """Healthy fake destination that records its handoff concurrency."""

    def __init__(self, adapter_id: str, tracker: _LaneTracker) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self._tracker.enter(self.adapter_id)
        try:
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


class _CooldownRefusalAdapter(FakePresentationAdapter):
    """Refuses once before native acceptance, then accepts.

    Exercises the initial retry/cooldown path through the background
    dispatcher alongside the held lane.
    """

    def __init__(self, adapter_id: str, tracker: _LaneTracker) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker
        self.deliver_calls = 0

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self._tracker.enter(self.adapter_id)
        try:
            self.deliver_calls += 1
            if self.deliver_calls == 1:
                raise AdapterSendError(
                    "destination refused before native handoff",
                    retry_after_seconds=1.0,
                )
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


class _QueryProbe:
    """Times dispatcher claim and status-count queries against the store."""

    def __init__(self, store: SQLiteStorage) -> None:
        self._store = store
        self._original_claim = store.claim_due_outbox_items
        self._original_count = store.count_outbox_by_status
        self.claim_nonempty_ms: list[float] = []
        self.claim_empty_ms: list[float] = []
        self.count_ms: list[float] = []
        self.reader_epoch = 0
        self.overlap_ms: dict[str, list[float]] = {
            "claim_nonempty": [],
            "claim_empty": [],
            "count": [],
        }

    def install(self) -> None:
        probe = self

        async def timed_claim(*args: Any, **kwargs: Any) -> list[Any]:
            epoch = probe.reader_epoch
            started = time.perf_counter()
            batch = await probe._original_claim(*args, **kwargs)
            duration = (time.perf_counter() - started) * 1000
            if epoch % 2 or epoch != probe.reader_epoch:
                probe.overlap_ms["claim_nonempty" if batch else "claim_empty"].append(
                    duration
                )
            elif batch:
                probe.claim_nonempty_ms.append(duration)
            else:
                probe.claim_empty_ms.append(duration)
            return batch

        async def timed_count() -> dict[str, int]:
            epoch = probe.reader_epoch
            started = time.perf_counter()
            counts = await probe._original_count()
            samples = (
                probe.overlap_ms["count"]
                if epoch % 2 or epoch != probe.reader_epoch
                else probe.count_ms
            )
            samples.append((time.perf_counter() - started) * 1000)
            return counts

        self._store.claim_due_outbox_items = timed_claim  # type: ignore[method-assign]
        self._store.count_outbox_by_status = timed_count  # type: ignore[method-assign]

    def restore(self) -> None:
        self._store.claim_due_outbox_items = self._original_claim  # type: ignore[method-assign]
        self._store.count_outbox_by_status = self._original_count  # type: ignore[method-assign]


@dataclass
class _Profile:
    """One deliberately named dispatch/capacity configuration."""

    name: str
    delivery_limit: int
    batch_size: int
    interval_seconds: float | None
    retries_enabled: bool
    healthy_batches: int
    events_per_batch: int
    expired_rows: int = 0
    deadline_sweep_enabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _pipeline(store: SQLiteStorage, adapters: dict[str, Any]) -> PipelineRunner:
    """Production-shaped pipeline assembly for the measurement routes."""
    fanout = Route(
        id="measure-fanout",
        source=RouteSource(
            adapter="synthetic",
            channel=_ROOM_CHANNEL,
            event_kinds=("message.created",),
        ),
        targets=[
            RouteTarget(adapter=name, channel="out")
            for name in (_HELD_LANE, *_HEALTHY_LANES)
        ],
    )
    solo = Route(
        id="measure-solo",
        source=RouteSource(
            adapter="synthetic",
            channel=_SOLO_CHANNEL,
            event_kinds=("message.created",),
        ),
        targets=[RouteTarget(adapter=_REFUSER_LANE, channel="out")],
    )
    seed = Route(
        id="measure-seed",
        source=RouteSource(
            adapter="synthetic",
            channel=_SEED_CHANNEL,
            event_kinds=("message.created",),
        ),
        targets=[RouteTarget(adapter=_SETTLER_LANE, channel="out")],
    )
    config = PipelineConfig(
        storage=store,  # type: ignore[arg-type]
        router=Router(routes=[fanout, solo, seed]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=EventBus(),
    )
    config.route_retry_policies[fanout.id] = RetryPolicy(jitter=False)
    config.route_retry_policies[solo.id] = RetryPolicy(jitter=False)
    config.route_retry_policies[seed.id] = RetryPolicy(jitter=False)
    return PipelineRunner(config)


def _event(event_id: str, channel: str) -> CanonicalEvent:
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
        payload={"body": f"held-lane measurement {event_id} " + "x" * 64},
        metadata=EventMetadata(),
        source_native_ref=NativeRef(
            adapter="synthetic",
            native_channel_id=channel,
            native_message_id=event_id,
        ),
    )


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


async def _work_status_probe(db_path: str, *, samples: int = 10) -> dict[str, Any]:
    """Time full read-only work-status samples over the retained history.

    Each sample is one complete operator-view read: the storage summary
    over one consistent read snapshot plus assembly of the report document,
    exactly as ``medre inspect work-status`` performs it. Observations at
    reproducible history sizes, not machine-speed assertions.
    """
    from medre.core.observability.work_status import assemble_work_status_report

    query_ms: list[float] = []
    readonly = await SQLiteStorage.open_readonly(db_path)
    try:
        for _ in range(samples):
            started = time.perf_counter()
            summary = await readonly.query_work_status_summary()
            assemble_work_status_report(
                sampled_at=summary["sampled_at"],
                adapter_filter=None,
                adapter_rows=summary["adapter_rows"],
                oldest_rows=summary["oldest_rows"],
                raw_status_rows=summary["raw_status_rows"],
                current_generations_total=summary["current_generations_total"],
                receipts_by_status=summary["receipts_by_status"],
            )
            query_ms.append((time.perf_counter() - started) * 1000)
    finally:
        await readonly.close()
    return {
        "summary_and_assembly_ms": _timings(query_ms),
        "sample_count": len(query_ms),
    }


async def _stage(
    store: SQLiteStorage,
    runner: PipelineRunner,
    event_id: str,
    channel: str,
    *,
    admission_ms: list[float] | None = None,
) -> None:
    """Admit one event durably and stage its outbox obligations."""
    started = time.perf_counter()
    admitted = await runner.admit_ingress(_event(event_id, channel), "live")
    if not admitted.created or admitted.event_id != event_id:
        raise RuntimeError(f"{event_id}: expected a new durable admission")
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    processed = await ingress.run_once()
    if admission_ms is not None:
        admission_ms.append((time.perf_counter() - started) * 1000)
    if processed != 1:
        raise RuntimeError(f"{event_id}: expected one ingress item, got {processed}")


async def _stage_expired_rows(
    store: SQLiteStorage,
    *,
    rows: int,
    admission_ms: list[float] | None = None,
) -> datetime:
    """Stage pending obligations whose ceiling elapses during the run.

    Each row is created through the real producer boundary: admission installs
    the immutable ceiling and the source hold a few seconds into the future
    (an already-elapsed deadline is refused at admission, exactly as the
    producer contract requires), adoption stages the obligation, and the
    future ``next_attempt_at`` parks the pending row past every claim
    predicate so no dispatcher cycle can race the sweep. The sweep observes
    the durable expiry mid-run and is the only path that may settle them.
    """
    ceiling_at = datetime.now(timezone.utc)
    parked_until = datetime.now(timezone.utc) + timedelta(seconds=_EXPIRED_PARK_SECONDS)
    for index in range(rows):
        event_id = f"expired-{index:04d}"
        started = time.perf_counter()
        ceiling_at = datetime.now(timezone.utc) + timedelta(
            seconds=_EXPIRED_CEILING_SECONDS
        )
        # History provenance suppresses the durable work marker so the
        # ingress worker's FIFO queue only ever carries the measured events;
        # the ceiling rows and source hold commit exactly as production
        # writes them.
        admitted = await store.admit_ingress(
            _event(event_id, _EXPIRED_CHANNEL),
            None,
            "history",
            content_deadline=ceiling_at,
        )
        if not admitted.created or admitted.event_id != event_id:
            raise RuntimeError(f"{event_id}: expected new ceiling-bearing admission")
        await store.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id=f"obox-{event_id}",
                event_id=event_id,
                delivery_plan_id="plan-expired-measure",
                route_id="measure-expired",
                target_adapter=_EXPIRED_LANE,
                target_channel="out",
                status="pending",
                attempt_number=0,
                next_attempt_at=parked_until.isoformat(),
            )
        )
        if admission_ms is not None:
            admission_ms.append((time.perf_counter() - started) * 1000)
    return ceiling_at


async def _seed_terminal_history(
    store: SQLiteStorage,
    runner: PipelineRunner,
    *,
    history_rows: int,
    adapter: FakePresentationAdapter,
) -> int:
    """Retain ``history_rows`` terminal rows through guarded transitions."""
    # Exercise the real initial-attempt path, including receipt and native-ref
    # creation, rather than stamping attempt-zero rows as sent without evidence.
    # This one-shot worker owns no background task; seed timings are excluded.
    settler = RetryWorker(
        store,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        batch_size=_SEED_CLAIM_BATCH,
    )
    for start in range(0, history_rows, _SEED_CLAIM_BATCH):
        stop = min(start + _SEED_CLAIM_BATCH, history_rows)
        event_ids = [f"seed-{index:06d}" for index in range(start, stop)]
        for event_id in event_ids:
            await _stage(store, runner, event_id, _SEED_CHANNEL)
        await settler._process_due(datetime.now(timezone.utc))
        for event_id in event_ids:
            rows = await store.list_outbox_items_for_event(event_id, metadata_only=True)
            if (
                len(rows) != 1
                or rows[0].target_adapter != _SETTLER_LANE
                or rows[0].status != "sent"
                or rows[0].attempt_number != 1
                or not rows[0].receipt_id
            ):
                raise AssertionError(
                    f"seed {event_id}: missing settled attempt evidence"
                )
        # Claim order is not part of this measurement contract.
        if sorted(p.event_id for p in adapter.delivered_payloads) != event_ids:
            raise AssertionError("seed handoffs do not match the staged event set")
        adapter.delivered_payloads.clear()
    return history_rows


async def _run_profile(
    destination: Path,
    profile: _Profile,
    *,
    history_rows: int,
) -> dict[str, Any]:
    """One profile run: seed history, hold a lane, admit fresh work, measure."""
    db_path = destination / f"held-{profile.name}.sqlite"
    if any(destination.glob(db_path.name + "*")):
        raise FileExistsError("measurement requires fresh disposable SQLite state")
    store = SQLiteStorage(str(db_path))
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        await store.initialize()
        footprint_at_close: dict[str, int] = {}

        def record_pre_close_footprint() -> None:
            footprint_at_close.update(
                {
                    path.name: path.stat().st_size
                    for path in destination.glob(db_path.name + "*")
                    if path.is_file()
                }
            )

        resources.callback(record_pre_close_footprint)
        tracker = _LaneTracker()
        held_entered, held_release = asyncio.Event(), asyncio.Event()
        held = _HeldAdapter(_HELD_LANE, tracker, held_entered, held_release)
        healthy = {name: _TrackedAdapter(name, tracker) for name in _HEALTHY_LANES}
        settler = FakePresentationAdapter(adapter_id=_SETTLER_LANE)
        refuser = _CooldownRefusalAdapter(_REFUSER_LANE, tracker)
        adapters = {
            _HELD_LANE: held,
            **healthy,
            _SETTLER_LANE: settler,
            _REFUSER_LANE: refuser,
        }
        runner = _pipeline(store, adapters)
        limits = RuntimeLimits(
            max_inflight_deliveries=profile.delivery_limit,
            delivery_acquire_timeout_seconds=0.05,
        )
        capacity = CapacityController(limits)
        runner.set_capacity_controller(capacity)
        resources.push_async_callback(runner.stop)
        await runner.start()

        seeded = await _seed_terminal_history(
            store, runner, history_rows=history_rows, adapter=settler
        )

        probe = _QueryProbe(store)
        resources.callback(probe.restore)
        probe.install()
        # The sweep probe must wrap the storage method before the worker is
        # constructed: the poll cycle captures the sweep surface once.
        sweep_probe = _SweepProbe(store)
        sweep_probe.install()
        dispatcher = RetryWorker(
            store,
            runner,
            capacity,
            initial_dispatch=True,
            enabled=profile.retries_enabled,
            interval_seconds=profile.interval_seconds,
            batch_size=profile.batch_size,
            max_attempts=4,
            stop_timeout_seconds=0.5,
            retry_config=RetryConfig(
                deadline_sweep=DeadlineSweepConfig(
                    enabled=profile.deadline_sweep_enabled,
                    limit=_SWEEP_LIMIT,
                    interval_seconds=_SWEEP_INTERVAL_SECONDS,
                )
            ),
        )
        dispatcher.set_available_target_adapters(adapters.keys())
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()

        round_metrics: list[dict[str, Any]] = []
        healthy_rows_completed = 0
        healthy_observed_latency_ms: list[float] = []
        admission_ms: list[float] = []
        expected_total_rows = 0
        # Stage the held event before admitting fresh fanout work: one lane parks
        # behind the barrier while its healthy fanout completes immediately.
        await _stage(store, runner, "held-1", _ROOM_CHANNEL, admission_ms=admission_ms)
        await _poll_until(
            held_entered.is_set,
            timeout=10.0,
            description="held lane never entered its handoff",
        )
        held_rows = await store.list_outbox_items_for_event(
            "held-1", metadata_only=True
        )
        held_row = next(r for r in held_rows if r.target_adapter == _HELD_LANE)
        if held_row.status != "in_progress" or held_row.active_attempt != 1:
            raise AssertionError(
                f"held row state unexpected: {held_row.status}/"
                f"{held_row.active_attempt}"
            )

        # Stage while the real dispatcher is active, with a fresh producer
        # ceiling per row; slow staging cannot exhaust a shared setup clock.
        expired_admission_ms: list[float] = []
        expired_at = await _stage_expired_rows(
            store, rows=profile.expired_rows, admission_ms=expired_admission_ms
        )

        for round_index in range(profile.healthy_batches):
            staged_at = time.perf_counter()
            round_rows: list[tuple[str, str, str]] = []
            admission_started: dict[str, float] = {}
            for event_index in range(profile.events_per_batch):
                event_id = f"fresh-{round_index:02d}-{event_index:02d}"
                admission_started[event_id] = time.perf_counter()
                await _stage(
                    store, runner, event_id, _ROOM_CHANNEL, admission_ms=admission_ms
                )
                rows = await store.list_outbox_items_for_event(
                    event_id, metadata_only=True
                )
                if len(rows) != len(healthy) + 1 or {
                    r.target_adapter for r in rows
                } != {_HELD_LANE, *healthy}:
                    raise AssertionError(f"{event_id}: unexpected fanout obligations")
                for row in rows:
                    if row.target_adapter in healthy:
                        round_rows.append((event_id, row.target_adapter, row.outbox_id))
            expected_total_rows += len(round_rows)
            staging_done = time.perf_counter()
            first_progress_ms: float | None = None
            sent_at: dict[str, float] = {}
            deadline = time.perf_counter() + 30.0
            while len(sent_at) < len(round_rows):
                if time.perf_counter() >= deadline:
                    raise AssertionError(
                        f"round {round_index}: healthy rows did not complete "
                        f"while the held lane stayed parked"
                    )
                for event_id, _adapter_id, outbox_id in round_rows:
                    if outbox_id in sent_at:
                        continue
                    rows = await store.list_outbox_items_for_event(
                        event_id, metadata_only=True
                    )
                    row = next(r for r in rows if r.outbox_id == outbox_id)
                    if (
                        row.status in {"in_progress", "sent"}
                        and first_progress_ms is None
                    ):
                        first_progress_ms = (time.perf_counter() - staged_at) * 1000
                    if row.status == "sent":
                        sent_at[outbox_id] = time.perf_counter()
                await asyncio.sleep(_POLL_SECONDS)
            healthy_rows_completed += len(sent_at)
            healthy_observed_latency_ms.extend(
                (sent_at[outbox_id] - admission_started[event_id]) * 1000
                for event_id, _, outbox_id in round_rows
            )
            round_metrics.append(
                {
                    "round": round_index,
                    "healthy_rows": len(round_rows),
                    "staging_ms": round((staging_done - staged_at) * 1000, 3),
                    "first_observed_progress_ms": (
                        round(first_progress_ms, 3)
                        if first_progress_ms is not None
                        else None
                    ),
                    "round_completion_ms": round(
                        (max(sent_at.values()) - staged_at) * 1000, 3
                    ),
                }
            )

        # One cooldown refusal through the background dispatcher while the
        # held lane stays parked: a retry_wait row must recover to sent.
        calls_before = refuser.deliver_calls
        await _stage(
            store, runner, "cooldown-solo", _SOLO_CHANNEL, admission_ms=admission_ms
        )

        async def cooldown_row_sent() -> bool:
            rows = await store.list_outbox_items_for_event(
                "cooldown-solo", metadata_only=True
            )
            return bool(rows) and rows[0].status == "sent"

        await _poll_until(
            cooldown_row_sent,
            timeout=45.0,
            description="cooldown refusal never recovered through its schedule",
        )
        if refuser.deliver_calls != calls_before + 2:
            raise AssertionError(
                f"cooldown row reported sent after {refuser.deliver_calls} "
                f"handoff calls (expected a refusal plus a re-dispatch)"
            )

        # Sweep accounting: the enabled policy retires every expired parked
        # obligation; the disabled policy leaves all of them pending. The
        # probe wraps the exact surface the worker captured, so its counts
        # and the worker's own sweep counters must agree.
        await _poll_until(
            lambda: datetime.now(timezone.utc) >= expired_at,
            timeout=_EXPIRED_CEILING_SECONDS + 10,
            description="last parked ceiling did not elapse",
        )
        if profile.deadline_sweep_enabled:
            await _poll_until(
                lambda: sweep_probe.retired_total == profile.expired_rows
                and dispatcher.state.deadline_retired == profile.expired_rows,
                timeout=30.0,
                description="deadline sweep did not retire all parked rows",
            )
        retired_expired = sweep_probe.retired_total
        if profile.deadline_sweep_enabled:
            if retired_expired != profile.expired_rows:
                raise AssertionError(
                    f"sweep retired {retired_expired} of "
                    f"{profile.expired_rows} expired obligations"
                )
            if dispatcher.state.deadline_retired != retired_expired:
                raise AssertionError(
                    "worker sweep counters disagree with the timed surface: "
                    f"{dispatcher.state.deadline_retired} != {retired_expired}"
                )
            if not sweep_probe.elapsed_ms:
                raise AssertionError("enabled sweep never ran")
        else:
            if retired_expired or dispatcher.state.deadline_retired:
                raise AssertionError(
                    "disabled policy invoked the deadline sweep surface"
                )

        # Logical accounting: healthy lanes terminal exactly once, the held
        # row never terminal behind the barrier, and lane exclusivity intact.
        counts_now = await probe._original_count()
        # Terminal accounting: seeded history + round rows + the held event's
        # healthy fanout + the recovered cooldown row. Retired expired
        # obligations settle as abandoned; unretired ones stay parked pending.
        expected_sent = seeded + expected_total_rows + len(healthy) + 1
        expected_counts: dict[str, int] = {
            "sent": expected_sent,
            "in_progress": 1,
            "pending": (
                profile.healthy_batches * profile.events_per_batch
                + profile.expired_rows
                - retired_expired
            ),
        }
        if retired_expired:
            expected_counts["abandoned"] = retired_expired
        if counts_now != expected_counts:
            raise AssertionError(
                f"unexpected terminal/active accounting: {counts_now}; "
                f"expected {expected_counts}"
            )
        if healthy_rows_completed != expected_total_rows:
            raise AssertionError(
                f"healthy completion mismatch: {healthy_rows_completed} != "
                f"{expected_total_rows}"
            )
        if held.delivered_payloads:
            raise AssertionError("held destination accepted a handoff")
        held_rows = await store.list_outbox_items_for_event(
            "held-1", metadata_only=True
        )
        held_row = next(r for r in held_rows if r.target_adapter == _HELD_LANE)
        if held_row.status != "in_progress" or held_row.active_attempt != 1:
            raise AssertionError("original held attempt no longer owns its handoff")
        await _poll_until(
            lambda: set(dispatcher._active) == {_HELD_LANE}
            and capacity.delivery_current == 1
            and dispatcher.outbox_counts == counts_now,
            timeout=10.0,
            description="worker bookkeeping did not settle to the held lane",
        )
        delivery_current_while_held = capacity.delivery_current
        # Operator-view cost during a settled hold, after healthy deliveries
        # and the cooldown retry finish. Concurrent commits are not required.
        probe.reader_epoch += 1
        try:
            work_status_during_hold = await _work_status_probe(str(db_path))
        finally:
            probe.reader_epoch += 1
        for name, adapter in healthy.items():
            expected_handoffs = profile.healthy_batches * profile.events_per_batch + 1
            if len(adapter.delivered_payloads) != expected_handoffs:
                raise AssertionError(
                    f"{name}: unexpected handoff count "
                    f"{len(adapter.delivered_payloads)} != {expected_handoffs}"
                )
        if tracker.max_concurrent > profile.delivery_limit:
            raise AssertionError(
                f"concurrent handoffs {tracker.max_concurrent} exceeded the "
                f"configured delivery limit {profile.delivery_limit}"
            )
        if any(v != 1 for v in tracker.max_by_adapter.values()):
            raise AssertionError(
                f"per-adapter lane exclusivity broken: {tracker.max_by_adapter}"
            )

        # Explicit final query samples against the retained history size.
        final_claim_ms: list[float] = []
        final_count_ms: list[float] = []
        for _ in range(_FINAL_QUERY_SAMPLES):
            started = time.perf_counter()
            batch = await probe._original_claim(
                datetime.now(timezone.utc).isoformat(),
                "final-probe",
                lease_seconds=300,
                limit=min(profile.batch_size, profile.delivery_limit - 1),
                excluded_adapters=(_HELD_LANE,),
                one_per_adapter=True,
            )
            final_claim_ms.append((time.perf_counter() - started) * 1000)
            if batch:
                raise AssertionError(
                    f"final probe claimed {len(batch)} rows; nothing should "
                    f"be claimable while the held lease is unexpired"
                )
            started = time.perf_counter()
            await probe._original_count()
            final_count_ms.append((time.perf_counter() - started) * 1000)

        footprint_before_close = {
            path.name: path.stat().st_size
            for path in destination.glob(db_path.name + "*")
            if path.is_file()
        }
        worker_state = {
            "processed": dispatcher.state.processed,
            "succeeded": dispatcher.state.succeeded,
            "failed": dispatcher.state.failed,
            "dead_lettered": dispatcher.state.dead_lettered,
        }
        outbox_counts = dispatcher.outbox_counts
    if (
        dispatcher.state.abandoned
        or tracker.current != 0
        or capacity.delivery_current != 0
    ):
        raise AssertionError("synthetic dispatcher did not retire its held operation")
    # Capture writer-close footprint before a WAL reader can create sidecars.
    footprint_after_close = {
        path.name: path.stat().st_size
        for path in destination.glob(db_path.name + "*")
        if path.is_file()
    }
    # Operator-view cost at rest over fully retained history; no writer runs.
    work_status_at_rest = await _work_status_probe(str(db_path))

    return {
        "profile": profile.name,
        "config": {
            "delivery_limit": profile.delivery_limit,
            "held_lane_occupies": delivery_current_while_held,
            "spare_delivery_slots": profile.delivery_limit - 1,
            "dispatch_batch_size": profile.batch_size,
            "dispatch_interval_seconds": (
                profile.interval_seconds
                if profile.interval_seconds is not None
                else RetryConfig().interval_seconds
            ),
            "retries_enabled": profile.retries_enabled,
            "worker_fallback_max_attempts": 4,
            "route_retry_policy": asdict(RetryPolicy(jitter=False)),
            "runtime_limits": asdict(limits),
            "delivery_acquire_timeout_seconds": 0.05,
            "dispatcher_stop_timeout_seconds": 0.5,
            "initial_dispatch": True,
            "healthy_batches": profile.healthy_batches,
            "events_per_batch": profile.events_per_batch,
            "history_rows_seeded": history_rows,
            "expired_rows_staged": profile.expired_rows,
            "deadline_sweep_enabled": profile.deadline_sweep_enabled,
            **profile.extra,
        },
        "terminal_history_retained": {
            "seeded_sent_rows": seeded,
            "outbox_counts_after_run": counts_now,
            "dispatcher_snapshot_counts": outbox_counts,
        },
        "healthy_vs_held": {
            "healthy_rows_completed": healthy_rows_completed,
            "healthy_rows_expected": (
                profile.healthy_batches * profile.events_per_batch * len(healthy)
            ),
            "held_rows_completed_during_hold": 0,
            "held_row_status": held_row.status,
            "held_row_reserved_attempt": held_row.active_attempt,
            "cooldown_refusals_recovered": refuser.deliver_calls - calls_before - 1,
        },
        "writer_wait": {
            "note": (
                "durable admission latency (runner ingress plus staging commit) "
                "sampled per fresh event during active work; parked ceiling "
                "staging is reported separately"
            ),
            "fresh_admission_ms": {
                **_timings(admission_ms),
                "sample_count": len(admission_ms),
            },
            "expired_staging_ms": {
                **_timings(expired_admission_ms),
                "sample_count": len(expired_admission_ms),
            },
        },
        "deadline_sweep": {
            "enabled": profile.deadline_sweep_enabled,
            "interval_seconds": _SWEEP_INTERVAL_SECONDS,
            "limit": _SWEEP_LIMIT,
            "expired_rows_staged": profile.expired_rows,
            "expired_ceiling_at": expired_at.isoformat(),
            "observed_sweep_invocations": len(sweep_probe.elapsed_ms),
            "retired_total": retired_expired,
            "sweep_elapsed_ms": {
                **_timings(sweep_probe.elapsed_ms),
                "sample_count": len(sweep_probe.elapsed_ms),
            },
            "worker_state": {
                "runs": dispatcher.state.deadline_sweep_runs,
                "errors": dispatcher.state.deadline_sweep_errors,
                "retired": dispatcher.state.deadline_retired,
            },
        },
        "healthy_lane_completion": {
            "observed_completion_latency_ms": _timings(healthy_observed_latency_ms),
            "sample_count": len(healthy_observed_latency_ms),
        },
        "round_progress": {
            "rounds": round_metrics,
            "first_observed_progress_ms": _timings(
                [
                    r["first_observed_progress_ms"]
                    for r in round_metrics
                    if r["first_observed_progress_ms"] is not None
                ]
            ),
        },
        "claim_query_timings": {
            "dispatcher_nonempty": {
                **_timings(probe.claim_nonempty_ms),
                "sample_count": len(probe.claim_nonempty_ms),
            },
            "dispatcher_empty": {
                **_timings(probe.claim_empty_ms),
                "sample_count": len(probe.claim_empty_ms),
            },
            "final_empty_probe": {
                **_timings(final_claim_ms),
                "sample_count": len(final_claim_ms),
            },
        },
        "count_query_timings": {
            "dispatcher_refreshes": {
                **_timings(probe.count_ms),
                "sample_count": len(probe.count_ms),
            },
            "final_probe": {
                **_timings(final_count_ms),
                "sample_count": len(final_count_ms),
            },
        },
        "dispatcher_query_timings_overlapping_work_status_probe": {
            kind: {**_timings(samples), "sample_count": len(samples)}
            for kind, samples in probe.overlap_ms.items()
        },
        "work_status_query_timings": {
            "during_hold": work_status_during_hold,
            "at_rest": work_status_at_rest,
        },
        "worker_state": worker_state,
        "capacity_snapshot_while_held": {
            "delivery_limit": capacity.delivery_limit,
            "delivery_current": delivery_current_while_held,
            "rejected_delivery_starts": capacity.snapshot()["delivery_rejections"],
            "max_concurrent_handoffs": tracker.max_concurrent,
            "max_concurrent_by_adapter": dict(sorted(tracker.max_by_adapter.items())),
        },
        "sqlite_file_and_sidecar_bytes_before_close": footprint_before_close,
        "sqlite_file_and_sidecar_bytes_at_close": footprint_at_close,
        "sqlite_file_and_sidecar_bytes_after_close": footprint_after_close,
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


async def measure_held_lane_workload(
    destination: Path,
    *,
    history_rows: int = 120,
    sweep_comparison: bool = False,
) -> dict[str, Any]:
    """Run the default-cadence and constrained retry-enabled profiles.

    With ``sweep_comparison`` the script instead runs one fixed held-lane
    workload twice — the deadline sweep policy disabled and then enabled —
    so every recorded field compares like for like.
    """
    if type(history_rows) is not int or not 0 <= history_rows <= _MAX_HISTORY_ROWS:
        raise ValueError(f"history-rows must be in [0, {_MAX_HISTORY_ROWS}]")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if sweep_comparison:
        common: dict[str, Any] = {
            "delivery_limit": RuntimeLimits().max_inflight_deliveries,
            "batch_size": RetryConfig().batch_size,
            "interval_seconds": _SWEEP_INTERVAL_SECONDS,
            "retries_enabled": True,
            "healthy_batches": 2,
            "events_per_batch": 3,
            "expired_rows": _SWEEP_EXPIRED_ROWS,
        }
        profiles = [
            _Profile(
                name="deadline_sweep_disabled",
                **common,
                extra={
                    "note": (
                        "fixed held-lane workload with the opt-in deadline "
                        "sweep policy disabled; expired obligations stay "
                        "parked pending"
                    ),
                },
            ),
            _Profile(
                name="deadline_sweep_enabled",
                **common,
                deadline_sweep_enabled=True,
                extra={
                    "note": (
                        "the same fixed held-lane workload with the opt-in "
                        "deadline sweep policy enabled at the same cadence"
                    ),
                },
            ),
        ]
    else:
        profiles = [
            _Profile(
                name="retry_enabled_default_cadence",
                delivery_limit=RuntimeLimits().max_inflight_deliveries,
                batch_size=RetryConfig().batch_size,
                interval_seconds=None,
                retries_enabled=True,
                healthy_batches=2,
                events_per_batch=3,
                extra={
                    "note": (
                        "default capacity and cadence; retries and timeouts overridden"
                    ),
                },
            ),
            _Profile(
                name="constrained",
                delivery_limit=2,
                batch_size=2,
                interval_seconds=0.2,
                retries_enabled=True,
                healthy_batches=2,
                events_per_batch=2,
                extra={
                    "note": "the only configured spare slot serves all healthy lanes",
                },
            ),
        ]
    results = {}
    for profile in profiles:
        results[profile.name] = await _run_profile(
            destination, profile, history_rows=history_rows
        )
    return {
        "qualification": "synthetic_held_lane_dispatch_no_native_io",
        "mode": (
            "deadline_sweep_off_on_comparison"
            if sweep_comparison
            else "dispatch_profiles"
        ),
        "source_identity": _source_identity(),
        "history_rows": history_rows,
        "profiles": results,
        "limitations": [
            "no transport SDK or actual native sender; fake handoffs prove "
            "local acceptance, never recipient delivery",
            "the held lane models a stalled destination with an explicit "
            "barrier at the adapter boundary; a real SDK may be "
            "cancellation-resistant where this fake is not",
            "completion latency runs from the start of admission to the first "
            "polling observation of sent; staging and observation delay are included",
            "first observed progress is not an exact claim timestamp or loop lag",
            "terminal history uses real initial dispatch with synthetic handoffs; "
            "seed timings and seed handoffs are excluded from the measured lane sample",
            "expired obligations are synthetic pending rows created through the "
            "real admission and adoption boundary, parked past every claim "
            "predicate with a ceiling that elapses mid-run; the sweep observes "
            "the same producer-installed ceiling rows production would, but no "
            "transport attempted them",
            "timings are measurement data only; no performance pass/fail "
            "verdict and no machine-speed gate",
            "restart recovery semantics are characterized by the four-way "
            "recovery campaign tests, not by this script",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--history-rows", type=int, default=120)
    parser.add_argument(
        "--sweep-comparison",
        action="store_true",
        help=(
            "run one fixed held-lane workload twice, differing only in the "
            "opt-in retry.deadline_sweep policy"
        ),
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                measure_held_lane_workload(
                    args.destination,
                    history_rows=args.history_rows,
                    sweep_comparison=args.sweep_comparison,
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
