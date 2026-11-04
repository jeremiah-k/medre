"""Producer-side pressure reports: contract type, core sink, durable staging.

Covers the closed producer outcome vocabulary (``scheduled``,
``overflow``, ``cancelled_before_admission``), the report type's machine
token validation, the core-supplied sink semantics (thread-safe,
non-blocking, never raises, bounded coalescing with one flush path), the
loss-honest staging decision (unflushed telemetry is lost by design), and
write-failure containment around the shared single-flight flusher.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from medre.core.contracts.adapter import (
    PRODUCER_PRESSURE_OUTCOMES,
    ProducerPressureReport,
)
from medre.core.storage.sqlite._pressure import pressure_window_start
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime import app as _app_module
from medre.runtime.app import PRODUCER_PRESSURE_MAX_PENDING_KEYS
from tests.helpers.async_utils import wait_until
from tests.helpers.inbound_gate import GateAppDouble as _GateApp


def _report(**overrides: object) -> ProducerPressureReport:
    """Build a valid report with optional field overrides."""
    fields: dict[str, object] = {
        "adapter_id": "radio",
        "outcome": "overflow",
        "count": 1,
        "reason": "callback_capacity",
    }
    fields.update(overrides)
    return ProducerPressureReport(**fields)  # type: ignore[arg-type]


async def _open_storage(tmp_path: Path) -> SQLiteStorage:
    storage = SQLiteStorage(str(tmp_path / "pressure.db"))
    await storage.initialize()
    return storage


def _bind_sink(app: _GateApp, adapter_id: str = "radio") -> object:
    """Bind the production sink through the double and require a callable."""
    sink = app._producer_pressure_reporter(adapter_id)
    assert sink is not None
    return sink


async def _await_flush_settled(app: _GateApp) -> None:
    """Run one absorb epoch, then await the single-flight flush task."""
    app._absorb_producer_pressure()
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None and not task.done():
        await task


async def _await_automatic_report_count(storage: SQLiteStorage, expected: int) -> None:
    async def persisted() -> bool:
        rows = await storage.list_inbound_pressure_observations(source="radio")
        return sum(row["count"] for row in rows) == expected

    assert await wait_until(persisted)


# TestProducerReportType: module-level function tests.
# The report type enforces the closed producer vocabulary.


@pytest.mark.parametrize("outcome", sorted(PRODUCER_PRESSURE_OUTCOMES))
def test_closed_vocabulary_constructs(outcome: str) -> None:
    report = _report(outcome=outcome)
    assert report.outcome == outcome


@pytest.mark.parametrize(
    "outcome", ["rejected", "timed_out", "deferred", "admitted", "", "OVERFLOW"]
)
def test_foreign_or_invalid_outcomes_rejected(outcome: str) -> None:
    with pytest.raises(ValueError, match="invalid producer pressure outcome"):
        _report(outcome=outcome)


@pytest.mark.parametrize("count", [0, -1, True, False, 1.5, "2", None])
def test_count_must_be_positive_integer(count: object) -> None:
    with pytest.raises(ValueError, match="count must be a positive integer"):
        _report(count=count)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reason",
    [
        "callback_capacity",
        "reader_thread_backlog",
        "staging_full_2",
        None,
    ],
)
def test_reason_machine_tokens_accepted(reason: str | None) -> None:
    assert _report(reason=reason).reason == reason


@pytest.mark.parametrize(
    "reason",
    [
        "callback capacity",
        "Callback_Capacity",
        "callback-capacity",
        "line1\nline2",
        "sender!@mesh",
        "x" * 65,
        "",
    ],
)
def test_reason_free_text_rejected(reason: str) -> None:
    with pytest.raises(ValueError, match="reason must be a short machine token"):
        _report(reason=reason)


def test_empty_adapter_id_rejected() -> None:
    with pytest.raises(ValueError, match="adapter_id"):
        _report(adapter_id="   ")


def test_report_is_frozen() -> None:
    report = _report()
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.count = 2  # type: ignore[misc]


@pytest.mark.parametrize("outcome", [[], {}, set(), 1, None])
def test_unhashable_or_non_string_outcomes_raise_value_error(outcome: object) -> None:
    with pytest.raises(ValueError, match="invalid producer pressure outcome"):
        _report(outcome=outcome)


@pytest.mark.parametrize("count", [(1 << 63), (1 << 63) + 1])
def test_count_above_sqlite_range_is_rejected(count: int) -> None:
    with pytest.raises(ValueError, match="within SQLite range"):
        _report(count=count)


# TestProducerPressureSink: module-level function tests.
# The core sink is thread-safe, coalesced, bounded, and loss-honest.


async def test_sink_absent_without_pressure_capable_storage() -> None:
    app = _GateApp()
    app.storage = SimpleNamespace()
    assert app._producer_pressure_reporter("radio") is None


async def test_reports_from_plain_thread_coalesce_into_one_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    # A test spanning a real minute boundary must still represent one window.
    monkeypatch.setattr(_app_module._time, "time", lambda: 1_800_000_000.0)
    try:
        done = threading.Event()

        def produce() -> None:
            for _ in range(64):
                sink(_report(outcome="overflow"))
            done.set()

        loop = asyncio.get_running_loop()
        schedule = Mock(wraps=loop.call_soon_threadsafe)
        flush = AsyncMock(wraps=app._flush_inbound_pressure)
        monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)
        monkeypatch.setattr(app, "_flush_inbound_pressure", flush)
        thread = threading.Thread(target=produce, daemon=True)
        thread.start()
        thread.join(timeout=5)  # Hold the loop until the burst has one absorb epoch.
        assert not thread.is_alive() and done.is_set()
        assert schedule.call_count == 1
        await _await_automatic_report_count(storage, 64)
        assert flush.call_count == 1

        rows = await storage.list_inbound_pressure_observations(source="radio")
        assert [(r["outcome"], r["count"]) for r in rows] == [("overflow", 64)]
        # One scheduled absorb epoch, one flush task: never a task per
        # report.
        assert getattr(app, "_pressure_flush_task", None) is not None
    finally:
        await storage.close()


async def test_concurrent_threads_preserve_every_count(tmp_path: Path) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    try:
        failures: list[BaseException] = []

        def produce(offset: int) -> None:
            try:
                for index in range(25):
                    sink(
                        _report(
                            outcome="scheduled",
                            reason=None,
                            count=1 + (index + offset) % 3,
                        )
                    )
            except BaseException as exc:  # pragma: no cover - failure path
                failures.append(exc)

        threads = [threading.Thread(target=produce, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []
        await _await_automatic_report_count(
            storage, sum(1 + (i + off) % 3 for off in range(8) for i in range(25))
        )

        rows = await storage.list_inbound_pressure_observations(source="radio")
        total = sum(row["count"] for row in rows)
        assert total == sum(
            sum(1 + (i + off) % 3 for i in range(25)) for off in range(8)
        )
    finally:
        await storage.close()


async def test_sink_never_raises_into_the_caller(tmp_path: Path) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    try:
        # Foreign identity: dropped, not misattributed.
        sink(_report(adapter_id="matrix"))
        # A lookalike that is not the contract type: dropped.
        sink(SimpleNamespace(adapter_id="radio", outcome="overflow", count=1))  # type: ignore[arg-type]
        await _await_flush_settled(app)
        rows = await storage.list_inbound_pressure_observations()
        assert rows == []
    finally:
        await storage.close()


async def test_stopped_loop_drops_telemetry_without_raising(tmp_path: Path) -> None:
    storage = await _open_storage(tmp_path)
    try:
        sink_holder: dict[str, object] = {}

        def scenario() -> None:
            async def inner() -> None:
                app = _GateApp()
                app.storage = storage
                sink_holder["sink"] = app._producer_pressure_reporter("radio")

            # The owner loop binds the sink and stops immediately.
            asyncio.run(inner())

        thread = threading.Thread(target=scenario)
        thread.start()
        thread.join()
        # A retired SDK thread reports after the owner loop stopped: the
        # call must return normally and stage nothing durable.
        sink_holder["sink"](_report(outcome="cancelled_before_admission"))
        rows = await storage.list_inbound_pressure_observations()
        assert rows == []
    finally:
        await storage.close()


async def test_unflushed_staging_is_lost_by_design(tmp_path: Path) -> None:
    db_path = str(tmp_path / "pressure.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        app = _GateApp()
        app.storage = storage
        sink = _bind_sink(app)
        # Stage reports but never run the absorb epoch nor the flush:
        # the counts live only in the bounded in-memory buffer.
        sink(_report(outcome="scheduled", reason=None))
        sink(_report(outcome="overflow"))
        buffer = getattr(app, "_producer_pressure_buffer", {})
        assert len(buffer) == 2
        assert not getattr(app, "_pressure_pending", None)
        # The runtime loses its pressure-capable storage before any
        # flush reaches it (crash or teardown edge): the absorb epoch
        # finds no recorder and the staged counts are dropped as
        # operational accounting — never reported as admitted.
        app.storage = SimpleNamespace()
        await asyncio.sleep(0)
        assert app._producer_pressure_buffer == {}
        assert not getattr(app, "_pressure_pending", None)
        rows = await storage.list_inbound_pressure_observations()
        assert rows == []
    finally:
        await storage.close()

    reopened = SQLiteStorage(db_path)
    await reopened.initialize()
    try:
        rows = await reopened.list_inbound_pressure_observations()
        # Restart honesty: only flushed windows persist; the unflushed
        # in-memory producer telemetry was lost by design, never
        # reported as admitted.
        assert rows == []
    finally:
        await reopened.close()


async def test_flushed_producer_outcomes_survive_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "pressure.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    try:
        sink(_report(outcome="cancelled_before_admission", reason=None))
        await _await_flush_settled(app)
    finally:
        await storage.close()

    reopened = SQLiteStorage(db_path)
    await reopened.initialize()
    try:
        rows = await reopened.list_inbound_pressure_observations()
        assert [(r["outcome"], r["count"]) for r in rows] == [
            ("cancelled_before_admission", 1)
        ]
    finally:
        await reopened.close()


async def test_buffer_bound_drops_oldest_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    monkeypatch.setattr(_app_module, "PRODUCER_PRESSURE_MAX_PENDING_KEYS", 2)
    assert PRODUCER_PRESSURE_MAX_PENDING_KEYS == 1024
    try:
        # Three distinct windows overflow the bounded staging buffer:
        # the oldest staged key is dropped, the newest two retained.
        for window_offset in range(3):
            observed_at = 1_800_000_000 + window_offset * 120
            monkeypatch.setattr(
                _app_module._time, "time", lambda t=observed_at: float(t)
            )
            sink(_report(outcome="overflow"))
        await _await_flush_settled(app)

        rows = await storage.list_inbound_pressure_observations(source="radio")
        windows = {r["window_start"] for r in rows}
        assert windows == {
            pressure_window_start(1_800_000_120),
            pressure_window_start(1_800_000_240),
        }
        assert sum(r["count"] for r in rows) == 2
    finally:
        await storage.close()


# TestProducerFlushContainment: module-level function tests.
# Storage failures never escape the sink or disturb message ingress.


async def test_write_failure_retains_batch_and_never_raises(tmp_path: Path) -> None:
    storage = await _open_storage(tmp_path)
    calls = 0

    async def _failing_record(source, outcome, *, count=1, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("disk full")

    app = _GateApp()
    app.storage = SimpleNamespace(record_inbound_pressure=_failing_record)
    sink = _bind_sink(app)
    sink(_report(outcome="overflow"))
    # The flush path fails; the sink already returned and the failure is
    # contained inside the single-flight flusher (logged, batch retained
    # for a later flush or the shutdown drain).
    await _await_flush_settled(app)
    assert calls == 1
    assert app._pressure_pending

    # Recovery: a later drain persists the retained batch unchanged.
    app.storage = storage
    await app._drain_inbound_pressure_flush()
    assert app._pressure_pending == {}
    rows = await storage.list_inbound_pressure_observations(source="radio")
    assert [(r["outcome"], r["count"]) for r in rows] == [("overflow", 1)]
    await storage.close()


async def test_producer_reports_and_gate_refusals_share_one_flush(
    tmp_path: Path,
) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    try:
        sink(_report(outcome="overflow"))
        # The gate path stages the admission-gate vocabulary into the
        # same pending store and flush path.
        refused = SimpleNamespace(reason="queue_full")
        app._record_inbound_pressure_loss("radio", refused, cursor_aware=False)
        await _await_flush_settled(app)

        rows = await storage.list_inbound_pressure_observations(source="radio")
        by_outcome = {r["outcome"]: r["count"] for r in rows}
        assert by_outcome == {"overflow": 1, "rejected": 1}
    finally:
        await storage.close()


def test_staging_bound_constant_is_documented() -> None:
    assert PRODUCER_PRESSURE_MAX_PENDING_KEYS > 0
    assert isinstance(PRODUCER_PRESSURE_MAX_PENDING_KEYS, int)


def test_cli_payload_shape_is_json(tmp_path: Path) -> None:
    """Producer outcomes reach the operator surface as durable rows."""

    async def _seed() -> None:
        storage = await _open_storage(tmp_path)
        try:
            await storage.record_inbound_pressure("radio", "overflow", count=3)
            await storage.record_inbound_pressure("radio", "cancelled_before_admission")
        finally:
            await storage.close()

    asyncio.run(_seed())
    from tests.helpers.cli import _run_cli

    out = _run_cli(
        "inspect",
        "pressure",
        "--storage-path",
        str(tmp_path / "pressure.db"),
    )
    payload = json.loads(out)
    outcomes = {row["outcome"]: row["count"] for row in payload["observations"]}
    assert outcomes == {"overflow": 3, "cancelled_before_admission": 1}


async def test_final_drain_absorbs_sdk_thread_staging_before_close(
    tmp_path: Path,
) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    try:
        # No owner-loop absorb callback has run yet: final drain must pull
        # this batch synchronously before closing the database.
        sink(_report(outcome="overflow", count=4))
        assert not getattr(app, "_pressure_pending", None)
        await app._drain_inbound_pressure_flush()
        rows = await storage.list_inbound_pressure_observations(source="radio")
        assert [(row["outcome"], row["count"]) for row in rows] == [("overflow", 4)]
        # The final-drain fence also refuses late SDK-thread reports.
        sink(_report(outcome="scheduled"))
        await asyncio.sleep(0)
        assert not getattr(app, "_pressure_pending", None)
        assert not app._producer_pressure_buffer
    finally:
        await storage.close()


async def test_delayed_absorb_after_storage_closed_does_not_restart_flush(
    tmp_path: Path,
) -> None:
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    sink(_report())
    await app._drain_inbound_pressure_flush()
    prior = app._pressure_flush_task
    await storage.close()
    app._storage_initialized = False
    # A queued SDK callback that runs after close cannot schedule another
    # SQLite writer or retain stale counts against a retired store.
    app._absorb_producer_pressure()
    sink(_report(outcome="scheduled"))
    await asyncio.sleep(0)
    assert app._pressure_flush_task is prior
    assert app._pressure_pending == {}


async def test_retained_producer_keys_bound_without_evicting_gate_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def failing_record(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("storage unavailable")

    app = _GateApp()
    app.storage = SimpleNamespace(record_inbound_pressure=failing_record)
    sink = _bind_sink(app)
    monkeypatch.setattr(_app_module, "PRODUCER_PRESSURE_MAX_PENDING_KEYS", 2)
    monkeypatch.setattr(_app_module._time, "time", lambda: 1_800_000_000.0)
    app._record_inbound_pressure_loss(
        "gate", SimpleNamespace(reason="queue_full"), cursor_aware=False
    )
    await _await_flush_settled(app)
    for offset in range(4):
        at = 1_800_000_000.0 + offset * 120
        monkeypatch.setattr(_app_module._time, "time", lambda t=at: t)
        sink(_report(outcome="overflow"))
        await _await_flush_settled(app)
    assert calls >= 5
    pending = app._pressure_pending
    assert [key for key in pending if key[1] == "rejected"] == [
        ("gate", "rejected", pressure_window_start(1_800_000_000))
    ]
    windows = {key[2] for key in pending if key[1] == "overflow"}
    assert windows == {
        pressure_window_start(1_800_000_240),
        pressure_window_start(1_800_000_360),
    }


async def test_pressure_count_saturates_across_merged_reports_and_sqlite_upserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceiling = (1 << 63) - 1
    storage = await _open_storage(tmp_path)
    app = _GateApp()
    app.storage = storage
    sink = _bind_sink(app)
    monkeypatch.setattr(_app_module._time, "time", lambda: 1_800_000_000.0)
    try:
        sink(_report(count=ceiling))
        sink(_report(count=1))
        await _await_flush_settled(app)
        # The in-memory merge saturates and SQLite's conflict upsert
        # independently saturates when a later flush arrives.
        sink(_report(count=2))
        await _await_flush_settled(app)
        rows = await storage.list_inbound_pressure_observations(source="radio")
        assert len(rows) == 1
        assert rows[0]["count"] == ceiling
        assert isinstance(rows[0]["count"], int)
    finally:
        await storage.close()


async def test_storage_rejects_out_of_range_pressure_counts(tmp_path: Path) -> None:
    storage = await _open_storage(tmp_path)
    try:
        with pytest.raises(ValueError, match="within SQLite range"):
            await storage.record_inbound_pressure("radio", "overflow", count=(1 << 63))
        assert await storage.list_inbound_pressure_observations(source="radio") == []
    finally:
        await storage.close()
