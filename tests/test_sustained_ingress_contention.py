"""Sustained multi-source contention qualification of durable ingress.

Drives the bounded admission gate, the durable pre-admission pressure
evidence, and the producer pressure sink simultaneously instead of in
isolation.  Four scenarios, all deterministic (bounded iterations and
condition polling through :func:`tests.helpers.async_utils.wait_until`,
never fixed sleeps):

* simultaneous sources share the gate across grant waves while a
  downstream consumer periodically holds admissions — per-source
  fairness, bounded waiting, and exact outcome accounting;
* a second SQLite connection holding ``BEGIN IMMEDIATE`` blocks the
  pressure flush while admissions proceed — best-effort retention with
  gate-refusal keys never evicted and no exception escaping the gate;
* storage failure across a sustained reporting window — bounded, logged
  producer-telemetry loss, no raised reporter, honest recovery;
* owner-loop liveness while the gate is saturated.

Every persisted pressure row is asserted content-free: adapter identity,
closed-vocabulary outcome, and counters only.  When
``MEDRE_SUSTAINED_INGRESS_REPORT_DIR`` is set, each scenario appends one
content-free JSON measurement (window totals and counts, never payloads,
sender identity, or key material); nothing is written by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.core.contracts.adapter import PRODUCER_PRESSURE_OUTCOMES
from medre.core.contracts.adapter import ProducerPressureReport
from medre.core.ingress.types import DurableIngressDeferredError
from medre.core.storage.sqlite._pressure import pressure_window_start
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.core.supervision.capacity import InboundAdmissionRejected
from medre.runtime import app as _app_module
from tests.helpers.async_utils import wait_until
from tests.helpers.inbound_gate import GateAppDouble as _GateApp
from tests.helpers.inbound_gate import LimitsDouble as _Limits

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_REPORT_DIR_ENV = "MEDRE_SUSTAINED_INGRESS_REPORT_DIR"

#: Grant waves in the simultaneous-source scenario.  Iteration-bounded,
#: so the scenario costs wall time only in the admission timeouts it
#: deliberately triggers.
_WAVES = 6

#: Admission timeout for the sustained gate scenario.  Short enough that
#: a held wave times its queued arrivals out quickly, long enough that a
#: 5 ms condition poll observes the queued state reliably.
_SUSTAINED_TIMEOUT_S = 0.1

#: Sustained reporting sub-windows under injected storage failure.
_FAILURE_WINDOWS = 4

#: Producer staging bound used where the eviction math is asserted.
_FLOODED_PRODUCER_BOUND = 2
_SUSTAINED_PRODUCER_BOUND = 3

#: Liveness bound for owner-loop sentinels under gate saturation.  The
#: controller's own wait quantum elsewhere in this suite is 0.02s and its
#: grant/refusal work is microsecond-scale synchronous bookkeeping, so
#: 2.0s is roughly 100x headroom for a loaded CI runner.  This proves
#: starvation freedom, not a latency SLA.
_SENTINEL_BOUND_S = 2.0
_SENTINEL_ROUNDS = 12

#: Fixed epoch for window-stable assertions; windows advance by explicit
#: clock offsets, never by real elapsed time.
_BASE_EPOCH = 1_800_000_000

_PRESSURE_ROW_FIELDS = frozenset(
    {"window_start", "source", "outcome", "count", "first_seen_at", "last_seen_at"}
)
_GATE_OUTCOMES = frozenset({"rejected", "timed_out", "deferred"})
_ALL_OUTCOMES = _GATE_OUTCOMES | PRODUCER_PRESSURE_OUTCOMES

_SOURCES = ("radio-a", "relay-b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _HeldRunner:
    """Pipeline runner double holding each admission on a private gate.

    Every ``admit_ingress`` call records its entry (the grant moment) and
    then awaits a FIFO future; :meth:`release` resolves the oldest held
    admissions, so the test controls exactly which wave completes and the
    downstream consumer stays periodically held without any sleep.
    """

    def __init__(self) -> None:
        self.entries: list[object] = []
        self.admitted: list[object] = []
        self._gates: deque[asyncio.Future[None]] = deque()

    async def admit_ingress(
        self,
        event: object,
        provenance: object,
        attachment: object = None,
        attachment_limits: object = None,
    ) -> object:
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._gates.append(gate)
        self.entries.append(event)
        await gate
        self.admitted.append(event)
        return {"admitted": len(self.admitted)}

    def release(self, count: int = 1) -> None:
        """Complete the *count* oldest held admissions."""
        for _ in range(count):
            gate = self._gates.popleft()
            gate.set_result(None)


class _Clock:
    """Test-controlled wall clock so windows advance only by explicit steps."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def _report(adapter_id: str, outcome: str, count: int = 1) -> ProducerPressureReport:
    """Build a content-free producer report for one source."""
    return ProducerPressureReport(adapter_id=adapter_id, outcome=outcome, count=count)


