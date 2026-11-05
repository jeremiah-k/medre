"""Accounting-gap evidence for admission-gate refusals under storage outage.

Covers both halves of the bounded-retention decision for authoritative
admission-gate refusal evidence.  The durable half: the
``inbound_pressure_outage_gaps`` table installs through the allowlisted
additive predecessor path, its records round-trip content-free through
the storage authority surface, and record validation refuses malformed
counters.  The runtime half: retained refusal keys are bounded by a
documented constant, every discarded fact is counted unconditionally
into saturating counters, the loss is logged coalesced (never per-row,
never silent), the first fully successful flush after the outage
persists one content-free accounting-gap record, and that record lets a
reader distinguish zero refusals during the outage from refusals with
discarded retained facts.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.core.contracts.adapter import PRODUCER_PRESSURE_OUTCOMES
from medre.core.contracts.adapter import ProducerPressureReport
from medre.core.storage.sqlite._pressure import SQLITE_MAX_PRESSURE_COUNT
from medre.core.storage.sqlite._pressure import pressure_window_start
from medre.core.storage.sqlite.schema import _PRESSURE_OUTAGE_GAPS_DDL
from medre.core.storage.sqlite.schema_validation import execute_ddl
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime import app as _app_module
from tests.helpers.inbound_gate import GateAppDouble as _GateApp

pytestmark = pytest.mark.usefixtures("isolated_config_env")

#: Fixed epoch for deterministic interval bounds; windows advance by
#: explicit values, never by real elapsed time.
_BASE_EPOCH = 1_800_000_000

_GAP_FIELDS = frozenset(
    {
        "gap_id",
        "gap_token",
        "opened_at",
        "recovered_at",
        "refusals_observed",
        "refusals_retained",
        "refusals_discarded",
    }
)

#: Gate-refusal bound used where the eviction math is asserted.
_FLOODED_GATE_BOUND = 2


def _expected_gap_table_sql() -> str:
    """Derive the exact stored form from the authoritative DDL slice.

    The same derivation scheme as the prerelease identity validators: the
    expected catalog definition comes from the declared DDL, never from a
    hand-written string, so detection cannot drift from the declared shape.
    """
    reference = sqlite3.connect(":memory:")
    try:
        execute_ddl(reference, _PRESSURE_OUTAGE_GAPS_DDL)
        row = reference.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='inbound_pressure_outage_gaps'"
        ).fetchone()
    finally:
        reference.close()
    assert row is not None
    return str(row[0])


def _gap_table_sql(db_path: Path) -> str | None:
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='inbound_pressure_outage_gaps'"
        ).fetchone()
    finally:
        db.close()
    return None if row is None else str(row[0])


async def _open_storage(tmp_path: Path, name: str) -> SQLiteStorage:
    storage = SQLiteStorage(str(tmp_path / name))
    await storage.initialize()
    return storage


def _drop_gap_table(db_path: Path) -> None:
    """Reduce a current-shape database to the allowlisted predecessor.

    Dropping exactly the accounting-gap table from a stamped,
    otherwise-byte-exact database yields the approved additive
    predecessor layout: every other catalog object already matches the
    declared shape.
    """
    db = sqlite3.connect(db_path)
    try:
        db.execute("DROP TABLE inbound_pressure_outage_gaps")
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Additive installation
# ---------------------------------------------------------------------------


async def test_additive_predecessor_gains_the_gap_table(tmp_path: Path) -> None:
    """A stamped database without the gap table gains it on open."""
    storage = await _open_storage(tmp_path, "predecessor.db")
    await storage.close()
    db_path = tmp_path / "predecessor.db"
    _drop_gap_table(db_path)
    assert _gap_table_sql(db_path) is None

    migrated = SQLiteStorage(str(db_path))
    try:
        await migrated.initialize()
        installed = _gap_table_sql(db_path)
        expected = _expected_gap_table_sql()
        assert installed == expected
        # The installed table is immediately usable through the authority
        # surface, and a second open is a no-op for the table shape.
        await migrated.record_inbound_pressure_outage_gap(
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 90,
            refusals_observed=0,
            refusals_retained=0,
            refusals_discarded=0,
        )
        rows = await migrated.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
    finally:
        await migrated.close()

    reopened = SQLiteStorage(str(db_path))
    try:
        await reopened.initialize()
        assert _gap_table_sql(db_path) == expected
        rows = await reopened.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
    finally:
        await reopened.close()


async def test_exact_legacy_gap_table_upgrade_preserves_rows(tmp_path: Path) -> None:
    """A previously opened #462 prerelease database retains its gap history."""
    from medre.core.storage.sqlite._prerelease_migrate import (
        known_outage_gap_table_forms,
        outage_gap_token_rebuild_applies,
    )

    db_path = tmp_path / "legacy-gap.db"
    storage = await _open_storage(tmp_path, db_path.name)
    try:
        await storage.record_inbound_pressure_outage_gap(
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 30,
            refusals_observed=5,
            refusals_retained=3,
            refusals_discarded=2,
        )
    finally:
        await storage.close()

    columns = known_outage_gap_table_forms().columns
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "ALTER TABLE inbound_pressure_outage_gaps "
            "RENAME TO inbound_pressure_outage_gaps__old"
        )
        # Literal predecessor from e9816b4e26; do not derive it from the detector.
        db.execute("""CREATE TABLE IF NOT EXISTS inbound_pressure_outage_gaps (
    gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    recovered_at TEXT NOT NULL,
    refusals_observed INTEGER NOT NULL CHECK (refusals_observed >= 0),
    refusals_retained INTEGER NOT NULL CHECK (refusals_retained >= 0),
    refusals_discarded INTEGER NOT NULL CHECK (refusals_discarded >= 0)
);""")
        assert outage_gap_token_rebuild_applies(db)
        db.execute(
            f"INSERT INTO inbound_pressure_outage_gaps ({', '.join(columns)}) "
            f"SELECT {', '.join(columns)} FROM inbound_pressure_outage_gaps__old"
        )
        db.execute("DROP TABLE inbound_pressure_outage_gaps__old")
        db.commit()
    finally:
        db.close()

    upgraded = SQLiteStorage(str(db_path))
    try:
        await upgraded.initialize()
        rows = await upgraded.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
        assert rows[0]["gap_id"] == 1
        assert rows[0]["gap_token"] == "0" * 31 + "1"
        assert rows[0]["refusals_observed"] == 5
        assert rows[0]["refusals_retained"] == 3
        assert rows[0]["refusals_discarded"] == 2
        assert _gap_table_sql(db_path) == _expected_gap_table_sql()
        await upgraded.record_inbound_pressure_outage_gap(
            gap_token=rows[0]["gap_token"],
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 30,
            refusals_observed=5,
            refusals_retained=3,
            refusals_discarded=2,
        )
        assert await upgraded.list_inbound_pressure_outage_gaps() == rows
    finally:
        await upgraded.close()

    reopened = SQLiteStorage(str(db_path))
    try:
        await reopened.initialize()
        assert await reopened.list_inbound_pressure_outage_gaps() == rows
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Authority surface: content-free record and listing
# ---------------------------------------------------------------------------


