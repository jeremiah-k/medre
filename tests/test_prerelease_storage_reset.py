"""Tests for pre-release storage reset UX: schema mismatch error diagnostics.

Verifies that old-shape pre-release databases produce actionable errors that
include the database path, the affected table, and the missing columns —
giving operators clear guidance to preserve state and choose recovery or reset.

Covers:

- ``PreReleaseSchemaMismatchError`` attributes (path, table, missing_columns).
- Error message includes the database file path.
- Error message names the affected table and missing columns.
- ``_EXPECTED_SCHEMA_VERSION`` remains at 1 during prerelease.
- ``open_readonly`` raises the same structured error for old-shape DBs.
- Unsupported catalogs (a narrowed declared CHECK) are refused without
  modifying the database.
- Reopening a current-catalog database leaves the catalog unchanged.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    PreReleaseSchemaMismatchError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.schema import (
    _EXPECTED_SCHEMA_VERSION,
    _INDEXES,
    _SCHEMA,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Isolate the obsolete relation shape from unrelated schema additions, so
# diagnostics name precisely the missing relation column.
_relation_start = _SCHEMA.index("CREATE TABLE IF NOT EXISTS event_relations (")
_relation_end = _SCHEMA.index("\n);", _relation_start)
_OLD_SHAPE_DDL = (
    _SCHEMA[:_relation_start]
    + _SCHEMA[_relation_start:_relation_end].replace(
        "    target_native_thread_id TEXT,\n", "", 1
    )
    + _SCHEMA[_relation_end:]
)


def _create_old_shape_db() -> str:
    """Create a temporary old-shape database and return its path.

    The caller is responsible for deleting the file.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(_OLD_SHAPE_DDL)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    return db_path


# ---------------------------------------------------------------------------
# Tests: schema mismatch error includes database path
# ---------------------------------------------------------------------------


async def test_schema_mismatch_includes_db_path() -> None:
    """PreReleaseSchemaMismatchError.message includes the database file path."""
    db_path = _create_old_shape_db()
    try:
        storage = SQLiteStorage(db_path=db_path)
        with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
            await storage.initialize()

        # The error message must include the database path so the operator
        # can identify which file needs attention.
        assert db_path in str(exc_info.value)

        # The structured attribute is also available.
        assert exc_info.value.path == db_path
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: schema mismatch error names table and missing columns
# ---------------------------------------------------------------------------


async def test_schema_mismatch_names_table_and_columns() -> None:
    """PreReleaseSchemaMismatchError names the affected table and the missing columns."""
    db_path = _create_old_shape_db()
    try:
        storage = SQLiteStorage(db_path=db_path)
        with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
            await storage.initialize()

        err = exc_info.value
        # The event_relations table is missing target_native_thread_id.
        assert err.table == "event_relations"
        assert "target_native_thread_id" in err.missing_columns

        # The error message also contains the table name and column name.
        msg = str(err)
        assert "event_relations" in msg
        assert "target_native_thread_id" in msg
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: expected schema version stays at 1 during prerelease
# ---------------------------------------------------------------------------


def test_expected_schema_version_remains_1() -> None:
    """_EXPECTED_SCHEMA_VERSION is 1 for the entire prerelease period.

    Schema version stays at 1 until storage compatibility becomes
    release-tracked.  DDL shape changes during prerelease do not require
    a version bump; shape validation catches old builds instead.
    """
    assert _EXPECTED_SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Tests: open_readonly also detects old shape
# ---------------------------------------------------------------------------


async def test_schema_mismatch_on_open_readonly() -> None:
    """open_readonly raises PreReleaseSchemaMismatchError for old-shape DBs."""
    db_path = _create_old_shape_db()
    try:
        with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
            await SQLiteStorage.open_readonly(db_path)

        assert exc_info.value.table == "event_relations"
        assert "target_native_thread_id" in exc_info.value.missing_columns
        assert db_path in str(exc_info.value)
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: error is a StorageInitializationError subclass
# ---------------------------------------------------------------------------


