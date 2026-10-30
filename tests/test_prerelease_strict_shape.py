"""No additive schema repair while the first-release shape remains unfrozen."""

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.backend import (
    PreReleaseSchemaMismatchError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage


@pytest.mark.parametrize(
    "removed_table",
    [
        "attachment_blobs",
        "event_attachment_associations",
        "inbound_pressure_observations",
    ],
)
async def test_missing_previously_additive_table_is_rejected_before_ddl(
    tmp_path: Path, removed_table: str
) -> None:
    path = tmp_path / "stale.sqlite"
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    await storage.close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(f"DROP TABLE {removed_table}")
    db.commit()
    before = list(
        db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    )
    db.close()
    with pytest.raises(PreReleaseSchemaMismatchError, match=removed_table):
        await SQLiteStorage(str(path)).initialize()
    db = sqlite3.connect(path)
    assert (
        list(
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        == before
    )
    db.close()


async def test_unstamped_nonempty_database_is_not_adopted(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite"
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE unrelated(id INTEGER)")
        db.commit()
    finally:
        db.close()
    with pytest.raises(PreReleaseSchemaMismatchError, match="_medre_schema_meta"):
        await SQLiteStorage(str(path)).initialize()
    db = sqlite3.connect(path)
    try:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='_medre_schema_meta'"
            ).fetchone()
            is None
        )
    finally:
        db.close()


async def test_unexpected_column_is_not_silently_allowed(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    await storage.close()
    db = sqlite3.connect(path)
    try:
        db.execute("ALTER TABLE canonical_events ADD COLUMN forgotten TEXT")
        db.commit()
    finally:
        db.close()
    with pytest.raises(
        PreReleaseSchemaMismatchError, match="unexpected column"
    ) as error:
        await SQLiteStorage(str(path)).initialize()
    assert error.value.missing_columns == []
    assert error.value.unexpected_columns == ["forgotten"]
    assert "missing required columns" not in str(error.value)


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize(
    ("before", "after", "tail"),
    [
        ("event_kind TEXT NOT NULL", "event_kind TEXT", ""),
        ("event_kind TEXT NOT NULL", "event_kind INTEGER NOT NULL", ""),
        ("DEFAULT '[]'", "DEFAULT '[1]'", ""),
        ("CHECK (version = 1)", "CHECK (version IN (1, 2))", ""),
        (
            "REFERENCES canonical_events(event_id)",
            "REFERENCES canonical_events(event_id) ON DELETE CASCADE",
            "",
        ),
        ("", "", "DROP INDEX idx_events_timestamp;"),
        (
            "",
            "",
            "DROP INDEX idx_events_timestamp; CREATE INDEX idx_events_timestamp ON canonical_events(event_id);",
        ),
        (
            "WHERE replay_run_id IS NOT NULL AND replay_run_id <> ''",
            "WHERE replay_run_id IS NOT NULL",
            "",
        ),
        ("", "", "CREATE INDEX extra_index ON canonical_events(event_id);"),
        ("", "", "DROP VIEW delivery_status;"),
        ("", "", "DROP VIEW delivery_status; CREATE VIEW delivery_status AS SELECT 1;"),
        (
            "",
            "",
            "CREATE TRIGGER extra_trigger AFTER INSERT ON plugin_state BEGIN DELETE FROM plugin_state; END;",
        ),
        ("", "", "UPDATE _medre_schema_meta SET value='2';"),
        ("", "", "DELETE FROM _medre_schema_meta;"),
    ],
)
async def test_incompatible_definition_is_refused_without_writing(
    tmp_path: Path, readonly: bool, before: str, after: str, tail: str
) -> None:
    path = tmp_path / "definition.sqlite"
    db = sqlite3.connect(path)
    try:
        ddl = _SCHEMA + _INDEXES
        if before:
            assert before in ddl
            ddl = ddl.replace(before, after, 1)
        db.executescript(ddl)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.executescript(tail)
        db.commit()
    finally:
        db.close()
    original_bytes = path.read_bytes()
    store = SQLiteStorage(str(path))
    try:
        with pytest.raises(StorageInitializationError):
            if readonly:
                await SQLiteStorage.open_readonly(str(path))
            else:
                await store.initialize()
    finally:
        await store.close()
    assert path.read_bytes() == original_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


async def test_view_only_database_is_not_fresh_state(tmp_path: Path) -> None:
    path = tmp_path / "view.sqlite"
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE VIEW orphan AS SELECT 1")
        db.commit()
    finally:
        db.close()
    original_bytes = path.read_bytes()
    with pytest.raises(StorageInitializationError):
        await SQLiteStorage(str(path)).initialize()
    assert path.read_bytes() == original_bytes


async def test_schema_preflight_preserves_literal_path_characters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state#draft.sqlite"
    store = SQLiteStorage(str(path))
    await store.initialize()
    await store.close()
    await store.initialize()
    await store.close()
    reader = await SQLiteStorage.open_readonly(str(path))
    await reader.close()
    assert path.exists()
    assert {p.name for p in tmp_path.iterdir()} <= {
        path.name,
        path.name + "-wal",
        path.name + "-shm",
    }


def test_schema_error_distinguishes_missing_unexpected_and_metadata_details():
    error = PreReleaseSchemaMismatchError(
        "state.sqlite", "events", ["body"], unexpected_columns=["forgotten"]
    )
    assert error.missing_columns == ["body"]
    assert error.unexpected_columns == ["forgotten"]
    assert "missing required columns ['body']" in str(error)
    assert "unexpected columns ['forgotten']" in str(error)
    unstamped = PreReleaseSchemaMismatchError(
        "state.sqlite", "_medre_schema_meta", detail="unstamped database"
    )
    assert unstamped.missing_columns == []
    assert "missing required columns" not in str(unstamped)
    assert "unstamped database" in str(unstamped)
