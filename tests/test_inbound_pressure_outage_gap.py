"""Accounting-gap evidence for admission-gate refusals under storage outage.

Covers the durable half of the bounded-retention decision: the
``inbound_pressure_outage_gaps`` table installs through the allowlisted
additive predecessor path, its records round-trip content-free through
the storage authority surface, and record validation refuses malformed
counters.  The runtime half — bounded retention, discard accounting, and
gap persistence on recovery — is qualified in this suite's runtime
scenarios and in ``tests/test_sustained_ingress_contention.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.sqlite._pressure import SQLITE_MAX_PRESSURE_COUNT
from medre.core.storage.sqlite.schema import _PRESSURE_OUTAGE_GAPS_DDL
from medre.core.storage.sqlite.schema_validation import execute_ddl
from medre.core.storage.sqlite.storage import SQLiteStorage

pytestmark = pytest.mark.usefixtures("isolated_config_env")

#: Fixed epoch for deterministic interval bounds; windows advance by
#: explicit values, never by real elapsed time.
_BASE_EPOCH = 1_800_000_000

_GAP_FIELDS = frozenset(
    {
        "gap_id",
        "opened_at",
        "recovered_at",
        "refusals_observed",
        "refusals_retained",
        "refusals_discarded",
    }
)


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