async def test_schema_mismatch_is_initialization_error_subclass() -> None:
    """PreReleaseSchemaMismatchError is a StorageInitializationError subclass."""
    db_path = _create_old_shape_db()
    try:
        storage = SQLiteStorage(db_path=db_path)
        with pytest.raises(StorageInitializationError) as exc_info:
            await storage.initialize()
        assert isinstance(exc_info.value, PreReleaseSchemaMismatchError)
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: fresh DB passes validation without error
# ---------------------------------------------------------------------------


async def test_fresh_db_no_schema_mismatch() -> None:
    """A freshly initialized DB does not trigger schema shape mismatch."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        await storage.close()
    finally:
        os.unlink(db_path)


async def test_conversation_membership_missing_checks_is_rejected(
    tmp_path: Path,
) -> None:
    """Current columns and foreign keys do not compensate for missing checks."""
    db_path = tmp_path / "missing-conversation-checks.db"
    ddl = _SCHEMA.replace(
        "    updated_at TEXT NOT NULL,\n"
        "    CHECK (depth >= 0),\n"
        "    CHECK (conversation_id = root_event_id),\n"
        "    CHECK (resolution_state IN "
        "('root', 'resolved', 'unresolved', 'cycle'))\n",
        "    updated_at TEXT NOT NULL\n",
    )
    assert ddl != _SCHEMA

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(ddl)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    storage = SQLiteStorage(str(db_path))
    try:
        with pytest.raises(
            PreReleaseSchemaConstraintMismatchError,
            match="conversation_membership",
        ):
            await storage.initialize()
    finally:
        await storage.close()


async def test_conversation_projection_state_missing_checks_is_rejected(
    tmp_path: Path,
) -> None:
    """Projection progress tables must retain singleton/state invariants."""
    db_path = tmp_path / "missing-projection-state-checks.db"
    constrained = (
        "    updated_at TEXT NOT NULL,\n"
        "    CHECK (singleton_id = 1),\n"
        "    CHECK (projection_revision >= 1),\n"
        "    CHECK (status IN ('clean', 'dirty', 'rebuilding')),\n"
        "    CHECK (status = 'rebuilding' OR last_event_id IS NULL)\n"
    )
    ddl = _SCHEMA.replace(constrained, "    updated_at TEXT NOT NULL\n", 1)
    assert ddl != _SCHEMA

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(ddl)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    storage = SQLiteStorage(str(db_path))
    try:
        with pytest.raises(
            PreReleaseSchemaConstraintMismatchError,
            match="conversation_projection_state",
        ):
            await storage.initialize()
    finally:
        await storage.close()


async def test_stale_outbox_unique_key_is_rejected(tmp_path: Path) -> None:
    """A pre-release DB must include event_id in the outbox identity key."""
    db_path = tmp_path / "stale-outbox-unique.db"
    current = (
        "UNIQUE(event_id, delivery_plan_id, target_adapter, "
        "target_channel, attempt_number)"
    )
    stale = "UNIQUE(delivery_plan_id, target_adapter, target_channel, attempt_number)"
    ddl = _SCHEMA.replace(current, stale, 1)
    assert ddl != _SCHEMA

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(ddl)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    storage = SQLiteStorage(str(db_path))
    try:
        with pytest.raises(
            PreReleaseSchemaConstraintMismatchError,
            match="delivery_outbox",
        ) as exc_info:
            await storage.initialize()
    finally:
        await storage.close()

    assert exc_info.value.missing_constraints == [
        "UNIQUE(event_id, delivery_plan_id, target_adapter, target_channel, attempt_number)"
    ]


async def test_schema_check_validator_rejects_unconstrained_table_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sqlite_master DDL, not column presence, proves required checks exist."""
    storage = SQLiteStorage("unused.db")

    def _current_table_definition(table: str) -> str:
        prefix = f"CREATE TABLE IF NOT EXISTS {table} ("
        start = _SCHEMA.index(prefix)
        end = _SCHEMA.index("\n);", start) + len("\n);")
        return _SCHEMA[start:end]

    async def _unconstrained_definition(
        sql: str, params: tuple[object, ...] = ()
    ) -> dict[str, str]:
        assert "sqlite_master" in sql
        assert len(params) == 1
        table = str(params[0])
        if table != "conversation_membership":
            return {"sql": _current_table_definition(table)}
        return {"sql": """
                CREATE TABLE conversation_membership (
                    event_id TEXT PRIMARY KEY,
                    depth INTEGER NOT NULL,
                    conversation_id TEXT NOT NULL,
                    root_event_id TEXT NOT NULL,
                    resolution_state TEXT NOT NULL
                )
            """}

    monkeypatch.setattr(storage, "_read_one", _unconstrained_definition)

    with pytest.raises(
        PreReleaseSchemaConstraintMismatchError,
        match="conversation_membership",
    ) as exc_info:
        await storage._validate_schema_checks()

    assert set(exc_info.value.missing_constraints) == {
        "CHECK (depth >= 0)",
        "CHECK (conversation_id = root_event_id)",
        "CHECK (resolution_state IN ('root', 'resolved', 'unresolved', 'cycle'))",
    }


