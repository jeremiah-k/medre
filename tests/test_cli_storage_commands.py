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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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
    from datetime import datetime, timezone

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
                timestamp=datetime.now(timezone.utc),
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


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    """Create and return path to a freshly seeded test database."""
    return _seed_fresh_db_with_event(tmp_path)


@pytest.fixture()
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
    original_size = fresh_db.stat().st_size
    original_mtime = fresh_db.stat().st_mtime

    _run_cli(
        "evidence",
        "--storage-path",
        str(fresh_db),
        "--json",
    )

    assert fresh_db.stat().st_size == original_size
    assert fresh_db.stat().st_mtime == original_mtime


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
            try:
                await storage.close()
            except Exception:
                pass

    # Initializing with a directory path should raise an error.
    with pytest.raises(Exception):  # noqa: B017
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


def test_storage_status_missing_db_does_not_create_file(tmp_path: Path) -> None:
    """evidence --storage-path with missing file does not create it."""
    missing = tmp_path / "nonexistent.db"
    _run_cli(
        "evidence",
        "--storage-path",
        str(missing),
        "--json",
    )
    assert not missing.exists()
