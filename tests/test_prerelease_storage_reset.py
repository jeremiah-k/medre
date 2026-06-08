"""Tests for pre-release storage reset UX: schema mismatch error diagnostics.

Verifies that old-shape pre-release databases produce actionable errors that
include the database path, the affected table, and the missing columns —
giving operators clear guidance to recreate the database.

Covers:

- ``PreReleaseSchemaMismatchError`` attributes (path, table, missing_columns).
- Error message includes the database file path.
- Error message names the affected table and missing columns.
- ``_EXPECTED_SCHEMA_VERSION`` remains at 1 during prerelease.
- ``open_readonly`` raises the same structured error for old-shape DBs.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from medre.core.storage.backend import (
    PreReleaseSchemaMismatchError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.schema import _EXPECTED_SCHEMA_VERSION
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal old-shape DDL that triggers schema shape mismatch.
# Uses the current DDL for all tables EXCEPT event_relations, which is missing
# target_native_thread_id (a column added in a later pre-release build).
# schema_version is stamped as 1 to simulate an old prerelease DB.
_OLD_SHAPE_DDL: str = """
CREATE TABLE IF NOT EXISTS canonical_events (
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
CREATE TABLE IF NOT EXISTS event_relations (
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
CREATE TABLE IF NOT EXISTS native_message_refs (
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
CREATE TABLE IF NOT EXISTS delivery_receipts (
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
CREATE TABLE IF NOT EXISTS delivery_outbox (
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
CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plugin_id, key)
);
CREATE TABLE IF NOT EXISTS _medre_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO _medre_schema_meta (key, value)
    VALUES ('schema_version', '1');
"""


def _create_old_shape_db() -> str:
    """Create a temporary old-shape database and return its path.

    The caller is responsible for deleting the file.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(_OLD_SHAPE_DDL)
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


# ---------------------------------------------------------------------------
# Tests: error message is actionable
# ---------------------------------------------------------------------------


async def test_schema_mismatch_error_is_actionable() -> None:
    """Error message contains guidance to recreate the database."""
    db_path = _create_old_shape_db()
    try:
        storage = SQLiteStorage(db_path=db_path)
        with pytest.raises(PreReleaseSchemaMismatchError) as exc_info:
            await storage.initialize()

        msg = str(exc_info.value).lower()
        # The error should tell the operator what to do.
        assert "recreate" in msg
        assert "no automatic migration" in msg or "no auto" in msg
    finally:
        os.unlink(db_path)