async def test_schema_check_validator_ignores_quoted_constraint_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quoted CHECK-like identifiers do not prove constraint enforcement."""
    storage = SQLiteStorage("unused.db")

    def _current_table_definition(table: str) -> str:
        prefix = f"CREATE TABLE IF NOT EXISTS {table} ("
        start = _SCHEMA.index(prefix)
        end = _SCHEMA.index("\n);", start) + len("\n);")
        return _SCHEMA[start:end]

    fake_labels = (
        '"CHECK (singleton_id = 1)" TEXT',
        '"CHECK (projection_revision >= 1)" TEXT',
        "\"CHECK (status IN ('clean', 'dirty', 'rebuilding'))\" TEXT",
        "\"CHECK (status = 'rebuilding' OR last_event_id IS NULL)\" TEXT",
    )
    spoofed_projection_state = (
        "CREATE TABLE conversation_projection_state (\n    "
        + ",\n    ".join(fake_labels)
        + "\n)"
    )

    async def _definition(sql: str, params: tuple[object, ...] = ()) -> dict[str, str]:
        assert "sqlite_master" in sql
        table = str(params[0])
        if table == "conversation_projection_state":
            return {"sql": spoofed_projection_state}
        return {"sql": _current_table_definition(table)}

    monkeypatch.setattr(storage, "_read_one", _definition)

    with pytest.raises(
        PreReleaseSchemaConstraintMismatchError,
        match="conversation_projection_state",
    ) as exc_info:
        await storage._validate_schema_checks()

    assert len(exc_info.value.missing_constraints) == 4


async def test_corrupt_existing_db_raises_stable_initialization_error(
    tmp_path: Path,
) -> None:
    """Pre-DDL inspection translates corrupt SQLite errors for operators."""
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is not a sqlite database")
    storage = SQLiteStorage(db_path=str(db_path))

    with pytest.raises(
        StorageInitializationError, match="unreadable or corrupt"
    ) as exc_info:
        await storage.initialize()

    assert isinstance(exc_info.value.__cause__, sqlite3.Error)


# ---------------------------------------------------------------------------
# Tests: error message is actionable
# ---------------------------------------------------------------------------


async def test_schema_mismatch_error_is_actionable() -> None:
    """Error message gives recovery or deliberate reset guidance."""
    db_path = _create_old_shape_db()
    try:
        storage = SQLiteStorage(db_path=db_path)
        with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
            await storage.initialize()

        msg = str(exc_info.value).lower()
        # The error should tell the operator what to do.
        assert "explicit recovery or reset decision" in msg
        assert "pre-release" not in msg
        assert "additive" in msg
    finally:
        os.unlink(db_path)


async def test_missing_conversation_projection_table_is_rejected_without_migration(
    tmp_path: Path,
) -> None:
    """A prerelease DB missing current projection shape is rejected, not upgraded."""
    db_path = tmp_path / "missing-conversation-projection.db"
    storage = SQLiteStorage(db_path=str(db_path))
    await storage.initialize()
    await storage.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DROP TABLE conversation_membership")
        raw.commit()
    finally:
        raw.close()

    stale = SQLiteStorage(db_path=str(db_path))
    with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
        await stale.initialize()

    assert exc_info.value.table == "conversation_membership"
    assert "event_id" in exc_info.value.missing_columns


async def test_schema_check_validator_rejects_outbox_without_reservation_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-version prerelease databases must enforce active-attempt invariants."""
    storage = SQLiteStorage("unused.db")

    def _current_table_definition(table: str) -> str:
        prefix = f"CREATE TABLE IF NOT EXISTS {table} ("
        start = _SCHEMA.index(prefix)
        end = _SCHEMA.index("\n);", start) + len("\n);")
        return _SCHEMA[start:end]

    required = "CHECK (active_attempt IS NULL OR status = 'in_progress')"
    stale_outbox = _current_table_definition("delivery_outbox").replace(
        f"    {required},\n",
        "",
        1,
    )
    assert required not in stale_outbox

    async def _definition(sql: str, params: tuple[object, ...] = ()) -> dict[str, str]:
        assert "sqlite_master" in sql
        table = str(params[0])
        if table == "delivery_outbox":
            return {"sql": stale_outbox}
        return {"sql": _current_table_definition(table)}

    monkeypatch.setattr(storage, "_read_one", _definition)

    with pytest.raises(
        PreReleaseSchemaConstraintMismatchError,
        match="delivery_outbox",
    ) as exc_info:
        await storage._validate_schema_checks()

    assert required in exc_info.value.missing_constraints