async def test_gap_record_round_trips_content_free(tmp_path: Path) -> None:
    """Records carry interval bounds and saturating counters only."""
    storage = await _open_storage(tmp_path, "gaps.db")
    try:
        await storage.record_inbound_pressure_outage_gap(
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 75,
            refusals_observed=6,
            refusals_retained=4,
            refusals_discarded=2,
        )
        await storage.record_inbound_pressure_outage_gap(
            opened_unix_seconds=_BASE_EPOCH + 600,
            recovered_unix_seconds=_BASE_EPOCH + 660,
            refusals_observed=0,
            refusals_retained=0,
            refusals_discarded=0,
        )
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 2
        for row in rows:
            assert set(row) == _GAP_FIELDS
        first, second = rows
        assert first["refusals_observed"] == 6
        assert first["refusals_retained"] == 4
        assert first["refusals_discarded"] == 2
        assert second["refusals_observed"] == 0
        assert first["opened_at"] < second["opened_at"]
        assert isinstance(first["gap_id"], int)

        limited = await storage.list_inbound_pressure_outage_gaps(limit=1)
        assert [row["gap_id"] for row in limited] == [second["gap_id"]]
    finally:
        await storage.close()


async def test_gap_record_rejects_malformed_accounting(tmp_path: Path) -> None:
    """Counter validation refuses values outside the SQLite range."""
    storage = await _open_storage(tmp_path, "invalid.db")
    try:
        for field in (
            "refusals_observed",
            "refusals_retained",
            "refusals_discarded",
        ):
            counters = {
                "refusals_observed": 0,
                "refusals_retained": 0,
                "refusals_discarded": 0,
            }
            for bad_value in (-1, SQLITE_MAX_PRESSURE_COUNT + 1):
                counters[field] = bad_value
                with pytest.raises(ValueError):
                    await storage.record_inbound_pressure_outage_gap(
                        opened_unix_seconds=_BASE_EPOCH,
                        recovered_unix_seconds=_BASE_EPOCH,
                        **counters,
                    )
        with pytest.raises(ValueError):
            await storage.record_inbound_pressure_outage_gap(
                opened_unix_seconds=_BASE_EPOCH + 10,
                recovered_unix_seconds=_BASE_EPOCH,
                refusals_observed=0,
                refusals_retained=0,
                refusals_discarded=0,
            )
        assert await storage.list_inbound_pressure_outage_gaps() == []
    finally:
        await storage.close()


