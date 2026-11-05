"""Process-lifetime qualification of the producer pressure collector.

The collector — the staging buffer, the absorb path, and the
single-flight flush — is process-level state that outlives any single
adapter session: the runtime binds one fresh ``report_producer_pressure``
sink per adapter session (the construction the ``AdapterContext``
receives at every adapter start), while the buffer, lock, retained
pending store, and flush-task slot are created lazily once and reused.

Adapter sessions themselves do not restart inside one live process:
``AdapterState.STOPPED`` and ``AdapterState.FAILED`` are terminal states
with no outgoing transitions, and adapter restart is the
process-generation boundary.  What a long-lived process does exercise is
sequential session binding — one sink per session, retired sinks still
callable from SDK threads — so this suite drives 25 simulated adapter
session cycles through the production seams in one process and
qualifies:

* staging key count and per-context registrations stay flat across
  cycles (keys coalesce by adapter id and outcome, never per session);
* tracemalloc retained bytes stay flat under dual guards;
* every new session's reporter remains functional with honest
  per-session accounting, while late calls through retired sessions'
  reporters are harmless;
* the shutdown fence drops late retired-reporter traffic without
  reopening storage writes.
"""

from __future__ import annotations

import gc
import tracemalloc
import weakref
from pathlib import Path

import pytest

from medre.core.contracts.adapter import ProducerPressureReport
from medre.core.lifecycle.states import AdapterState, VALID_TRANSITIONS
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime import app as _app_module
from tests.helpers.inbound_gate import GateAppDouble as _GateApp

pytestmark = pytest.mark.usefixtures("isolated_config_env")

#: Session cycles driven through the collector in one process.
_SESSIONS = 25

#: Warm-up cycles before the tracemalloc baseline.  Beyond lazy
#: construction (buffer, lock, first flush task), the warm-up also runs
#: the sqlite3 driver's write path past its bounded statement-warming
#: window — measured per-cycle retention decays to a flat plateau by
#: roughly two hundred real writes — so the baseline reflects collector
#: steady state rather than driver cache fill.
_WARMUP_SESSIONS = 220

#: Dual retained-memory guards for the measured window.
_EXTRA_BYTES_LIMIT = 256_000

#: Fixed epoch base; sessions advance the clock by explicit one-second
#: steps, never by real elapsed time.
_BASE_EPOCH = 1_800_000_000

_ADAPTER_ID = "radio-session"