async def test_same_version_db_missing_replay_outbox_provenance_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-replay-outbox-provenance.db"
    stale_schema = _SCHEMA.replace(
        "    dispatch_source TEXT NOT NULL DEFAULT 'live',\n"
        "    replay_run_id TEXT,\n"
        "    metadata TEXT NOT NULL DEFAULT '{}',\n",
        "    dispatch_source TEXT NOT NULL DEFAULT 'live',\n"
        "    metadata TEXT NOT NULL DEFAULT '{}',\n",
        1,
    ).replace(
        "    CHECK (dispatch_source IN ('live', 'replay', 'retry')),\n"
        "    CHECK (dispatch_source != 'live' OR replay_run_id IS NULL),\n"
        "    CHECK (replay_run_id IS NULL OR (length(replay_run_id) > 0 AND replay_run_id = trim(replay_run_id)))\n",
        "    CHECK (dispatch_source IN ('live', 'replay', 'retry'))\n",
        1,
    )
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(stale_schema)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    storage = SQLiteStorage(str(db_path))
    with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
        await storage.initialize()

    assert _EXPECTED_SCHEMA_VERSION == 1
    assert exc_info.value.table == "delivery_outbox"
    assert "replay_run_id" in exc_info.value.missing_columns


async def test_same_version_db_missing_dispatch_source_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-dispatch-source.db"
    stale_schema = _SCHEMA.replace(
        "    dispatch_source TEXT NOT NULL DEFAULT 'live',\n"
        "    replay_run_id TEXT,\n",
        "    replay_run_id TEXT,\n",
        1,
    ).replace(
        "    CHECK (dispatch_source IN ('live', 'replay', 'retry')),\n"
        "    CHECK (dispatch_source != 'live' OR replay_run_id IS NULL),\n",
        "",
        1,
    )
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(stale_schema)
        raw.execute(
            "INSERT INTO _medre_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        raw.commit()
    finally:
        raw.close()

    storage = SQLiteStorage(str(db_path))
    with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
        await storage.initialize()

    assert _EXPECTED_SCHEMA_VERSION == 1
    assert exc_info.value.table == "delivery_outbox"
    assert "dispatch_source" in exc_info.value.missing_columns


def test_constraint_mismatch_diagnostic_preserves_recovery_choice() -> None:
    error = PreReleaseSchemaConstraintMismatchError(
        "/private/medre.sqlite", "delivery_outbox", ["CHECK(attempt_number >= 1)"]
    )
    message = str(error)
    assert "Storage schema constraint mismatch" in message
    assert "delivery_outbox" in message
    assert "/private/medre.sqlite" in message
    assert "CHECK(attempt_number >= 1)" in message
    assert "explicit recovery or reset decision" in message
    assert "pre-release" not in message.lower()


# ---------------------------------------------------------------------------
# Helpers: catalog snapshots
# ---------------------------------------------------------------------------


def _catalog(db_path: Path) -> dict[tuple[str, str], str]:
    """Return the persisted catalog as ``(kind, name) -> stored sql``."""
    raw = sqlite3.connect(db_path)
    try:
        return {
            (str(row[0]), str(row[1])): str(row[2])
            for row in raw.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            )
        }
    finally:
        raw.close()