def _entry_source(event: object) -> str:
    """Return the source token carried by a synthetic arrival's prefix.

    Both scenario sources have seven-character names, so the prefix is
    the identity; arrivals are in-memory operational tokens only.
    """
    return str(event)[:7]


async def _open_storage(tmp_path: Path, name: str) -> SQLiteStorage:
    storage = SQLiteStorage(str(tmp_path / name))
    await storage.initialize()
    return storage


def _gate_app(
    storage: object, runner: object, controller: CapacityController
) -> _GateApp:
    """Assemble the shared production-seam double for one scenario."""
    app = _GateApp()
    app.pipeline_runner = runner
    app.storage = storage
    app._capacity_controller = controller
    app._runtime_accounting = RuntimeAccounting()
    return app


async def _await_flush_settled(app: _GateApp) -> None:
    """Run one absorb epoch, then await the single-flight flush task."""
    app._absorb_producer_pressure()
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None and not task.done():
        await task


def _totals(rows: list[dict[str, object]]) -> dict[tuple[str, str], int]:
    """Fold durable pressure rows into per-(source, outcome) totals."""
    folded: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["source"]), str(row["outcome"]))
        folded[key] = folded.get(key, 0) + int(row["count"])  # type: ignore[arg-type]
    return folded


def _assert_content_free_rows(
    rows: list[dict[str, object]], sources: tuple[str, ...]
) -> None:
    """Every durable pressure row carries identity, outcome, counters only."""
    assert rows, "expected persisted pressure rows"
    for row in rows:
        assert set(row) == _PRESSURE_ROW_FIELDS
        assert row["source"] in sources
        assert row["outcome"] in _ALL_OUTCOMES
        assert isinstance(row["count"], int) and not isinstance(row["count"], bool)
        assert row["count"] >= 1  # type: ignore[comparison-overlap]
        assert isinstance(row["window_start"], int)
        assert row["first_seen_at"] <= row["last_seen_at"]


def _record_measurement(name: str, payload: dict[str, object]) -> None:
    """Emit one content-free JSON measurement when the report dir is set.

    Nothing is written unless ``MEDRE_SUSTAINED_INGRESS_REPORT_DIR`` is
    present, and the payload carries only closed-vocabulary tokens and
    integer counters — the same content-free shape as the durable rows.
    """
    root = os.environ.get(_REPORT_DIR_ENV)
    if not root:
        return
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _marker_count(caplog: pytest.LogCaptureFixture, marker: str) -> int:
    return sum(1 for message in caplog.messages if message.startswith(marker))


def _pending_half(
    pending: dict[tuple[str, str, int], tuple[int, int, int]],
) -> tuple[
    dict[tuple[str, str, int], tuple[int, int, int]],
    dict[tuple[str, str, int], tuple[int, int, int]],
]:
    """Split the pending store into gate-refusal and producer halves."""
    gate: dict[tuple[str, str, int], tuple[int, int, int]] = {}
    producer: dict[tuple[str, str, int], tuple[int, int, int]] = {}
    for key, aggregate in pending.items():
        if key[1] in PRODUCER_PRESSURE_OUTCOMES:
            producer[key] = aggregate
        else:
            gate[key] = aggregate
    return gate, producer