class _Clock:
    """Test-controlled wall clock; sessions advance only by explicit steps."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def _report(outcome: str, count: int) -> ProducerPressureReport:
    """Build one content-free producer report for the session adapter."""
    return ProducerPressureReport(adapter_id=_ADAPTER_ID, outcome=outcome, count=count)


async def _open_storage(tmp_path: Path) -> SQLiteStorage:
    storage = SQLiteStorage(str(tmp_path / "sessions.db"))
    await storage.initialize()
    return storage


def _collector_app(storage: object) -> _GateApp:
    """Assemble the production collector seams on the minimal double."""
    app = _GateApp()
    app.storage = storage
    app._capacity_controller = None
    app._runtime_accounting = None
    return app


async def _absorb_and_flush(app: _GateApp) -> None:
    """Run one absorb epoch, then await the single-flight flush task."""
    app._absorb_producer_pressure()
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None and not task.done():
        await task


def _assert_collector_settled(app: _GateApp) -> None:
    """After a flush the staging buffer and retained store are empty."""
    assert getattr(app, "_producer_pressure_buffer", None) == {}
    assert getattr(app, "_producer_pressure_absorb_scheduled", False) is False
    assert getattr(app, "_pressure_pending", None) == {}


async def _durable_total(storage: SQLiteStorage, outcome: str) -> int:
    """Fold durable pressure rows of one outcome into a total count."""
    rows = await storage.list_inbound_pressure_observations()
    assert all(row["source"] == _ADAPTER_ID for row in rows)
    return sum(int(row["count"]) for row in rows if row["outcome"] == outcome)


async def test_collector_footprint_stays_flat_across_session_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25 simulated adapter session cycles leave the collector flat.

    Each cycle binds one fresh sink exactly as an adapter start does,
    reports through it, drains the staging epoch, and persists against
    real SQLite.  Staging keys coalesce by adapter id and outcome, every
    session's accounting reconciles exactly with the durable rows, and
    the retained memory stays flat under dual guards.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    storage = await _open_storage(tmp_path)
    app = _collector_app(storage)
    try:
        # --- tracemalloc ownership: begin before the warm-up so the
        # pre-burst baseline is a real steady-state figure rather than a
        # degenerate near-zero residue of untraced allocations ---
        owns_tracing = not tracemalloc.is_tracing()
        if owns_tracing:
            tracemalloc.start()

        # --- Warm-up cycles (lazy construction and the sqlite3 driver's
        # statement-warming window sit outside the measured window) ---
        retired_sink = None
        previous_sink = None
        for _ in range(_WARMUP_SESSIONS):
            sink = app._producer_pressure_reporter(_ADAPTER_ID)
            assert sink is not None
            if retired_sink is None:
                retired_sink = sink
            assert sink is not previous_sink
            previous_sink = sink
            sink(_report("scheduled", 2))
            sink(_report("overflow", 1))
            await _absorb_and_flush(app)
            _assert_collector_settled(app)
        gc.collect()
        baseline_current, _baseline_peak = tracemalloc.get_traced_memory()

        # --- Measured session cycles ---
        reporter_refs = []
        for session in range(_SESSIONS):
            sink = app._producer_pressure_reporter(_ADAPTER_ID)
            assert sink is not None
            reporter_refs.append(weakref.ref(sink))
            # One fresh bound sink per session, never a shared one.
            assert sink is not previous_sink
            scheduled_before = await _durable_total(storage, "scheduled")
            sink(_report("scheduled", session + 1))
            sink(_report("overflow", 1))
            # A misattributed report is dropped by the production sink:
            # foreign adapter ids are not pressure evidence.
            sink(
                ProducerPressureReport(
                    adapter_id="some-other-adapter", outcome="overflow", count=99
                )
            )
            # Staging coalesces onto per-outcome keys; never per session.
            assert len(app._producer_pressure_buffer) <= 2
            await _absorb_and_flush(app)
            _assert_collector_settled(app)
            # Per-session diagnostics are honest: exactly this session's
            # counts became durable, attributed to the adapter id only.
            scheduled_after = await _durable_total(storage, "scheduled")
            assert scheduled_after == scheduled_before + session + 1
            clock.now = _BASE_EPOCH + session + 1

        del sink, previous_sink
        gc.collect()
        assert all(ref() is None for ref in reporter_refs)
        final_current, _final_peak = tracemalloc.get_traced_memory()
        extra = final_current - baseline_current
        assert extra < _EXTRA_BYTES_LIMIT
        assert final_current < 2 * max(baseline_current, 1)
        if owns_tracing:
            tracemalloc.stop()

        # --- Retired sessions' reporters remain harmless and honest ---
        retired_sink(_report("overflow", 2))
        assert len(app._producer_pressure_buffer) == 1
        await _absorb_and_flush(app)
        _assert_collector_settled(app)
        # The retired sink still reports under its own adapter id; the
        # durable totals reconcile exactly with every session's reports.
        assert await _durable_total(storage, "scheduled") == 2 * _WARMUP_SESSIONS + sum(
            range(1, _SESSIONS + 1)
        )
        assert await _durable_total(storage, "overflow") == (
            _WARMUP_SESSIONS + _SESSIONS + 2
        )

        # --- The shutdown fence drops late retired traffic ---
        await app._drain_inbound_pressure_flush()
        assert app._producer_pressure_reports_closed is True
        retired_sink(_report("scheduled", 5))
        app._absorb_producer_pressure()
        assert app._producer_pressure_buffer == {}
        assert await _durable_total(storage, "scheduled") == (
            2 * _WARMUP_SESSIONS + sum(range(1, _SESSIONS + 1))
        )
    finally:
        await storage.close()


def test_adapter_sessions_do_not_restart_within_a_live_process() -> None:
    """The lifecycle boundary is terminal: restart is a process generation.

    ``AdapterState.STOPPED`` and ``AdapterState.FAILED`` have no outgoing
    transitions, so a stop-then-start cycle inside one live process is
    not a supported path; a fresh process generation binds fresh adapter
    contexts.  The collector suite above therefore qualifies sequential
    session binds through the production seams — the construction every
    adapter start performs — instead of inventing an in-process restart.
    """
    # The terminal states are dead ends: nothing that stopped or failed
    # ever transitions again, in particular never back to INITIALIZING.
    assert VALID_TRANSITIONS[AdapterState.STOPPED] == frozenset()
    assert VALID_TRANSITIONS[AdapterState.FAILED] == frozenset()
    for _source, targets in VALID_TRANSITIONS.items():
        assert AdapterState.INITIALIZING not in targets
    # STOPPED is reachable (clean stop, or an aborted partial start) but
    # always as a final step: every transition into it leaves the state
    # machine with nowhere further to go.
    stopped_sources = {
        source
        for source, targets in VALID_TRANSITIONS.items()
        if AdapterState.STOPPED in targets
    }
    assert stopped_sources == {AdapterState.STOPPING, AdapterState.INITIALIZING}


async def test_adapter_start_binds_a_usable_pressure_reporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter receives a working, attributed sink through real startup."""
    from medre.config.adapters.meshtastic_runtime import MeshtasticRuntimeConfig
    from medre.config.model import AdapterConfigSet, RuntimeConfig, StorageConfig
    from medre.config.paths import resolve
    from medre.runtime.builder import RuntimeBuilder

    config = RuntimeConfig(
        storage=StorageConfig(backend="sqlite", path=str(tmp_path / "startup.db")),
        adapters=AdapterConfigSet(
            meshtastic={
                _ADAPTER_ID: MeshtasticRuntimeConfig(
                    adapter_id=_ADAPTER_ID, adapter_kind="fake", enabled=True
                )
            }
        ),
    )
    app = RuntimeBuilder(config, resolve(environ={"MEDRE_HOME": str(tmp_path)})).build()
    adapter = app.adapters[_ADAPTER_ID]
    original_start = adapter.start
    captured = []

    async def capture_context(ctx):
        captured.append(ctx)
        assert callable(ctx.report_producer_pressure)
        ctx.report_producer_pressure(_report("overflow", 3))
        await original_start(ctx)

    monkeypatch.setattr(adapter, "start", capture_context)
    try:
        await app.start()
        assert app.started_adapter_ids == [_ADAPTER_ID]
        assert len(captured) == 1
        await _absorb_and_flush(app)
        assert await _durable_total(app.storage, "overflow") == 3
    finally:
        await app.stop()
