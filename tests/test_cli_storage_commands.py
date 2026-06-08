"""Tests for storage CLI commands: status reporting and reset safety.

Verifies the operator-facing storage CLI behavior using the existing
``medre inspect`` and ``medre evidence`` commands with ``--storage-path``,
plus storage-level status checks that determine whether a DB is fresh or
stale.

Covers:

- Fresh DB status: opens successfully, passes shape validation.
- Old DB status: detects schema shape mismatch with actionable error.
- Read-only status: commands never mutate the database.
- Reset safety: no destructive operations available without explicit consent.
- Reset backup: database file backup before destructive operations.
- Directory safety: refuses to operate on directories.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from unittest.mock import patch

import pytest

from medre.cli import main
from medre.cli.exit_codes import EXIT_BUILD
from medre.core.storage.backend import (
    PreReleaseSchemaMismatchError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair. Catches SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_exit(*args: str) -> tuple[int, str, str]:
    """Run CLI expecting a SystemExit, returns (exit_code, stdout, stderr)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


def _seed_fresh_db(tmp_path: Path) -> Path:
    """Create and return path to a freshly initialized test database."""
    import asyncio

    db_path = tmp_path / "fresh.db"

    async def _go() -> None:
        storage = SQLiteStorage(str(db_path))
        try:
            await storage.initialize()
        finally:
            await storage.close()

    asyncio.run(_go())
    return db_path


def _seed_fresh_db_with_event(tmp_path: Path) -> Path:
    """Create a fresh DB with a single event for status reporting tests."""
    import asyncio
    from datetime import UTC, datetime

    from medre.core.events import CanonicalEvent, EventMetadata

    db_path = tmp_path / "status.db"

    async def _go() -> None:
        storage = SQLiteStorage(str(db_path))
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id="evt-status-1",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(UTC),
                source_adapter="test",
                source_transport_id="transport-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "status test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)
        finally:
            await storage.close()

    asyncio.run(_go())
    return db_path


def _create_old_shape_db(tmp_path: Path) -> Path:
    """Create a DB with old column shape (event_relations missing target_native_thread_id)."""
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(str(db_path))
    try:
        raw.executescript("""
            CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY,
                event_kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                source_adapter TEXT NOT NULL,
                source_transport_id TEXT NOT NULL,
                source_channel_id TEXT,
                parent_event_id TEXT,
                lineage TEXT NOT NULL DEFAULT '[]',
                payload TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                depth INTEGER NOT NULL DEFAULT 0,
                trace_id TEXT,
                root_event_id TEXT,
                conversation_id TEXT,
                source_native_adapter TEXT,
                source_native_channel_id TEXT,
                source_native_message_id TEXT,
                source_native_thread_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE event_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                target_event_id TEXT,
                target_native_adapter TEXT,
                target_native_channel_id TEXT,
                target_native_message_id TEXT,
                key TEXT,
                fallback_text TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE native_message_refs (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                adapter TEXT NOT NULL,
                native_channel_id TEXT,
                native_message_id TEXT NOT NULL,
                native_thread_id TEXT,
                native_relation_id TEXT,
                direction TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(adapter, native_channel_id, native_message_id)
            );
            CREATE TABLE delivery_receipts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT UNIQUE NOT NULL,
                event_id TEXT NOT NULL,
                delivery_plan_id TEXT NOT NULL,
                target_adapter TEXT NOT NULL,
                target_channel TEXT,
                route_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error TEXT,
                failure_kind TEXT,
                adapter_message_id TEXT,
                next_retry_at TEXT,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                parent_receipt_id TEXT,
                source TEXT NOT NULL DEFAULT 'live',
                replay_run_id TEXT,
                retry_max_attempts INTEGER,
                retry_backoff_base REAL,
                retry_max_delay REAL,
                retry_jitter INTEGER,
                rendering_evidence TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE delivery_outbox (
                outbox_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                route_id TEXT NOT NULL DEFAULT '',
                delivery_plan_id TEXT NOT NULL,
                target_adapter TEXT NOT NULL,
                target_channel TEXT,
                target_address TEXT,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                failure_kind TEXT,
                failure_kind_detail TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_attempt_at TEXT,
                locked_at TEXT,
                lease_until TEXT,
                worker_id TEXT,
                payload_hash TEXT,
                receipt_id TEXT,
                parent_receipt_id TEXT,
                error_summary TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(delivery_plan_id, target_adapter, target_channel, attempt_number)
            );
            CREATE TABLE plugin_state (
                plugin_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(plugin_id, key)
            );
            CREATE TABLE _medre_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO _medre_schema_meta (key, value)
                VALUES ('schema_version', '1');
        """)
    finally:
        raw.close()
    return db_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    """Create and return path to a freshly seeded test database."""
    return _seed_fresh_db_with_event(tmp_path)


