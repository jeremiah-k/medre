"""Atomic schema and index DDL during SQLite storage initialization.

``sync_open`` and ``sync_create_indexes`` run their DDL scripts as one
explicit transaction.  Per-statement autocommit would force one journal
cycle per ``CREATE`` statement and allow a failed initialize to persist a
half-built database; the single transaction bounds initialization cost and
makes schema creation atomic.  These tests pin the atomicity property and
prove the transactional wrapper produces exactly the schema that
statement-by-statement execution produces.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.sqlite.connection import (
    sync_create_indexes,
    sync_open,
)
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage

_BROKEN_SCHEMA_TAIL = "\nCREATE TABLE __atomicity_probe (id INTEGER"
_BROKEN_INDEX_TAIL = (
    "\nCREATE INDEX __atomicity_probe_idx ON canonical_events(event_id);\n"
    "CREATE INDEX __broken ("
)


def _split_statements(script: str) -> list[str]:
    """Split a DDL script into individually complete statements."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def _master_objects(db: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """Return the (type, name, sql) triples that define the database shape."""
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }


def _index_names(db: sqlite3.Connection) -> set[str]:
    """Return the explicitly created index names in the database."""
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master" " WHERE type = 'index' AND sql IS NOT NULL"
        )
    }


def test_failed_schema_ddl_leaves_no_partial_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema script that fails mid-flight must persist nothing.

    The trailing statement is deliberately incomplete.  Under
    per-statement autocommit every earlier ``CREATE`` would already be
    durable when the broken statement raises; with the single
    transaction the connection closes with an open transaction and
    SQLite rolls the whole schema back.
    """
    monkeypatch.setattr(
        "medre.core.storage.sqlite.connection._SCHEMA",
        _SCHEMA + _BROKEN_SCHEMA_TAIL,
    )
    db_path = tmp_path / "atomic-schema.db"
    with pytest.raises(sqlite3.Error):
        sync_open(str(db_path))

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = ro.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        ro.close()
    assert tables == []


async def test_failed_index_ddl_leaves_no_partial_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing index script must persist none of its earlier statements.

    The patched tail first creates a well-formed probe index and only
    then raises, so per-statement autocommit would leave the probe
    durable; the single transaction must roll it back.
    """
    storage = SQLiteStorage(db_path=str(tmp_path / "atomic-indexes.db"))
    await storage.initialize()
    try:
        before = _index_names(storage._db)
        assert before, "schema initialization must create indexes"
        monkeypatch.setattr(
            "medre.core.storage.sqlite.connection._INDEXES",
            _INDEXES + _BROKEN_INDEX_TAIL,
        )
        with pytest.raises(sqlite3.Error):
            sync_create_indexes(storage._db)
    finally:
        await storage.close()

    ro = sqlite3.connect(f"file:{tmp_path / 'atomic-indexes.db'}?mode=ro", uri=True)
    try:
        assert _index_names(ro) == before
    finally:
        ro.close()


def test_failed_initial_indexes_roll_back_tables_and_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "medre.core.storage.sqlite.connection._INDEXES",
        _INDEXES + _BROKEN_INDEX_TAIL,
    )
    path = tmp_path / "initial-index-failure.db"
    with pytest.raises(sqlite3.Error):
        sync_open(str(path))
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        db.close()


def test_transactional_schema_matches_statement_by_statement_reference(
    tmp_path: Path,
) -> None:
    """The single-transaction wrapper changes cost, not content.

    The reference database executes the unmodified DDL one statement at
    a time (statement-level autocommit, the non-transactional
    semantics).  Both databases must expose an identical
    ``sqlite_master`` shape.
    """
    db_path = tmp_path / "transactional.db"
    db = sync_open(str(db_path))
    try:
        sync_create_indexes(db)
        transactional = _master_objects(db)
    finally:
        db.close()

    reference_path = tmp_path / "reference.db"
    reference = sqlite3.connect(str(reference_path))
    try:
        for statement in _split_statements(_SCHEMA) + _split_statements(_INDEXES):
            reference.execute(statement)
        reference.commit()
        expected = _master_objects(reference)
    finally:
        reference.close()

    assert transactional == expected
    assert transactional  # the comparison is only meaningful over real DDL


def test_open_database_wal_mode_without_journal_artifacts(tmp_path: Path) -> None:
    """A freshly opened database uses WAL and holds no stray objects."""
    db = sync_open(str(tmp_path / "wal.db"))
    try:
        mode = db.execute("PRAGMA journal_mode").fetchone()
        assert str(mode[0]) == "wal"
        internal = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE name LIKE 'sqlite_%' AND sql IS NOT NULL"
            )
        }
        # AUTOINCREMENT tables require the sequence bookkeeping table;
        # any other declared internal object is transaction or journal
        # residue (implicit autoindexes carry NULL sql and are excluded).
        assert internal == {"sqlite_sequence"}
    finally:
        db.close()
