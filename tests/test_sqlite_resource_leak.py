"""Regression tests: every sqlite3.Connection opened by SQLiteStorage is
provably closed on both success and failure paths — no ResourceWarning leaks.
"""

from __future__ import annotations

import gc
import sqlite3
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from medre.core.storage.backend import StorageInitializationError
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Mock connection helpers — used when the defensive try/except blocks
# (e.g. ``row_factory`` assignment failures) cannot be triggered against
# real ``sqlite3.Connection`` objects.
# ---------------------------------------------------------------------------


class _FailingRowFactoryConnection:
    """Mock ``sqlite3.Connection`` whose ``row_factory`` setter raises.

    Used to verify that :meth:`SQLiteStorage._sync_open_readonly` and the
    equivalent aiosqlite path call ``.close()`` even when the assignment
    ``db.row_factory = sqlite3.Row`` fails.
    """

    def __init__(self) -> None:
        self._closed: bool = False

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def __setattr__(self, name: str, value: object) -> None:
        if name == "row_factory":
            raise RuntimeError("simulated row_factory assignment failure")
        super().__setattr__(name, value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _temp_db_path(tmp_path: Path) -> str:
    """Return a unique temporary database path under *tmp_path*."""
    return str(tmp_path / "test_resource_leak.db")


# ---------------------------------------------------------------------------
# Force sync-fallback path so these tests always exercise the sync code
# regardless of whether aiosqlite is installed in the environment.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_sync_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("medre.core.storage.sqlite.storage._HAS_AIOSQLITE", False)


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
            del storage
            gc.collect()

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
        del storage
        gc.collect()

        # Now open it read-only and verify no leak.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            ro = await SQLiteStorage.open_readonly(db_path)
            await ro.close()
            del ro
            gc.collect()

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

            with patch(
                "medre.core.storage.sqlite.connection._SCHEMA", "INVALID SQL !!@@##"
            ), pytest.raises(sqlite3.OperationalError):
                await storage.initialize()

            del storage
            gc.collect()

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
            del storage2
            gc.collect()

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
            gc.collect()

        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        assert (
            resource_warnings == []
        ), f"ResourceWarning(s) raised on read-only failure path: {resource_warnings}"


# ---------------------------------------------------------------------------
# Tests: _sync_open_readonly row_factory assignment failure (lines 933-935)
# ---------------------------------------------------------------------------


class TestSyncOpenReadonlyRowFactoryFailure:
    """_sync_open_readonly must close the connection if row_factory fails."""

    async def test_sync_open_readonly_row_factory_failure_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """When db.row_factory = sqlite3.Row raises inside _sync_open_readonly,
        the raw connection is closed before the exception propagates."""
        db_path = _temp_db_path(tmp_path)

        # Create a valid database file so open_readonly() passes the
        # file-existence guard at line 884.
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        await storage.close()
        del storage
        gc.collect()

        mock_conn = _FailingRowFactoryConnection()

        with patch(
            "medre.core.storage.sqlite.connection.sqlite3.connect",
            return_value=mock_conn,
        ), pytest.raises(
            RuntimeError, match="simulated row_factory assignment failure"
        ):
            await SQLiteStorage.open_readonly(db_path)

        assert mock_conn.closed, (
            "_sync_open_readonly() must close the connection when "
            "row_factory assignment fails"
        )

        gc.collect()


# ---------------------------------------------------------------------------
# Tests: close() executor cleanup on sync-fallback path
# ---------------------------------------------------------------------------


class TestSyncFallbackExecutorCleanup:
    """Sync fallback — executor is always cleaned up by close()."""

    async def test_close_clears_executor_sync_path(self, tmp_path: Path) -> None:
        """close() sets _executor to None on the sync fallback path."""
        db_path = _temp_db_path(tmp_path)
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        assert storage._executor is not None  # sync path creates executor
        await storage.close()
        assert storage._executor is None
        assert storage._closed is True

    async def test_executor_cleared_even_if_db_close_raises_sync(
        self, tmp_path: Path
    ) -> None:
        """If an exception occurs during DB close, executor is still shut down.

        We simulate a failure in the close path by replacing _db with a mock
        whose close() raises after the real connection is cleaned up.
        """
        db_path = _temp_db_path(tmp_path)
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        assert storage._executor is not None

        # Close the real connection ourselves, then install a mock that raises.
        real_db = storage._db
        real_db.close()

        class _MockConn:
            def close(self):
                raise RuntimeError("simulated db close error")

        storage._db = _MockConn()

        with pytest.raises(RuntimeError, match="simulated db close error"):
            await storage.close()

        # Executor must still be cleared.
        assert storage._executor is None
        assert storage._closed is True

    async def test_close_with_none_db_clears_executor_sync(
        self, tmp_path: Path
    ) -> None:
        """close() cleans up executor even when _db is already None."""
        db_path = _temp_db_path(tmp_path)
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        # Manually nil the db to simulate partial cleanup.
        storage._db = None
        assert storage._executor is not None
        await storage.close()
        assert storage._executor is None
        assert storage._closed is True