@pytest.fixture
def old_db(tmp_path: Path) -> Path:
    """Create and return path to an old-shape test database."""
    return _create_old_shape_db(tmp_path)


# ---------------------------------------------------------------------------
# Tests: storage status reports OK for fresh DB
# ---------------------------------------------------------------------------


def test_storage_status_reports_ok_for_fresh_db(fresh_db: Path) -> None:
    """evidence --storage-path on a fresh DB reports storage status as passed."""
    stdout, stderr = _run_cli(
        "evidence",
        "--storage-path",
        str(fresh_db),
        "--json",
    )
    bundle = json.loads(stdout)
    assert bundle["sections"]["storage"]["status"] == "passed"
    assert bundle["sections"]["storage"]["data"]["event_count"] == 1
    assert stderr == ""


# ---------------------------------------------------------------------------
# Tests: storage status reports stale for old DB
# ---------------------------------------------------------------------------


def test_storage_status_reports_stale_for_old_db(old_db: Path) -> None:
    """evidence --storage-path on an old-shape DB reports storage as partial/error."""
    stdout, stderr = _run_cli(
        "evidence",
        "--storage-path",
        str(old_db),
        "--json",
    )
    bundle = json.loads(stdout)
    storage_section = bundle["sections"]["storage"]
    assert storage_section["status"] in ("partial", "error")
    # The error message should indicate a schema shape mismatch.
    assert "schema shape mismatch" in storage_section.get("error", "").lower()


# ---------------------------------------------------------------------------
# Tests: storage status is read-only
# ---------------------------------------------------------------------------