async def test_gap_token_idempotent_and_conflicts_fail_closed(tmp_path: Path) -> None:
    """The same token retries once and cannot be rewritten with new facts."""
    storage = await _open_storage(tmp_path, "token.db")
    token = "a" * 32
    kwargs = dict(
        gap_token=token,
        opened_unix_seconds=_BASE_EPOCH,
        recovered_unix_seconds=_BASE_EPOCH + 10,
        refusals_observed=3,
        refusals_retained=2,
        refusals_discarded=1,
    )
    try:
        await storage.record_inbound_pressure_outage_gap(**kwargs)
        await storage.record_inbound_pressure_outage_gap(**kwargs)
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1 and rows[0]["gap_token"] == token
        with pytest.raises(ValueError, match="conflicting immutable"):
            await storage.record_inbound_pressure_outage_gap(
                **{**kwargs, "refusals_discarded": 2}
            )
        assert await storage.list_inbound_pressure_outage_gaps() == rows
        with pytest.raises(ValueError, match="gap_token"):
            await storage.record_inbound_pressure_outage_gap(
                **{**kwargs, "gap_token": "not-a-token"}
            )
    finally:
        await storage.close()


async def test_gap_snapshot_retries_after_post_commit_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canceled await after commit cannot duplicate the immutable gap."""
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    storage = await _open_storage(tmp_path, "cancel-commit.db")
    app = _outage_app(storage)
    app._pressure_outage = _app_module._PressureOutageAccounting(
        opened_at=_BASE_EPOCH, observed=3, discarded=1
    )
    original_write = storage._write
    did_cancel = False

    async def committed_then_cancel(sql: str, params: object = ()) -> object:
        nonlocal did_cancel
        result = await original_write(sql, params)
        if "INSERT INTO inbound_pressure_outage_gaps" in sql and not did_cancel:
            did_cancel = True
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(storage, "_write", committed_then_cancel)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _app_module._settle_pressure_outage_record(app, 2)
        assert did_cancel
        assert app._pressure_outage is not None
        token = app._pressure_outage.settlement.token
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1 and rows[0]["gap_token"] == token
        await _app_module._settle_pressure_outage_record(app, 0)
        assert app._pressure_outage is None
        after = await storage.list_inbound_pressure_outage_gaps()
        assert after == rows
        assert after[0]["refusals_retained"] == 2
    finally:
        await storage.close()


def test_older_refusal_remerge_does_not_evict_newer_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a failed write, old facts cannot replace new retained facts."""
    monkeypatch.setattr(_app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", 1)
    now = _BASE_EPOCH + 600
    newer = ("radio", "rejected", pressure_window_start(now))
    older = ("radio", "rejected", pressure_window_start(now - 120))
    pending = {newer: (3, now, now)}
    app = SimpleNamespace(_pressure_outage=None)
    _app_module._merge_retained_gate_pressure(
        pending, older, (2, now - 120, now - 120), app
    )
    assert pending == {newer: (3, now, now)}
    assert app._pressure_outage.observed == 3
    assert app._pressure_outage.discarded == 2


async def test_recovery_timestamp_is_clamped_after_backward_clock_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wall-clock rollback cannot permanently prevent gap recovery."""
    clock = _Clock(_BASE_EPOCH - 100)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    storage = await _open_storage(tmp_path, "clock.db")
    app = _outage_app(storage)
    app._pressure_outage = _app_module._PressureOutageAccounting(
        opened_at=_BASE_EPOCH, observed=1, discarded=0
    )
    try:
        await _app_module._settle_pressure_outage_record(app, 1)
        assert app._pressure_outage is None
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1 and rows[0]["recovered_at"] == rows[0]["opened_at"]
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Runtime helpers (production seams, mirroring the sustained-contention
# suite's harness shape)
# ---------------------------------------------------------------------------


class _Clock:
    """Test-controlled wall clock so windows advance only by explicit steps."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def _outage_app(storage: object) -> _GateApp:
    """Assemble the production-seam double for one outage scenario."""
    app = _GateApp()
    app.storage = storage
    app._capacity_controller = None
    app._runtime_accounting = None
    return app


async def _await_flush_settled(app: _GateApp) -> None:
    """Deterministically drain the single-flight pressure flush task."""
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None and not task.done():
        await task


def _refuse(app: _GateApp, source: str, *, cursor_aware: bool = False) -> None:
    """Record one gate refusal through the production refusal path."""
    app._record_inbound_pressure_loss(
        source, SimpleNamespace(reason="queue_full"), cursor_aware=cursor_aware
    )


def _gate_half(
    pending: dict[tuple[str, str, int], tuple[int, int, int]],
) -> dict[tuple[str, str, int], tuple[int, int, int]]:
    """Return the gate-refusal half of the retained pending store."""
    return {
        key: aggregate
        for key, aggregate in pending.items()
        if key[1] not in PRODUCER_PRESSURE_OUTCOMES
    }


def _marker_count(caplog: pytest.LogCaptureFixture, marker: str) -> int:
    return sum(1 for message in caplog.messages if message.startswith(marker))


# ---------------------------------------------------------------------------
# Bounded retention, discard accounting, and coalesced loss logging
# ---------------------------------------------------------------------------


async def test_gate_refusal_retention_is_bounded_and_accounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Overflow of retained refusal keys is counted, logged, never silent.

    Sustained distinct-window refusals against a failing store exceed the
    documented bound: the oldest refusal keys are discarded, each evicted
    fact moves into the saturating discard counter, the discard logs are
    coalesced to at most one per 5-second window carrying cumulative
    counts, and no exception reaches the refusal path.
    """
    caplog.set_level(logging.WARNING, logger="medre.runtime.app")
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(
        _app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", _FLOODED_GATE_BOUND
    )

    async def _failing_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=_failing_record))
    try:
        # First refusal: its flush fails, which opens the accounting gap.
        clock.now = _BASE_EPOCH
        _refuse(app, "src-0")
        await _await_flush_settled(app)
        accounting = app._pressure_outage
        assert accounting is not None
        assert accounting.observed == 1  # failed first write belongs to the gap

        # Five more refusals from distinct sources against the failing
        # store: four land inside one 5-second window, the last after the
        # window advances.  The bound of two retains only the newest
        # keys; four evicted keys land in the discard counter.
        for index in range(1, 6):
            if index == 5:
                clock.now = _BASE_EPOCH + 10
            _refuse(app, f"src-{index}")
            await _await_flush_settled(app)

        pending = app._pressure_pending
        gate_keys = _gate_half(pending)
        assert len(gate_keys) <= _FLOODED_GATE_BOUND
        assert {key[0] for key in gate_keys} == {"src-4", "src-5"}
        assert accounting.observed == 6
        assert accounting.discarded == 4

        # Coalesced visibility: one line for the first discard carrying
        # its cumulative count, none for the suppressed same-window
        # discards, one more after the window advances carrying the
        # cumulative totals.
        overflow_lines = [
            message
            for message in caplog.messages
            if message.startswith("INBOUND_PRESSURE_RETENTION_OVERFLOW")
        ]
        assert len(overflow_lines) == 2
        assert "discarded=1 observed=2" in overflow_lines[0]
        # The arriving refusal's own count lands after its eviction line.
        assert "discarded=4 observed=5" in overflow_lines[1]
        assert _marker_count(caplog, "INBOUND_PRESSURE_RECORD_FAILED") >= 1
        assert app._pressure_outage is not None
    finally:
        app._pressure_flush_task = None


async def test_saturating_counters_clamp_at_the_sqlite_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discard and observed counters saturate instead of overflowing.

    The signed-64-bit saturation pattern bounds every pressure count; a
    retained aggregate already at the ceiling evicts into a discard
    counter that stays at the ceiling, and further refusals leave the
    observed counter unchanged.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(_app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", 1)

    def _ceiling_aggregate(
        window: int,
    ) -> tuple[tuple[str, str, int], tuple[int, int, int]]:
        return (
            ("radio-a", "rejected", window),
            (SQLITE_MAX_PRESSURE_COUNT, window, window),
        )

    pending: dict[tuple[str, str, int], tuple[int, int, int]] = {}
    key, aggregate = _ceiling_aggregate(pressure_window_start(_BASE_EPOCH))
    pending[key] = aggregate
    app = SimpleNamespace(_pressure_outage=None)
    next_key = ("radio-a", "rejected", pressure_window_start(_BASE_EPOCH + 120))
    _app_module._merge_retained_gate_pressure(
        pending, next_key, (1, _BASE_EPOCH + 120, _BASE_EPOCH + 120), app
    )
    accounting = app._pressure_outage
    assert accounting is not None
    assert accounting.discarded == SQLITE_MAX_PRESSURE_COUNT
    assert accounting.observed == SQLITE_MAX_PRESSURE_COUNT
    assert set(pending) == {next_key}

    # Repeated evictions leave the discard counter saturated, and an
    # observed counter at the ceiling stays there when further refusals
    # are counted through the production refusal path.
    for offset in (2, 3):
        _app_module._merge_retained_gate_pressure(
            pending,
            ("radio-a", "rejected", pressure_window_start(_BASE_EPOCH + offset * 120)),
            (1, _BASE_EPOCH + offset * 120, _BASE_EPOCH + offset * 120),
            app,
        )
    assert accounting.discarded == SQLITE_MAX_PRESSURE_COUNT

    async def _failing_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    outage_app = _outage_app(SimpleNamespace(record_inbound_pressure=_failing_record))
    outage_app._pressure_outage = _app_module._PressureOutageAccounting(
        opened_at=_BASE_EPOCH, observed=SQLITE_MAX_PRESSURE_COUNT
    )
    clock.now = _BASE_EPOCH + 4 * 120
    _refuse(outage_app, "radio-a")
    await _await_flush_settled(outage_app)
    assert outage_app._pressure_outage.observed == SQLITE_MAX_PRESSURE_COUNT
    assert outage_app._pressure_outage.discarded == 0


def test_merge_dispatcher_honors_per_half_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer and gate halves bound independently; neither evicts the other.

    The flush re-merge path routes through the same dispatcher as the
    refusal path, so a failed batch's gate keys re-enter under the
    refusal bound and producer keys under the producer bound.
    """
    monkeypatch.setattr(_app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", 1)
    gate_key = ("radio-a", "rejected", pressure_window_start(_BASE_EPOCH))
    producer_key = ("radio-a", "scheduled", pressure_window_start(_BASE_EPOCH + 180))
    app = SimpleNamespace(_pressure_outage=None)
    pending: dict[tuple[str, str, int], tuple[int, int, int]] = {
        gate_key: (1, _BASE_EPOCH, _BASE_EPOCH)
    }

    # Producer flood past its own bound never evicts the gate key; the
    # producer allowance is reduced to exercise eviction with four windows.
    monkeypatch.setattr(_app_module, "PRODUCER_PRESSURE_MAX_PENDING_KEYS", 2)
    for offset in range(4):
        _app_module._merge_retained_pressure(
            pending,
            ("radio-a", "scheduled", pressure_window_start(_BASE_EPOCH + offset * 60)),
            (1, _BASE_EPOCH + offset * 60, _BASE_EPOCH + offset * 60),
            app,
        )
    assert gate_key in pending
    assert producer_key in pending
    assert len(pending) == 3  # two producer keys and one gate key
    assert app._pressure_outage is None  # producer eviction never opens a gate gap

    # Gate arrival past the refusal bound evicts the oldest gate key with
    # accounting; producer keys are never candidates.
    late_gate = ("radio-a", "timed_out", pressure_window_start(_BASE_EPOCH + 120))
    _app_module._merge_retained_pressure(
        pending, late_gate, (1, _BASE_EPOCH + 120, _BASE_EPOCH + 120), app
    )
    assert gate_key not in pending
    assert late_gate in pending
    assert producer_key in pending
    assert app._pressure_outage is not None
    assert app._pressure_outage.discarded == 1


# ---------------------------------------------------------------------------
# Recovery persists the accounting-gap record
# ---------------------------------------------------------------------------


async def test_recovery_persists_one_content_free_gap_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first successful flush after the outage persists the record.

    One row per detected outage: the interval bounds plus the exact
    observed, retained, and discarded counters.  The recovery batch's
    gate-half total is the retained figure, the gap closes, and the
    durable rows distinguish zero-refusal outages from lossy ones.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(
        _app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", _FLOODED_GATE_BOUND
    )

    async def _failing_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=_failing_record))
    clock.now = _BASE_EPOCH
    _refuse(app, "radio-a")
    await _await_flush_settled(app)
    for offset in range(1, 5):
        clock.now = _BASE_EPOCH + offset * 120
        _refuse(app, "radio-a")
        _refuse(app, "relay-b", cursor_aware=True)
        await _await_flush_settled(app)
    accounting = app._pressure_outage
    assert accounting is not None
    # Nine distinct gate keys against the bound of two: the initial
    # failed write is included when the gap opens, and seven evicted
    # aggregates (carrying seven facts) are discarded.
    observed_before = accounting.observed
    discarded_before = accounting.discarded
    assert observed_before == 9
    assert discarded_before == 7

    storage = await _open_storage(tmp_path, "recovery.db")
    try:
        app.storage = storage
        clock.now = _BASE_EPOCH + 10_000
        await app._drain_inbound_pressure_flush()

        # The gap closed on the first fully successful batch and the
        # durable record carries the exact accounting.  The recovery
        # batch's gate-half total (two facts) is the retained figure.
        assert app._pressure_outage is None
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == _GAP_FIELDS
        assert row["refusals_observed"] == observed_before
        assert row["refusals_discarded"] == discarded_before
        assert app._pressure_pending == {}
        assert row["refusals_retained"] == 2
        assert row["opened_at"] <= row["recovered_at"]
        assert row["gap_id"] >= 1

        # Exactly the retained refusal facts became durable alongside the
        # record; the discarded ones are absent and accounted for.
        pressure_rows = await storage.list_inbound_pressure_observations()
        totals: dict[tuple[str, str], int] = {}
        for pressure_row in pressure_rows:
            key = (str(pressure_row["source"]), str(pressure_row["outcome"]))
            totals[key] = totals.get(key, 0) + int(pressure_row["count"])
        assert totals[("radio-a", "rejected")] == 1
        assert totals[("relay-b", "deferred")] == 1
    finally:
        await storage.close()