def _table_counts(db_path: Path) -> dict[str, int]:
    """Return one row count per catalog table."""
    raw = sqlite3.connect(db_path)
    try:
        names = [
            str(row[0])
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT GLOB 'sqlite_*'"
            )
        ]
        return {
            name: int(
                raw.execute(
                    f"SELECT COUNT(*) FROM {name}"  # nosec B608 # Fixture tables.
                ).fetchone()[0]
            )
            for name in names
        }
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Tests: unsupported catalogs are refused without modification
# ---------------------------------------------------------------------------


async def test_narrowed_check_catalog_is_refused_without_modification(
    tmp_path: Path,
) -> None:
    """A stamped catalog that narrows a declared CHECK is refused, unchanged.

    The narrowed database is not the released catalog and matches no
    allowlisted forward predecessor, so writable initialization raises the
    constraint mismatch, states the recovery or reset decision the operator
    owns, and leaves the database bytes untouched.
    """
    current_status_check = "    CHECK (observed_status IN ('pending', 'retry_wait')),\n"
    narrowed_status_check = "    CHECK (observed_status IN ('pending')),\n"
    assert current_status_check in _SCHEMA
    ddl = _SCHEMA.replace(current_status_check, narrowed_status_check)
    db_path = tmp_path / "narrowed.sqlite"
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(ddl + _INDEXES)
        raw.execute(
            "INSERT INTO _medre_schema_meta VALUES ('schema_version', ?)",
            (str(_EXPECTED_SCHEMA_VERSION),),
        )
        raw.commit()
    finally:
        raw.close()
    original_bytes = db_path.read_bytes()
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        with pytest.raises(PreReleaseSchemaConstraintMismatchError) as exc_info:
            await storage.initialize()
    finally:
        await storage.close()
    message = str(exc_info.value)
    assert "explicit recovery or reset decision" in message
    assert "additive" in message
    assert "pre-release" not in message.lower()
    assert db_path.read_bytes() == original_bytes


async def test_current_catalog_reopen_is_a_no_op(tmp_path: Path) -> None:
    """Reopening a database at the released catalog changes nothing."""
    db_path = tmp_path / "current.sqlite"
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    catalog = _catalog(db_path)
    counts = _table_counts(db_path)
    reopened = SQLiteStorage(db_path=str(db_path))
    try:
        await reopened.initialize()
    finally:
        await reopened.close()
    assert _catalog(db_path) == catalog
    assert _table_counts(db_path) == counts
