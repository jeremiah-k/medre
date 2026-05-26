"""Regression tests: every sqlite3.Connection opened by SQLiteStorage is
provably closed on both success and failure paths — no ResourceWarning leaks.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageInitializationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _temp_db_path(tmp_path: Path) -> str:
    """Return a unique temporary database path under *tmp_path*."""
    return str(tmp_path / "test_resource_leak.db")


# ---------------------------------------------------------------------------
# Tests: normal (success) path — connections must close cleanly
# ---------------------------------------------------------------------------


class TestSyncFallbackNormalClose:
    """Sync fallback (no aiosqlite) — normal init/close must not leak."""

    async def test_initialize_and_close_no_resource_warning(
        self, tmp_path: Path
    ) -> None:
        db_path = _temp_db_path(tmp_path)
        storage = SQLiteStorage(db_path=db_path)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            await storage.initialize()
            await storage.close()

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised during normal init/close: {resource_warnings}"

    async def test_open_readonly_and_close_no_resource_warning(
        self, tmp_path: Path
    ) -> None:
        db_path = _temp_db_path(tmp_path)

        # First create a valid database.
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        await storage.close()

        # Now open it read-only and verify no leak.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            ro = await SQLiteStorage.open_readonly(db_path)
            await ro.close()

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised during read-only open/close: {resource_warnings}"


# ---------------------------------------------------------------------------
# Tests: failure path — connections must still close cleanly
# ---------------------------------------------------------------------------


class TestSyncFallbackFailureClose:
    """Sync fallback — failure during open must not leak connections."""

    async def test_sync_open_failure_closes_connection(self, tmp_path: Path) -> None:
        """If executescript fails inside _sync_open, the raw connection is closed.

        We patch the _SCHEMA constant to invalid SQL so that db.executescript()
        inside _sync_open raises. The fix's try/except must close the connection
        before re-raising.
        """
        db_path = _temp_db_path(tmp_path)
        storage = SQLiteStorage(db_path=db_path)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)

            with patch("medre.core.storage.sqlite._SCHEMA", "INVALID SQL !!@@##"):
                with pytest.raises(sqlite3.OperationalError):
                    await storage.initialize()

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised on _sync_open failure path: {resource_warnings}"

    async def test_initialize_schema_version_mismatch_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """If schema validation fails, close() is called and connection is freed."""
        db_path = _temp_db_path(tmp_path)

        # Create and stamp with wrong version.
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        # Corrupt the schema version to trigger verification failure.
        storage._require_db()
        await storage._write(
            "UPDATE _medre_schema_meta SET value = '9999' WHERE key = 'schema_version'"
        )
        await storage.close()

        # Re-open: verify_schema_version should raise StorageInitializationError.
        storage2 = SQLiteStorage(db_path=db_path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with pytest.raises(StorageInitializationError):
                await storage2.initialize()

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised on schema mismatch path: {resource_warnings}"

    async def test_sync_open_readonly_failure_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """If _sync_open_readonly encounters an error, the connection is closed."""
        db_path = _temp_db_path(tmp_path)

        # Attempt to open a non-existent file read-only (will fail at
        # the sqlite3.connect level with URI mode=ro).
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with pytest.raises(StorageInitializationError, match="does not exist"):
                await SQLiteStorage.open_readonly(db_path)

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised on read-only failure path: {resource_warnings}"