async def test_zero_refusal_outage_persists_a_zero_counter_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detected outage without gate refusals records observed = 0.

    Producer telemetry failing to persist opens the same gap; the
    recovery record proves the outage happened and that zero gate
    refusals were observed during it — the distinction the durable
    record exists to carry.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)

    async def _failing_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=_failing_record))
    sink = app._producer_pressure_reporter("radio-a")
    assert sink is not None
    sink(ProducerPressureReport(adapter_id="radio-a", outcome="scheduled", count=3))
    app._absorb_producer_pressure()
    await _await_flush_settled(app)
    assert app._pressure_outage is not None
    assert app._pressure_outage.observed == 0

    storage = await _open_storage(tmp_path, "zero.db")
    try:
        app.storage = storage
        clock.now = _BASE_EPOCH + 300
        await app._drain_inbound_pressure_flush()
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
        row = rows[0]
        assert row["refusals_observed"] == 0
        assert row["refusals_retained"] == 0
        assert row["refusals_discarded"] == 0
        assert app._pressure_outage is None
        # The producer facts themselves persisted as best-effort
        # telemetry — never a substitute for the gate accounting.
        pressure_rows = await storage.list_inbound_pressure_observations()
        assert [
            (str(r["source"]), str(r["outcome"]), int(r["count"]))
            for r in pressure_rows
        ] == [("radio-a", "scheduled", 3)]
    finally:
        await storage.close()