def test_storage_status_is_read_only(fresh_db: Path) -> None:
    """Running evidence --storage-path does not mutate the database file."""
    # Record row counts across all tables before the CLI call.
    conn_before = sqlite3.connect(str(fresh_db))
    tables = [
        row[0]
        for row in conn_before.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    counts_before = {}
    for t in tables:
        counts_before[t] = conn_before.execute(
            f"SELECT COUNT(*) FROM [{t}]"  # nosec B608 - table names from sqlite_master
        ).fetchone()[0]
    conn_before.close()

    _run_cli(
        "evidence",
        "--storage-path",
        str(fresh_db),
        "--json",
    )

    # Verify no rows were added, removed, or modified.
    conn_after = sqlite3.connect(str(fresh_db))
    for t in tables:
        count = conn_after.execute(
            f"SELECT COUNT(*) FROM [{t}]"  # nosec B608 - table names from sqlite_master
        ).fetchone()[0]
        assert count == counts_before[t]
    conn_after.close()


# ---------------------------------------------------------------------------
# Tests: storage reset refuses without confirmation
# ---------------------------------------------------------------------------


def test_storage_reset_refuses_without_yes(tmp_path: Path) -> None:
    """Destructive reset of a database requires explicit operator confirmation.

    Tests that the storage layer does NOT silently delete or overwrite an
    existing database when initialize() is called on an old-shape DB. The
    operator must explicitly delete or backup the file.
    """
    old_db_path = _create_old_shape_db(tmp_path)
    assert old_db_path.exists()

    original_size = old_db_path.stat().st_size

    # Attempting to initialize on the old DB should raise, not silently
    # reset/overwrite.
    import asyncio

    async def _attempt_init() -> None:
        storage = SQLiteStorage(str(old_db_path))
        try:
            await storage.initialize()
        finally:
            await storage.close()

    with pytest.raises(PreReleaseSchemaMismatchError):
        asyncio.run(_attempt_init())

    # The file must still exist and be unchanged — no silent reset.
    assert old_db_path.exists()
    assert old_db_path.stat().st_size == original_size


# ---------------------------------------------------------------------------
# Tests: storage reset with backup
# ---------------------------------------------------------------------------


def test_storage_reset_with_backup(tmp_path: Path) -> None:
    """Operator can backup the old DB file and reinitialize a fresh one."""
    old_db_path = _create_old_shape_db(tmp_path)
    backup_path = tmp_path / "backup.db"

    # Step 1: Operator creates a backup before reset.
    shutil.copy2(str(old_db_path), str(backup_path))
    assert backup_path.exists()
    assert backup_path.stat().st_size == old_db_path.stat().st_size

    # Step 2: Operator deletes the old DB.
    os.unlink(str(old_db_path))
    assert not old_db_path.exists()

    # Step 3: Fresh initialize succeeds.
    import asyncio

    async def _fresh_init() -> None:
        storage = SQLiteStorage(str(old_db_path))
        try:
            await storage.initialize()
        finally:
            await storage.close()

    asyncio.run(_fresh_init())
    assert old_db_path.exists()

    # Backup is still intact.
    assert backup_path.exists()


# ---------------------------------------------------------------------------
# Tests: storage reset refuses directories
# ---------------------------------------------------------------------------


def test_storage_reset_refuses_directories(tmp_path: Path) -> None:
    """Storage operations refuse to use a directory path as a database."""
    dir_path = tmp_path / "not_a_db"
    dir_path.mkdir()

    import asyncio

    async def _attempt_init() -> None:
        storage = SQLiteStorage(str(dir_path))
        try:
            await storage.initialize()
        finally:
            with suppress(Exception):
                await storage.close()

    # Initializing with a directory path should raise an error.
    with pytest.raises(
        (PreReleaseSchemaMismatchError, sqlite3.OperationalError, OSError)
    ):
        asyncio.run(_attempt_init())

    # Directory must still exist and be unchanged.
    assert dir_path.is_dir()


# ---------------------------------------------------------------------------
# Tests: inspect event reports actionable error for old DB
# ---------------------------------------------------------------------------


def test_inspect_event_old_db_reports_actionable_error(old_db: Path) -> None:
    """inspect event on an old-shape DB exits with actionable error."""
    code, _, stderr = _run_cli_exit(
        "inspect",
        "event",
        "--storage-path",
        str(old_db),
        "any-event-id",
    )
    assert code == EXIT_BUILD
    assert "storage error" in stderr.lower()
    assert "schema shape mismatch" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: inspect receipts reports actionable error for old DB
# ---------------------------------------------------------------------------


def test_inspect_receipts_old_db_reports_actionable_error(old_db: Path) -> None:
    """inspect receipts on an old-shape DB exits with actionable error."""
    code, _, stderr = _run_cli_exit(
        "inspect",
        "receipts",
        "--storage-path",
        str(old_db),
        "--event",
        "any-event-id",
    )
    assert code == EXIT_BUILD
    assert "storage error" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: missing DB does not create file
# ---------------------------------------------------------------------------


def test_evidence_missing_db_does_not_create_file(tmp_path: Path) -> None:
    """evidence --storage-path with missing file does not create it."""
    missing = tmp_path / "nonexistent.db"
    _run_cli(
        "evidence",
        "--storage-path",
        str(missing),
        "--json",
    )
    assert not missing.exists()


# ---------------------------------------------------------------------------
# Tests: CLI storage status subcommand
# ---------------------------------------------------------------------------


def test_cli_storage_status_fresh_db(fresh_db: Path) -> None:
    """`medre storage status --storage-path` reports HEALTHY for a fresh DB."""
    stdout, stderr = _run_cli(
        "storage",
        "status",
        "--storage-path",
        str(fresh_db),
    )
    assert "HEALTHY" in stdout
    assert "Schema version: 1" in stdout
    assert str(fresh_db) in stdout


def test_cli_storage_status_old_db(old_db: Path) -> None:
    """`medre storage status --storage-path` reports MISMATCH for an old-shape DB."""
    stdout, stderr = _run_cli(
        "storage",
        "status",
        "--storage-path",
        str(old_db),
    )
    assert "MISMATCH" in stdout
    assert "event_relations" in stdout


def test_cli_storage_status_missing_db(tmp_path: Path) -> None:
    """`medre storage status` with missing file exits with error."""
    code, stdout, stderr = _run_cli_exit(
        "storage",
        "status",
        "--storage-path",
        str(tmp_path / "nope.db"),
    )
    assert code == EXIT_BUILD
    assert "not found" in stderr.lower() or "error" in stderr.lower()


def test_cli_storage_status_connect_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`medre storage status` reports sqlite3 connection errors cleanly."""
    db_path = tmp_path / "exists.db"
    db_path.write_bytes(b"not important")

    def _raise_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _raise_connect)

    code, stdout, stderr = _run_cli_exit(
        "storage",
        "status",
        "--storage-path",
        str(db_path),
    )
    assert code == EXIT_BUILD
    assert stdout == ""
    assert "Storage error:" in stderr


def test_cli_storage_status_missing_schema_meta(tmp_path: Path) -> None:
    """Status reports schema version None when schema metadata is absent."""
    db_path = tmp_path / "no-meta.db"
    conn = sqlite3.connect(db_path)
    conn.close()

    stdout, stderr = _run_cli(
        "storage",
        "status",
        "--storage-path",
        str(db_path),
    )
    assert "Schema version: None" in stdout
    assert "Status: MISMATCH" in stdout
    assert stderr == ""


def test_cli_storage_status_table_info_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status treats PRAGMA table_info failures as missing columns."""
    db_path = tmp_path / "pragma-error.db"
    db_path.write_bytes(b"placeholder")

    class _SelectResult:
        def fetchone(self) -> dict[str, str]:
            return {"value": "1"}

    class _Conn:
        row_factory = None

        def execute(self, sql: str) -> _SelectResult:
            if sql.startswith("SELECT value FROM _medre_schema_meta"):
                return _SelectResult()
            raise sqlite3.OperationalError("malformed schema")

        def close(self) -> None:
            pass

    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: _Conn())

    stdout, stderr = _run_cli(
        "storage",
        "status",
        "--storage-path",
        str(db_path),
    )
    assert "Schema version: 1" in stdout
    assert "MISSING" in stdout
    assert "Status: MISMATCH" in stdout
    assert stderr == ""