# ---------------------------------------------------------------------------
# Scenario 1 — simultaneous sources across grant waves
# ---------------------------------------------------------------------------


async def test_simultaneous_sources_share_the_gate_across_waves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sources make admission progress every wave under saturation.

    One source drives the ``publish_inbound`` seam, the other the
    cursor-aware ``admit_inbound`` seam, so every gate outcome
    (``admitted`` via the fake runner, ``rejected``, ``timed_out``, and
    ``deferred``) occurs and the per-source totals reconcile exactly with
    the submissions while a held consumer throttles completion.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    controller = CapacityController(
        _Limits(
            max_inflight_inbound_admissions=2,
            inbound_admission_timeout_seconds=_SUSTAINED_TIMEOUT_S,
        )
    )
    controller.configure_inbound_sources(_SOURCES)
    runner = _HeldRunner()
    storage = await _open_storage(tmp_path, "sustained.db")
    app = _gate_app(storage, runner, controller)
    try:
        publish = app._make_publish_inbound("radio-a")
        admit = app._make_admit_inbound("relay-b")

        # Producer-side telemetry reports concurrently with the gate waves
        # from a plain thread, the way an SDK reader thread would.
        sinks = {source: app._producer_pressure_reporter(source) for source in _SOURCES}
        assert all(sink is not None for sink in sinks.values())
        thread_failures: list[BaseException] = []
        producer_done = threading.Event()

        def produce() -> None:
            try:
                for source in _SOURCES:
                    for _ in range(12):
                        sinks[source](_report(source, "scheduled"))  # type: ignore[misc]
                    for _ in range(3):
                        sinks[source](  # type: ignore[misc]
                            _report(source, "overflow", count=4)
                        )
            except BaseException as exc:  # pragma: no cover - failure path
                thread_failures.append(exc)
            finally:
                producer_done.set()

        producer_thread = threading.Thread(target=produce, daemon=True)
        producer_thread.start()

        submitted = {"radio-a": 0, "relay-b": 0}
        outcome_counts = {
            "admitted": 0,
            "rejected": 0,
            "timed_out": 0,
            "deferred": 0,
        }
        peak_waiting = 0

        for wave in range(1, _WAVES + 1):
            # One arrival per source crosses admission and is held there by
            # the throttled downstream consumer.
            held = {
                "radio-a": asyncio.create_task(publish(f"radio-a-w{wave}")),
                "relay-b": asyncio.create_task(admit(f"relay-b-w{wave}", "live")),
            }
            assert await wait_until(
                lambda w=wave: len(runner.entries) == 2 * w, interval=0.005
            )
            entries_this_wave = runner.entries[2 * (wave - 1) :]
            assert {_entry_source(event) for event in entries_this_wave} == set(
                _SOURCES
            ), f"wave {wave}: a source starved across grant waves"

            # Overlapping burst: each source queues one more arrival, inside
            # its equal fair share of the bounded wait queue.
            queued = {
                "radio-a": asyncio.create_task(publish(f"radio-a-q{wave}")),
                "relay-b": asyncio.create_task(admit(f"relay-b-q{wave}", "live")),
            }
            assert await wait_until(
                lambda: controller.snapshot()["inbound_admission_waiting"] == 2,
                interval=0.005,
            )
            snapshot = controller.snapshot()
            peak_waiting = max(peak_waiting, snapshot["inbound_admission_waiting"])
            for source in _SOURCES:
                source_view = snapshot["inbound_sources"][source]
                assert source_view["waiting"] == 1
                assert source_view["waiting"] <= source_view["wait_limit"]
            submitted["radio-a"] += 2
            submitted["relay-b"] += 2

            # The bounded queue is full: the next arrival per source is
            # refused immediately with the seam's typed outcome.
            with pytest.raises(InboundAdmissionRejected):
                await publish(f"radio-a-x{wave}")
            with pytest.raises(DurableIngressDeferredError) as deferred:
                await admit(f"relay-b-x{wave}", "live")
            assert deferred.value.reasons == ("inbound_admission_capacity",)
            submitted["radio-a"] += 1
            submitted["relay-b"] += 1
            outcome_counts["rejected"] += 1
            outcome_counts["deferred"] += 1

            # The queued arrivals wait past the admission timeout while the
            # consumer stays held; condition-poll, never sleep.
            assert await wait_until(
                lambda w=wave: controller.snapshot()["inbound_timeouts"] == 2 * w,
                interval=0.005,
            )
            for task in queued.values():
                with pytest.raises(
                    (InboundAdmissionRejected, DurableIngressDeferredError)
                ):
                    await task
            outcome_counts["timed_out"] += 2

            # The consumer completes the held wave; both sources progress.
            runner.release(2)
            for task in held.values():
                await task
            outcome_counts["admitted"] += 2

        producer_thread.join(timeout=10)
        assert producer_done.is_set() and not thread_failures

        assert await wait_until(lambda: len(runner.admitted) == 2 * _WAVES)
        await _await_flush_settled(app)
        rows = await storage.list_inbound_pressure_observations()
        totals = _totals(rows)
        gate_totals = {
            key: total for key, total in totals.items() if key[1] in _GATE_OUTCOMES
        }
        producer_totals = {
            key: total
            for key, total in totals.items()
            if key[1] in PRODUCER_PRESSURE_OUTCOMES
        }

        # Exact accounting: admitted + rejected + timed_out + deferred
        # reconciles with submissions per source and in total.
        assert runner.admitted == runner.entries
        total_submitted = sum(submitted.values())
        assert total_submitted == 6 * _WAVES
        assert sum(gate_totals.values()) + outcome_counts["admitted"] == (
            total_submitted
        )
        for source, count in submitted.items():
            admitted_for_source = sum(
                1 for event in runner.admitted if _entry_source(event) == source
            )
            refused_for_source = sum(
                total for (src, _), total in gate_totals.items() if src == source
            )
            assert admitted_for_source + refused_for_source == count, source
        assert gate_totals == {
            ("radio-a", "rejected"): _WAVES,
            ("radio-a", "timed_out"): _WAVES,
            ("relay-b", "deferred"): 2 * _WAVES,
        }
        snapshot = controller.snapshot()
        assert snapshot["inbound_rejections"] == 2 * _WAVES
        assert snapshot["inbound_timeouts"] == 2 * _WAVES
        assert snapshot["inbound_current"] == 0
        assert snapshot["inbound_admission_waiting"] == 0
        assert peak_waiting <= controller.inbound_limit
        accounting = app._runtime_accounting.snapshot()
        assert accounting["capacity_rejections"] == 4 * _WAVES  # type: ignore[union-attr]

        # The producer half persisted alongside the gate pressure.
        assert producer_totals == {
            ("radio-a", "scheduled"): 12,
            ("radio-a", "overflow"): 12,
            ("relay-b", "scheduled"): 12,
            ("relay-b", "overflow"): 12,
        }
        _assert_content_free_rows(rows, _SOURCES)
        assert not (getattr(app, "_pressure_pending", None) or {})

        windows = {int(row["window_start"]) for row in rows}  # type: ignore[arg-type]
        _record_measurement(
            "simultaneous_sources",
            {
                "scenario": "simultaneous_sources",
                "grant_waves": _WAVES,
                "window_starts": sorted(windows),
                "window_count": len(windows),
                "submitted": total_submitted,
                "admitted": outcome_counts["admitted"],
                "rejected": outcome_counts["rejected"],
                "timed_out": outcome_counts["timed_out"],
                "deferred": outcome_counts["deferred"],
                "per_source": {
                    source: {
                        "submitted": submitted[source],
                        "admitted": sum(
                            1
                            for event in runner.admitted
                            if _entry_source(event) == source
                        ),
                        "refused": sum(
                            total
                            for (src, _), total in gate_totals.items()
                            if src == source
                        ),
                    }
                    for source in _SOURCES
                },
                "producer_outcomes": {
                    f"{src}:{outcome}": total
                    for (src, outcome), total in producer_totals.items()
                },
                "peak_inbound_waiting": peak_waiting,
            },
        )
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Scenario 2 — SQLite writer contention
# ---------------------------------------------------------------------------


