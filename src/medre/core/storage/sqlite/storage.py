"""SQLite-backed storage backend for the medre.

Uses *aiosqlite* when available for native async database access; otherwise
falls back to synchronous ``sqlite3`` dispatched through a private
``ThreadPoolExecutor``.  The database runs in WAL mode for safe concurrent
reads.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from medre.core.storage.backend import (
    DuplicateEventError,
    StorageError,
    StorageInitializationError,
)

# Mixin imports — method groups composed via multiple inheritance.
from medre.core.storage.sqlite._count import _CountMixin
from medre.core.storage.sqlite._event import _EventMixin
from medre.core.storage.sqlite._native_ref import _NativeRefMixin
from medre.core.storage.sqlite._outbox import _OutboxMixin
from medre.core.storage.sqlite._receipt import _ReceiptMixin
from medre.core.storage.sqlite._relation import _RelationMixin
from medre.core.storage.sqlite.connection import (
    sync_create_indexes,
    sync_open,
    sync_open_readonly,
    sync_read_all,
    sync_read_one,
    sync_write,
    sync_write_batch,
)
from medre.core.storage.sqlite.schema import (
    _EXPECTED_SCHEMA_VERSION,
    _INDEXES,
    _REQUIRED_COLUMNS,
    _SCHEMA,
)

try:
    import aiosqlite  # type: ignore[import-untyped]

    _HAS_AIOSQLITE: bool = True
except ImportError:
    aiosqlite = None  # type: ignore[assignment]
    _HAS_AIOSQLITE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _SQLiteStorageBase — lifecycle, connection management, and read/write
# primitives.  Domain methods live in the mixin classes above.
# ---------------------------------------------------------------------------


class _SQLiteStorageBase:
    """Lifecycle, connection management, and read/write primitives.

    This base class is *not* intended to be instantiated directly.  Use
    :class:`SQLiteStorage` which composes this base with all domain mixins.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # The underlying connection is either an aiosqlite.Connection or a
        # plain sqlite3.Connection, depending on whether aiosqlite is
        # available.  We keep the type as ``Any`` because the two
        # connection types are not related and every public method
        # dispatches on ``_use_aiosqlite`` before calling async/sync APIs.
        self._db: Any = None
        self._lock = threading.Lock()
        self._async_write_lock = asyncio.Lock()
        self._use_aiosqlite = _HAS_AIOSQLITE
        self._executor: ThreadPoolExecutor | None = None
        self._closed: bool = False

    # -- Internal helpers ---------------------------------------------------

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a synchronous function in the private executor."""
        if self._closed:
            raise RuntimeError("SQLiteStorage is closed.")
        executor = self._executor
        if executor is None:
            if self._db is None and self._use_aiosqlite:
                # aiosqlite path never needs the executor; raise if called
                # before initialize() or after close().
                raise RuntimeError(
                    "SQLiteStorage private executor is closed. "
                    "Cannot dispatch work after close()."
                )
            executor = ThreadPoolExecutor(max_workers=1)
            self._executor = executor
        loop = asyncio.get_running_loop()
        if kwargs:
            func = functools.partial(func, **kwargs)
        return await loop.run_in_executor(executor, func, *args)

    def _require_db(self) -> Any:
        """Return the active connection or raise if not initialised."""
        if self._db is None:
            raise StorageInitializationError(
                "Storage backend has not been initialised. " "Call initialize() first."
            )
        return self._db

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Open the database, enable WAL mode, create schema, and verify version.

        Raises
        ------
        StorageInitializationError
            If the database schema version does not match the expected
            version.  The operator must resolve the mismatch manually
            (no silent migration or reset).
        """
        self._closed = False
        if self._use_aiosqlite:
            db = await aiosqlite.connect(self._db_path)  # type: ignore[union-attr]
            try:
                db.row_factory = sqlite3.Row
                await db.executescript(_SCHEMA)
                await db.execute("PRAGMA journal_mode=WAL")
                await db.commit()
            except BaseException:
                await db.close()
                raise
            self._db = db
        else:
            self._db = await self._run_in_thread(sync_open, self._db_path)

        try:
            # Verify schema version after DDL.
            await self._verify_schema_version()

            # Verify column shape — catches old pre-release DBs that claim
            # schema_version=1 but predate current columns.
            await self._validate_schema_shape()

            # Create targeted indexes AFTER shape validation so that old-shape
            # databases fail with a clear StorageInitializationError before
            # index creation references missing columns.
            await self._create_indexes()
        except BaseException:
            try:
                await self.close()
            except BaseException:
                logger.debug(
                    "error while closing SQLite storage after initialization failure",
                    exc_info=True,
                )
            raise

    async def _verify_schema_version(self) -> None:
        """Check that the stored schema version matches the expected version.

        On a fresh database the version row does not exist, so we insert it.
        If it exists but mismatches, raise immediately.
        """
        row = await self._read_one(
            "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
        )
        if row is None:
            # Fresh database — stamp the current version.
            await self._write(
                "INSERT INTO _medre_schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(_EXPECTED_SCHEMA_VERSION),),
            )
            return

        stored_version = row["value"]
        try:
            stored_int = int(stored_version)
        except (ValueError, TypeError):
            raise StorageInitializationError(
                f"Storage schema version is not an integer: {stored_version!r}. "
                f"Expected {_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration or "
                f"silent reset is performed.  Options: export data, delete "
                f"the database file, and restart; or downgrade medre to "
                f"match the database version."
            )

    async def _validate_schema_shape(self) -> None:
        """Verify that every required table has all expected columns.

        This catches old pre-release databases whose ``schema_version`` still
        reads ``1`` but whose column shape predates the current DDL.  The
        check is intentionally lightweight — it inspects ``PRAGMA
        table_info`` for each required table and compares column names
        against :data:`_REQUIRED_COLUMNS`.

        Raises
        ------
        StorageInitializationError
            If any required table or column is missing.  No automatic
            migration is performed; the operator must recreate the DB.
        """
        for table, required in _REQUIRED_COLUMNS.items():
            rows = await self._read_all(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in rows}
            missing = required - existing
            if missing:
                raise StorageInitializationError(
                    f"Pre-release schema shape mismatch: table '{table}' is "
                    f"missing required columns {sorted(missing)}. "
                    f"The database was likely created by an older pre-release "
                    f"build.  Please recreate the database — no automatic "
                    f"migration is provided."
                )

    async def _create_indexes(self) -> None:
        """Create targeted indexes for current query patterns.

        Called after :meth:`_validate_schema_shape` so that old-shape
        databases raise :class:`StorageInitializationError` *before*
        any index DDL references columns that may not exist.
        """
        if self._use_aiosqlite:
            await self._db.executescript(_INDEXES)  # type: ignore[union-attr]
            await self._db.commit()  # type: ignore[union-attr]
        else:
            await self._run_in_thread(sync_create_indexes, self._require_db())

    @classmethod
    async def open_readonly(cls, db_path: str) -> SQLiteStorage:
        """Open an existing database in strict read-only mode.

        Does **not** create the database file, tables, indexes, or metadata
        rows.  Suitable for ``medre inspect`` commands that must never
        mutate storage.

        Raises
        ------
        StorageInitializationError
            If the database file does not exist, has no schema version
            metadata (uninitialised), or has an incompatible schema shape.
        """
        if db_path != ":memory:" and not os.path.exists(db_path):
            raise StorageInitializationError(
                f"Database file does not exist: {db_path}. "
                f"Cannot open in read-only mode — no file was created."
            )

        instance = cls(db_path)

        if instance._use_aiosqlite:
            db = await aiosqlite.connect(  # type: ignore[union-attr]
                f"file:{db_path}?mode=ro",
                uri=True,
            )
            try:
                db.row_factory = (
                    sqlite3.Row
                )  # redundant guard (connect won't fail), mirrors initialize() pattern
            except BaseException:
                await db.close()
                raise
            instance._db = db
        else:
            instance._db = await instance._run_in_thread(
                sync_open_readonly, instance._db_path
            )

        try:
            # Validate metadata and shape without writing anything.
            await instance._verify_schema_version_readonly()
            await instance._validate_schema_shape()
        except BaseException:
            try:
                await instance.close()
            except BaseException:
                logger.debug(
                    "error while closing read-only SQLite connection after initialization failure",
                    exc_info=True,
                )
            raise

        return instance

    async def _verify_schema_version_readonly(self) -> None:
        """Check schema version without writing.

        Unlike :meth:`_verify_schema_version`, this raises immediately when
        the version row is absent (uninitialised database) rather than
        inserting it.
        """
        try:
            row = await self._read_one(
                "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
            )
        except StorageError as exc:
            # Table doesn't exist — database not initialised.
            raise StorageInitializationError(
                "Database has no schema version metadata — likely "
                "uninitialised.  Cannot open in read-only mode."
            ) from exc

        if row is None:
            raise StorageInitializationError(
                "Database has no schema version metadata — likely "
                "uninitialised.  Cannot open in read-only mode."
            )

        stored_version = row["value"]
        try:
            stored_int = int(stored_version)
        except (ValueError, TypeError):
            raise StorageInitializationError(
                f"Storage schema version is not an integer: {stored_version!r}. "
                f"Expected {_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration or "
                f"silent reset is performed.  Options: export data, delete "
                f"the database file, and restart; or downgrade medre to "
                f"match the database version."
            )

    async def close(self) -> None:
        """Close the underlying database connection and release resources.

        Idempotent — safe to call multiple times.  Sets ``_closed`` early
        as a race-safety gate to prevent concurrent-close races; restored
        to ``False`` if the close I/O fails so a later ``close()`` can
        retry.  The aiosqlite close is wrapped in an explicit task and
        shielded so that a stray ``CancelledError`` delivered at this
        await checkpoint (e.g. from caller cancellation or an external
        ``CancelledError``) does not abort the close before aiosqlite's
        internal thread is joined — which would leave the connection
        half-closed and trigger ``ResourceWarning:
        <aiosqlite.core.Connection ...> was deleted before being closed``
        on ``__del__``.

        The private executor is shut down via ``asyncio.to_thread`` with
        ``wait=True`` to fully join worker threads without blocking the
        event loop.
        """
        # Mark closed defensively *before* any I/O so that concurrent
        # callers see the closed flag immediately and do not race.
        if self._closed:
            return
        self._closed = True

        try:
            db = self._db
            if db is not None:
                # Clear the reference *before* the await so concurrent
                # callers see _db as None and return early rather than
                # racing to close the same connection.
                self._db = None
                if self._use_aiosqlite:
                    # Run the close on an explicit task with a strong
                    # reference held by a local binding.  ``asyncio.shield``
                    # then protects the await from being interrupted by a
                    # stray CancelledError, while the local binding keeps
                    # the task alive for the duration of the close.
                    close_task = asyncio.create_task(db.close())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError as orig_cancelled:
                        # Outer cancellation arrived after the close had
                        # already started; let the close finish so aiosqlite
                        # can join its internal thread, then re-raise so the
                        # caller's exception flow continues.
                        try:
                            await close_task
                        except asyncio.CancelledError:
                            # If the close task itself was cancelled, the
                            # database did not close.  Restore state so a
                            # later close() can retry, then propagate the
                            # original cancellation.
                            self._db = db
                            self._closed = False
                            raise orig_cancelled
                        except BaseException as close_exc:
                            # If the close task raises a non-cancellation
                            # exception, we must restore _db so a later
                            # close() can retry, and then re-raise the
                            # close failure. The caller's cancellation
                            # request is superseded by the actual close
                            # failure, which is more informative and
                            # prevents silent resource leaks.
                            self._db = db
                            self._closed = False
                            raise close_exc
                        raise orig_cancelled
                    except BaseException:
                        # On any non-cancellation failure, ensure the close
                        # task is awaited so we don't leak it.  Widen the
                        # inner ``except`` to ``BaseException`` so
                        # ``KeyboardInterrupt`` / ``SystemExit`` cannot
                        # replace the triggering exception with a new
                        # one.
                        if not close_task.done():
                            try:
                                await close_task
                            except BaseException:
                                pass
                        # Restore ``_db`` so a later ``close()`` can
                        # retry.  The close task is either already done
                        # (its exception was raised through the shield)
                        # or is awaited above and any further exception
                        # suppressed because we are about to re-raise
                        # the original.
                        self._db = db
                        self._closed = False
                        raise
                else:
                    try:
                        with self._lock:
                            db.close()
                    except BaseException:
                        # Restore _db and _closed so a later close()
                        # can retry.
                        self._db = db
                        self._closed = False
                        raise
        finally:
            # Always shut down and clear the executor, even if DB close
            # raised an exception.
            executor = self._executor
            if executor is not None:
                self._executor = None
                await asyncio.to_thread(executor.shutdown, wait=True)

    # -- Read / write primitives --------------------------------------------

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single write statement and commit."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    try:
                        await db.execute(sql, params)
                        await db.commit()
                    except BaseException:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise
            else:
                await self._run_in_thread(sync_write, db, self._lock, sql, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Database write failed: {exc}") from exc

    async def _write_batch(self, ops: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple write statements in one transaction and commit."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    try:
                        for sql, params in ops:
                            await db.execute(sql, params)
                        await db.commit()
                    except BaseException:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise
            else:
                await self._run_in_thread(sync_write_batch, db, self._lock, ops)
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            # Only raise DuplicateEventError for canonical_events PK/UNIQUE
            # violations.  Other IntegrityErrors (FK violations, etc.) are
            # raised as generic StorageError.
            if "canonical_events" in msg and (
                "UNIQUE constraint failed" in msg or "PRIMARY KEY" in msg.upper()
            ):
                raise DuplicateEventError(f"Duplicate event: {exc}") from exc
            raise StorageError(f"Batch write failed: {exc}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"Batch write failed: {exc}") from exc

    async def _read_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a read and return the first row as a dict, or ``None``."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with db.execute(sql, params) as cur:
                    row = await cur.fetchone()
                return dict(row) if row else None
            else:
                return await self._run_in_thread(
                    sync_read_one, db, self._lock, sql, params
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Database read failed: {exc}") from exc

    async def _read_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        """Execute a read and return all rows as dicts."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                return [dict(r) for r in rows]
            else:
                return await self._run_in_thread(
                    sync_read_all, db, self._lock, sql, params
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Database read failed: {exc}") from exc


# ---------------------------------------------------------------------------
# SQLiteStorage — public class composing all mixins.
# ---------------------------------------------------------------------------


class SQLiteStorage(
    _EventMixin,
    _NativeRefMixin,
    _RelationMixin,
    _ReceiptMixin,
    _OutboxMixin,
    _CountMixin,
    _SQLiteStorageBase,
):
    """Thread-safe, WAL-mode SQLite storage.

    Implements the :class:`~medre.core.storage.backend.StorageBackend`
    protocol.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Use ``":memory:"``
        for an in-memory database (useful for testing).

    Notes
    -----
    * Prefers *aiosqlite* for native async database access.  When *aiosqlite*
      is not installed the implementation falls back to synchronous
      ``sqlite3`` dispatched through a private ``ThreadPoolExecutor``.
    * The database is opened in WAL mode for safe concurrent reads.
    * All public methods are async and require ``initialize()`` to have been
      called first.
    """