# ---------------------------------------------------------------------------
# Tests: CLI storage reset subcommand
# ---------------------------------------------------------------------------


def test_cli_storage_reset_refuses_without_yes(tmp_path: Path) -> None:
    """`medre storage reset` without --yes exits with config error."""
    db_path = _create_old_shape_db(tmp_path)
    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
    )
    assert code == 2  # EXIT_CONFIG
    assert "--yes" in stderr
    assert db_path.exists()  # file not deleted


def test_cli_storage_reset_missing_db_with_yes(tmp_path: Path) -> None:
    """`medre storage reset --yes` reports missing database paths."""
    missing = tmp_path / "missing.db"
    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(missing),
        "--yes",
    )
    assert code == EXIT_BUILD
    assert stdout == ""
    assert "file not found" in stderr.lower()


def test_cli_storage_reset_cannot_read_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`medre storage reset` reports OSError while reading the header."""
    db_path = _create_old_shape_db(tmp_path)

    def _raise_open(*_args: object, **_kwargs: object) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr("builtins.open", _raise_open)

    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
        "--yes",
    )
    assert code == EXIT_BUILD
    assert stdout == ""
    assert "cannot read" in stderr.lower()
    assert db_path.exists()


def test_cli_storage_reset_with_backup(tmp_path: Path) -> None:
    """`medre storage reset --backup --yes` creates backup and deletes DB."""
    db_path = _create_old_shape_db(tmp_path)
    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
        "--backup",
        "--yes",
    )
    assert code == 0
    assert "Backup:" in stdout
    assert "Deleted:" in stdout
    assert not db_path.exists()
    # Check backup was created
    backups = list(tmp_path.glob("old.bak-*.db"))
    assert len(backups) == 1


def test_cli_storage_reset_backup_includes_wal_shm(tmp_path: Path) -> None:
    """`medre storage reset --backup --yes` copies WAL/SHM sidecars."""
    db_path = _create_old_shape_db(tmp_path)
    # Create fake WAL and SHM sidecar files.
    wal_path = tmp_path / "old.db-wal"
    shm_path = tmp_path / "old.db-shm"
    wal_path.write_bytes(b"fake-wal-content")
    shm_path.write_bytes(b"fake-shm-content")

    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
        "--backup",
        "--yes",
    )
    assert code == 0
    # Check all three backup files were created.
    backups = list(tmp_path.glob("old.bak-*.db"))
    assert len(backups) == 1
    backup_name = backups[0].name
    assert (tmp_path / f"{backup_name}-wal").exists()
    assert (tmp_path / f"{backup_name}-shm").exists()
    # Original sidecars should be deleted.
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_cli_storage_reset_permission_error_on_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`medre storage reset` reports permission errors during deletion."""
    db_path = _create_old_shape_db(tmp_path)

    def _raise_remove(_path: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("medre.cli.storage_commands.os.remove", _raise_remove)

    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
        "--yes",
    )
    assert code == EXIT_BUILD
    assert "permission denied" in stderr.lower()
    assert db_path.exists()