async def test_writer_lock_blocks_flushes_without_touching_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A foreign ``BEGIN IMMEDIATE`` writer stalls the pressure flush only.

    Admissions and gate refusals keep producing typed outcomes while the
    flush blocks on SQLite's busy timeout; the unwritten batch is retained
    within the bounded-retention contract, producer-half keys are the only
    evictable ones, and no exception reaches the admission path.  Once the
    foreign writer leaves, the drain persists every retained key exactly.
    """
    caplog.set_level(logging.WARNING, logger="medre.runtime.app")
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(
        _app_module, "PRODUCER_PRESSURE_MAX_PENDING_KEYS", _FLOODED_PRODUCER_BOUND
    )
    controller = CapacityController(
        _Limits(
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=5.0,
        )
    )
    controller.configure_inbound_sources(_SOURCES)
    runner = _HeldRunner()
    storage = await _open_storage(tmp_path, "writer-lock.db")
    app = _gate_app(storage, runner, controller)
    holder: sqlite3.Connection | None = sqlite3.connect(
        str(tmp_path / "writer-lock.db"), isolation_level=None
    )
    try:
        holder.execute("BEGIN IMMEDIATE")

        publish = app._make_publish_inbound("radio-a")
        admit = app._make_admit_inbound("relay-b")

        # Hold the only slot and fill the single wait share, so the gate is
        # saturated before the contention window opens.
        held = asyncio.create_task(publish("radio-a-held"))
        assert await wait_until(lambda: controller.inbound_current == 1)
        queued = asyncio.create_task(publish("radio-a-queued"))
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )

        # The saturated gate keeps refusing with typed outcomes only; the
        # first refusal starts the single-flight flush, whose first write
        # blocks on the foreign lock (storage busy_timeout=5000ms).
        with pytest.raises(InboundAdmissionRejected):
            await publish("radio-a-refused")
        with pytest.raises(DurableIngressDeferredError):
            await admit("relay-b-deferred", "live")
        flush_task = getattr(app, "_pressure_flush_task", None)
        assert flush_task is not None and not flush_task.done()

        # While the flush is blocked in its writer thread, a producer
        # thread keeps reporting and producer keys flood past the retained
        # bound through the absorb epoch: producer keys are evicted with an
        # explicit marker, gate-refusal keys are never touched, and no
        # second flush starts (single flight).
        sink = app._producer_pressure_reporter("radio-a")
        assert sink is not None
        thread_failures: list[BaseException] = []
        producer_done = threading.Event()

        def produce() -> None:
            try:
                for _ in range(8):
                    sink(_report("radio-a", "scheduled", count=2))
            except BaseException as exc:  # pragma: no cover - failure path
                thread_failures.append(exc)
            finally:
                producer_done.set()

        producer_thread = threading.Thread(target=produce, daemon=True)
        producer_thread.start()
        producer_thread.join(timeout=10)
        assert producer_done.is_set() and not thread_failures
        for offset in range(1, 5):
            clock.now = _BASE_EPOCH + offset * 120
            sink(_report("radio-a", "overflow", count=2))
            app._absorb_producer_pressure()
        assert not flush_task.done()

        # The gate still completes held admissions during the block, and
        # the parked waiter is granted into admission on the freed slot.
        runner.release(1)
        await held
        assert await wait_until(lambda: len(runner.entries) == 2)
        runner.release(1)
        await queued

        # The blocked write fails after the busy timeout; the batch is
        # retained and the failure is logged.
        await flush_task
        assert flush_task.exception() is None
        pending = app._pressure_pending
        gate_half, producer_half = _pending_half(pending)
        base_window = pressure_window_start(_BASE_EPOCH)
        assert gate_half == {
            ("radio-a", "rejected", base_window): (1, _BASE_EPOCH, _BASE_EPOCH),
            ("relay-b", "deferred", base_window): (1, _BASE_EPOCH, _BASE_EPOCH),
        }
        assert len(producer_half) <= _FLOODED_PRODUCER_BOUND
        assert {key[2] for key in producer_half} == {
            pressure_window_start(_BASE_EPOCH + 3 * 120),
            pressure_window_start(_BASE_EPOCH + 4 * 120),
        }
        dropped = _marker_count(caplog, "PRODUCER_PRESSURE_ACCOUNTING_DROPPED")
        # Bound 2, five staged producer keys: the scheduled key and the two
        # oldest overflow keys are dropped, always producer-half keys.
        assert dropped == 3
        assert _marker_count(caplog, "INBOUND_PRESSURE_RECORD_FAILED") >= 1

        # The foreign writer leaves; the drain persists every retained key.
        holder.execute("COMMIT")
        holder.close()
        holder = None
        await app._drain_inbound_pressure_flush()
        assert app._pressure_pending == {}

        rows = await storage.list_inbound_pressure_observations()
        totals = _totals(rows)
        assert totals == {
            ("radio-a", "rejected"): 1,
            ("relay-b", "deferred"): 1,
            ("radio-a", "overflow"): 4,
        }
        _assert_content_free_rows(rows, _SOURCES)
        snapshot = controller.snapshot()
        assert snapshot["inbound_current"] == 0
        assert snapshot["inbound_admission_waiting"] == 0

        windows = {int(row["window_start"]) for row in rows}  # type: ignore[arg-type]
        _record_measurement(
            "writer_lock_contention",
            {
                "scenario": "writer_lock_contention",
                "window_starts": sorted(windows),
                "window_count": len(windows),
                "admitted": 2,
                "rejected": 1,
                "deferred": 1,
                "timed_out": 0,
                "producer_outcomes": {
                    f"{src}:{outcome}": total
                    for (src, outcome), total in totals.items()
                    if outcome in PRODUCER_PRESSURE_OUTCOMES
                },
                "producer_keys_dropped_with_marker": dropped,
            },
        )
    finally:
        if holder is not None:
            holder.close()
        await storage.close()


# ---------------------------------------------------------------------------
# Scenario 3 — storage failure across a sustained reporting window
# ---------------------------------------------------------------------------


async def test_sustained_store_failure_is_bounded_logged_and_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Storage raising across a sustained window loses only bounded telemetry.

    Producer keys are bounded by the documented constant and dropped with
    an explicit marker; every gate-refusal key survives; the reporter and
    the flusher never raise into any thread; and recovery persists the
    retained keys plus fresh windows exactly.
    """
    caplog.set_level(logging.WARNING, logger="medre.runtime.app")
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(
        _app_module, "PRODUCER_PRESSURE_MAX_PENDING_KEYS", _SUSTAINED_PRODUCER_BOUND
    )
    controller = CapacityController(
        _Limits(
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=5.0,
        )
    )
    controller.configure_inbound_sources(_SOURCES)

    calls = 0

    async def _failing_record(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("storage unavailable")

    app = _gate_app(
        SimpleNamespace(record_inbound_pressure=_failing_record),
        _HeldRunner(),
        controller,
    )
    sinks = {source: app._producer_pressure_reporter(source) for source in _SOURCES}
    assert all(sink is not None for sink in sinks.values())

    # A plain thread reports under the failing store the way an SDK reader
    # thread would; the sink must never raise into it.
    thread_failures: list[BaseException] = []
    producer_done = threading.Event()

    def produce() -> None:
        try:
            for _ in range(8):
                sinks["radio-a"](_report("radio-a", "scheduled", count=2))  # type: ignore[misc]
        except BaseException as exc:  # pragma: no cover - failure path
            thread_failures.append(exc)
        finally:
            producer_done.set()

    producer_thread = threading.Thread(target=produce, daemon=True)
    producer_thread.start()
    producer_thread.join(timeout=10)
    assert producer_done.is_set() and not thread_failures

    # Two producer keys per window (radio-a scheduled, relay-b overflow)
    # and one gate refusal pair per window, for four sustained windows.
    for window in range(_FAILURE_WINDOWS):
        clock.now = _BASE_EPOCH + window * 120
        sinks["radio-a"](_report("radio-a", "scheduled", count=2))  # type: ignore[misc]
        sinks["relay-b"](_report("relay-b", "overflow", count=2))  # type: ignore[misc]
        await _await_flush_settled(app)
        app._record_inbound_pressure_loss(
            "radio-a", SimpleNamespace(reason="queue_full"), cursor_aware=False
        )
        app._record_inbound_pressure_loss(
            "relay-b", SimpleNamespace(reason="queue_full"), cursor_aware=True
        )
        await _await_flush_settled(app)

    # The producer half is bounded and its loss is explicit; the gate
    # half is complete — one retained key per (source, outcome, window).
    gate_half, producer_half = _pending_half(app._pressure_pending)
    assert len(producer_half) <= _SUSTAINED_PRODUCER_BOUND
    refusal_windows = [
        pressure_window_start(_BASE_EPOCH + w * 120) for w in range(_FAILURE_WINDOWS)
    ]
    assert gate_half == {
        key: (1, key[2], key[2])
        for key in [("radio-a", "rejected", window) for window in refusal_windows]
        + [("relay-b", "deferred", window) for window in refusal_windows]
    }
    dropped = _marker_count(caplog, "PRODUCER_PRESSURE_ACCOUNTING_DROPPED")
    # Eight staged producer keys against bound three: exactly the five
    # oldest keys are dropped during the sustained window, newest windows
    # retained.
    assert dropped == 5
    assert producer_half == {
        ("radio-a", "scheduled", pressure_window_start(_BASE_EPOCH + 3 * 120)): (
            2,
            _BASE_EPOCH + 3 * 120,
            _BASE_EPOCH + 3 * 120,
        ),
        ("relay-b", "overflow", pressure_window_start(_BASE_EPOCH + 2 * 120)): (
            2,
            _BASE_EPOCH + 2 * 120,
            _BASE_EPOCH + 2 * 120,
        ),
        ("relay-b", "overflow", pressure_window_start(_BASE_EPOCH + 3 * 120)): (
            2,
            _BASE_EPOCH + 3 * 120,
            _BASE_EPOCH + 3 * 120,
        ),
    }
    assert calls >= 2 * _FAILURE_WINDOWS
    assert _marker_count(caplog, "INBOUND_PRESSURE_RECORD_FAILED") >= 1

    # Recovery: real storage drains every retained key, and a fresh window
    # staged before the final drain persists alongside them.  The fresh
    # key merges against the still-full producer bound, so the oldest
    # retained key (relay-b overflow two windows back) is dropped with one
    # more explicit marker — bounded loss, never a silent one.
    storage = await _open_storage(tmp_path, "recovery.db")
    try:
        app.storage = storage
        clock.now = _BASE_EPOCH + _FAILURE_WINDOWS * 120
        sinks["radio-a"](_report("radio-a", "scheduled", count=2))  # type: ignore[misc]
        app._record_inbound_pressure_loss(
            "radio-a", SimpleNamespace(reason="queue_full"), cursor_aware=False
        )
        await app._drain_inbound_pressure_flush()
        assert app._pressure_pending == {}

        rows = await storage.list_inbound_pressure_observations()
        totals = _totals(rows)
        assert totals[("radio-a", "rejected")] == _FAILURE_WINDOWS + 1
        assert totals[("relay-b", "deferred")] == _FAILURE_WINDOWS
        assert totals[("radio-a", "scheduled")] == 4
        assert totals[("relay-b", "overflow")] == 2
        assert _marker_count(caplog, "PRODUCER_PRESSURE_ACCOUNTING_DROPPED") == 6
        _assert_content_free_rows(rows, _SOURCES)

        windows = {int(row["window_start"]) for row in rows}  # type: ignore[arg-type]
        _record_measurement(
            "store_failure_sustained_window",
            {
                "scenario": "store_failure_sustained_window",
                "sustained_windows": _FAILURE_WINDOWS,
                "window_count": len(windows),
                "rejected": totals[("radio-a", "rejected")],
                "deferred": totals[("relay-b", "deferred")],
                "timed_out": 0,
                "admitted": 0,
                "producer_outcomes": {
                    f"{src}:{outcome}": total
                    for (src, outcome), total in totals.items()
                    if outcome in PRODUCER_PRESSURE_OUTCOMES
                },
                "producer_keys_dropped_with_marker": 6,
            },
        )
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Scenario 4 — owner-loop liveness under gate saturation
# ---------------------------------------------------------------------------


async def test_owner_loop_runs_sentinels_while_gate_saturated(tmp_path: Path) -> None:
    """A saturated gate never starves the owner event loop.

    With the single admission slot held and a waiter parked in the bounded
    queue, sentinel tasks scheduled on the owner loop complete within a
    generous liveness bound every round.  The bound proves no starvation,
    not a latency SLA — see the constant's rationale above.
    """
    controller = CapacityController(
        _Limits(
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=5.0,
        )
    )
    controller.configure_inbound_sources(("radio-a",))
    runner = _HeldRunner()
    storage = await _open_storage(tmp_path, "loop-health.db")
    app = _gate_app(storage, runner, controller)
    try:
        publish = app._make_publish_inbound("radio-a")

        held = asyncio.create_task(publish("radio-a-held"))
        assert await wait_until(lambda: controller.inbound_current == 1)
        queued = asyncio.create_task(publish("radio-a-queued"))
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        with pytest.raises(InboundAdmissionRejected):
            await publish("radio-a-queue-full-probe")

        max_sentinel_seconds = 0.0
        for _ in range(_SENTINEL_ROUNDS):
            loop = asyncio.get_running_loop()

            async def _sentinel() -> bool:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return True

            started = loop.time()
            assert await asyncio.wait_for(_sentinel(), timeout=_SENTINEL_BOUND_S)
            max_sentinel_seconds = max(max_sentinel_seconds, loop.time() - started)
            # Diagnostics stay responsive while the gate is saturated.
            assert controller.snapshot()["inbound_admission_waiting"] == 1
        assert max_sentinel_seconds < _SENTINEL_BOUND_S

        # Saturation persisted the whole time; the release grants the
        # parked waiter into admission and the gate returns to quiescence.
        assert controller.snapshot()["inbound_admission_waiting"] == 1
        runner.release(1)
        await held
        assert await wait_until(lambda: len(runner.entries) == 2)
        runner.release(1)
        await queued
        snapshot = controller.snapshot()
        assert snapshot["inbound_current"] == 0
        assert snapshot["inbound_admission_waiting"] == 0
        assert len(runner.admitted) == 2

        _record_measurement(
            "loop_health_under_pressure",
            {
                "scenario": "loop_health_under_pressure",
                "sentinel_rounds": _SENTINEL_ROUNDS,
                "sentinel_bound_seconds": _SENTINEL_BOUND_S,
                "gate_waiting_under_saturation": 1,
                "admitted_after_release": 2,
            },
        )
    finally:
        await storage.close()