async def test_failed_gap_record_write_keeps_the_gap_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap record write failure retries on the next flush success.

    The recovery batch persists even when the record write fails; the
    gap stays open and the next flush invocation settles it, so the
    durable accounting appears exactly once the store accepts it.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)

    gap_calls = 0

    async def _flaky_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    async def _flaky_gap(**kwargs: object) -> None:
        nonlocal gap_calls
        gap_calls += 1
        raise RuntimeError("gap write unavailable")

    storage = SimpleNamespace(
        record_inbound_pressure=_flaky_record,
        record_inbound_pressure_outage_gap=_flaky_gap,
    )
    app = _outage_app(storage)
    clock.now = _BASE_EPOCH
    _refuse(app, "radio-a")
    await _await_flush_settled(app)
    assert app._pressure_outage is not None
    assert gap_calls == 0  # failure opened the gap; nothing settled yet

    recovered_gap_calls = 0
    attempted_records: list[dict[str, object]] = []

    async def _working_record(*args: object, **kwargs: object) -> None:
        return None

    async def _working_gap_first_fails(**kwargs: object) -> None:
        nonlocal recovered_gap_calls
        recovered_gap_calls += 1
        attempted_records.append(kwargs)
        if recovered_gap_calls == 1:
            raise RuntimeError("gap write unavailable")

    app.storage = SimpleNamespace(
        record_inbound_pressure=_working_record,
        record_inbound_pressure_outage_gap=_working_gap_first_fails,
    )
    clock.now = _BASE_EPOCH + 600
    await app._drain_inbound_pressure_flush()
    # First settle attempt failed inside the drain, the empty-pending
    # entry retried it in the same invocation.
    assert recovered_gap_calls >= 1
    if app._pressure_outage is not None:
        await app._drain_inbound_pressure_flush()
    assert app._pressure_outage is None
    assert len(attempted_records) >= 2
    assert all(row["refusals_retained"] == 1 for row in attempted_records)
    assert all(row["refusals_observed"] == 1 for row in attempted_records)
    real = await _open_storage(tmp_path, "audit.db")
    try:
        # Cross-check the shape contract on a real store separately from
        # the flaky double above.
        await real.record_inbound_pressure_outage_gap(
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 1,
            refusals_observed=1,
            refusals_retained=1,
            refusals_discarded=0,
        )
        assert len(await real.list_inbound_pressure_outage_gaps()) == 1
    finally:
        await real.close()