def test_cli_storage_reset_oserror_on_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`medre storage reset` reports generic OSError during deletion."""
    db_path = _create_old_shape_db(tmp_path)

    def _raise_remove(_path: Path) -> None:
        raise OSError("disk is busy")

    monkeypatch.setattr("medre.cli.storage_commands.os.remove", _raise_remove)

    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(db_path),
        "--yes",
    )
    assert code == EXIT_BUILD
    assert "disk is busy" in stderr
    assert db_path.exists()


def test_cli_storage_reset_refuses_non_sqlite(tmp_path: Path) -> None:
    """`medre storage reset` refuses to delete files that are not SQLite databases."""
    fake_file = tmp_path / "not-a-db.txt"
    fake_file.write_text("hello world")
    code, stdout, stderr = _run_cli_exit(
        "storage",
        "reset",
        "--storage-path",
        str(fake_file),
        "--yes",
    )
    assert code == EXIT_BUILD
    assert "magic bytes" in stderr.lower() or "not appear to contain" in stderr.lower()
    assert fake_file.exists()  # file must NOT be deleted


def test_main_adapter_command_dispatches() -> None:
    """`medre adapter ...` dispatches through contributed command routing."""
    with patch("medre.cli.contrib.dispatch_contribution") as dispatch:
        _run_cli("adapter", "matrix", "auth", "status")

    dispatch.assert_called_once()
    args = dispatch.call_args.args[0]
    assert args.command == "adapter"
    assert args.adapter_command == "matrix"
    assert args.adapter_matrix_command == "auth"
    assert args.adapter_matrix_auth_command == "status"