async def test_gap_record_without_gap_method_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend lacking the gap method keeps accounting open, in memory.

    Saturating counters and the coalesced discard log remain the visible
    loss channel; no silent close manufactures a durable record that the
    backend could not store.
    """
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)

    async def _failing_record(*args: object, **kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=_failing_record))
    clock.now = _BASE_EPOCH
    _refuse(app, "radio-a")
    await _await_flush_settled(app)
    assert app._pressure_outage is not None

    async def _working_record(*args: object, **kwargs: object) -> None:
        return None

    app.storage = SimpleNamespace(record_inbound_pressure=_working_record)
    clock.now = _BASE_EPOCH + 120
    await app._drain_inbound_pressure_flush()
    assert app._pressure_outage is not None
    assert app._pressure_pending == {}


async def test_first_failing_batch_counts_every_unwritten_gate_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refused facts in a first failed batch are inside its gap."""
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("pressure store unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=unavailable))
    _refuse(app, "radio-a")
    _refuse(app, "radio-b")
    await _await_flush_settled(app)
    assert app._pressure_outage is not None
    assert app._pressure_outage.observed == 2
    assert app._pressure_outage.discarded == 0

    recorded: list[dict[str, object]] = []

    async def persist(*args: object, **kwargs: object) -> None:
        return None

    async def gap_record(**kwargs: object) -> None:
        recorded.append(kwargs)

    app.storage = SimpleNamespace(
        record_inbound_pressure=persist,
        record_inbound_pressure_outage_gap=gap_record,
    )
    await app._drain_inbound_pressure_flush()
    assert len(recorded) == 1
    assert recorded[0]["refusals_observed"] == 2
    assert recorded[0]["refusals_retained"] == 2
    assert recorded[0]["refusals_discarded"] == 0
    assert app._pressure_outage is None


async def test_refusals_during_gap_record_await_open_a_followup_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusals and evictions during an awaited record survive settlement."""
    clock = _Clock(_BASE_EPOCH)
    monkeypatch.setattr(_app_module._time, "time", clock.time)
    monkeypatch.setattr(_app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", 1)

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("pressure store unavailable")

    app = _outage_app(SimpleNamespace(record_inbound_pressure=unavailable))
    _refuse(app, "radio-a")
    await _await_flush_settled(app)
    assert app._pressure_outage.observed == 1

    entered = asyncio.Event()
    release = asyncio.Event()
    records: list[dict[str, object]] = []
    persisted: list[str] = []

    async def persist(source: str, outcome: str, **kwargs: object) -> None:
        persisted.append(source)

    async def gap_record(**kwargs: object) -> None:
        records.append(kwargs)
        if len(records) == 1:
            entered.set()
            await release.wait()

    app.storage = SimpleNamespace(
        record_inbound_pressure=persist,
        record_inbound_pressure_outage_gap=gap_record,
    )
    # Use the regular single-flight task, not a concurrent manual drain.
    clock.now += 120
    _refuse(app, "radio-b")
    flusher = app._pressure_flush_task
    await asyncio.wait_for(entered.wait(), 3)
    clock.now += 120
    _refuse(app, "radio-c")
    clock.now += 120
    _refuse(app, "radio-d")  # evicts c during the awaited gap write
    release.set()
    await asyncio.wait_for(flusher, 3)

    assert app._pressure_outage is None
    assert len(records) == 2
    first, second = records
    assert first["refusals_observed"] == 2
    assert first["refusals_retained"] == 1
    assert first["refusals_discarded"] == 1
    assert second["refusals_observed"] == 2
    assert second["refusals_retained"] == 1
    assert second["refusals_discarded"] == 1
    assert persisted == ["radio-b", "radio-d"]


async def test_outage_gaps_are_visible_to_inspection_and_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read-only operators see lost refusals even with no pressure rows."""
    import json

    from medre.cli.inspect_commands import _inspect_pressure
    from medre.runtime.evidence._storage_sections import (
        _collect_storage_data_from_backend,
    )

    db_path = tmp_path / "visible.db"
    storage = await _open_storage(tmp_path, "visible.db")
    try:
        await storage.record_inbound_pressure_outage_gap(
            gap_token="b" * 32,
            opened_unix_seconds=_BASE_EPOCH,
            recovered_unix_seconds=_BASE_EPOCH + 60,
            refusals_observed=10,
            refusals_retained=2,
            refusals_discarded=8,
        )
        section = await _collect_storage_data_from_backend(
            storage, str(db_path), None, None
        )
        gaps = section["data"]["inbound_pressure_outage_gaps"]
        assert len(gaps) == 1
        assert gaps[0]["refusals_discarded"] == 8
    finally:
        await storage.close()

    await _inspect_pressure(storage_path=str(db_path), source="radio-elsewhere")
    output = json.loads(capsys.readouterr().out)
    assert output["observations"] == []
    assert output["outcome_totals"] == {}
    assert output["outage_gap_count"] == 1
    assert output["outage_gaps"][0]["gap_token"] == "b" * 32
    assert output["outage_gaps"][0]["refusals_discarded"] == 8


def test_same_window_arrival_replaces_retained_key_regardless_of_source_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lower-sorting source must retain its fresh refusal in a full window."""
    monkeypatch.setattr(_app_module, "INBOUND_PRESSURE_MAX_PENDING_KEYS", 1)
    window = pressure_window_start(_BASE_EPOCH)
    old_key = ("radio-z", "rejected", window)
    new_key = ("radio-a", "rejected", window)
    pending = {old_key: (3, _BASE_EPOCH, _BASE_EPOCH)}
    app = SimpleNamespace(_pressure_outage=None)
    _app_module._merge_retained_gate_pressure(
        pending, new_key, (2, _BASE_EPOCH + 1, _BASE_EPOCH + 1), app
    )
    assert set(pending) == {new_key}
    assert app._pressure_outage.discarded == 3


@pytest.mark.parametrize(
    ("gap_open", "cancelled"), [(True, False), (True, True), (False, False)]
)
async def test_interrupted_recovery_counts_only_durable_gate_prefix(
    tmp_path: Path, gap_open: bool, cancelled: bool
) -> None:
    """Partial recovery retains durable gate facts exactly once, excluding telemetry."""
    storage = await _open_storage(tmp_path, "partial-recovery.db")
    app = _outage_app(storage)
    if gap_open:
        app._pressure_outage = _app_module._PressureOutageAccounting(
            opened_at=_BASE_EPOCH, observed=10
        )
    app._pressure_pending = {
        (source, outcome, pressure_window_start(_BASE_EPOCH)): (
            count,
            _BASE_EPOCH,
            _BASE_EPOCH,
        )
        for source, outcome, count in [
            ("radio-a", "rejected", 2),
            ("radio-a", "scheduled", 7),
            ("radio-b", "rejected", 3),
            ("radio-c", "deferred", 5),
        ]
    }
    calls = 0

    async def interrupted_record(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("storage interrupted after durable prefix")
        await storage.record_inbound_pressure(*args, **kwargs)

    try:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await app._flush_inbound_pressure(interrupted_record)
        else:
            await app._flush_inbound_pressure(interrupted_record)
        assert app._pressure_outage.retained == (2 if gap_open else 0)
        assert len(app._pressure_pending) == 2
        await app._flush_inbound_pressure(storage.record_inbound_pressure)
        assert app._pressure_outage is None
        rows = await storage.list_inbound_pressure_outage_gaps()
        assert len(rows) == 1
        expected = 10 if gap_open else 8
        assert rows[0]["refusals_observed"] == expected
        assert rows[0]["refusals_retained"] == expected
        assert rows[0]["refusals_discarded"] == 0
        pressure = await storage.list_inbound_pressure_observations()
        assert (
            sum(row["count"] for row in pressure if row["outcome"] != "scheduled") == 10
        )
    finally:
        await storage.close()
