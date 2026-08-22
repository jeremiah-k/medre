"""SQLite-backed storage backend for the medre.

Uses the standard-library ``sqlite3`` driver behind a private single-worker
``ThreadPoolExecutor`` so the public storage API remains asynchronous without
maintaining a second SQLite implementation.  The database runs in WAL mode for
safe concurrent reads.

Storage authority summary:
  - canonical_events: **create** (append-only ingress facts).
  - native_message_refs: **create** (idempotent transport correlation facts).
  - event_relations: **create** (append alongside events).
  - conversation_membership: mutable, rebuildable derived graph projection;
    canonical event evidence is never rewritten during repair.
  - conversation_projection_state: mutable singleton startup/rebuild marker;
    never treated as event evidence.
  - delivery_receipts: **append** (append-only historical delivery evidence;
    never updated or deleted by runtime code).
  - delivery_outbox: mutable operational state until terminal, then immutable
    operational history.  Terminal rows are never deleted or replaced.
  - No DELETE statements exist in the storage layer.  No runtime code path
    deletes historical data.
  - ``open_readonly`` opens a strict read-only connection — suitable for
    ``medre inspect`` commands that must never mutate storage.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from medre.core.storage.backend import (
    DuplicateEventError,
    PreReleaseSchemaConstraintMismatchError,
    PreReleaseSchemaMismatchError,
    StorageError,
    StorageInitializationError,
)

# Mixin imports — method groups composed via multiple inheritance.
from medre.core.storage.sqlite._conversation import _ConversationMixin
from medre.core.storage.sqlite._count import _CountMixin
from medre.core.storage.sqlite._delivery_finalize import _DeliveryFinalizationMixin
from medre.core.storage.sqlite._event import _EventMixin
from medre.core.storage.sqlite._ingress import _IngressMixin
from medre.core.storage.sqlite._native_ref import _NativeRefMixin
from medre.core.storage.sqlite._outbox import _OutboxMixin
from medre.core.storage.sqlite._receipt import _ReceiptMixin
from medre.core.storage.sqlite._relation import _RelationMixin
from medre.core.storage.sqlite.connection import (
    sync_close,
    sync_create_indexes,
    sync_open,
    sync_open_readonly,
    sync_find_schema_shape_mismatch,
    sync_read_all,
    sync_read_one,
    sync_write,
    sync_write_rowcount,
    sync_write_batch,
)
from medre.core.storage.sqlite.schema import (
    _EXPECTED_SCHEMA_VERSION,
    _REQUIRED_CHECK_CONSTRAINTS,
    _REQUIRED_COLUMNS,
    _REQUIRED_FOREIGN_KEYS,
)

logger = logging.getLogger(__name__)


def _skip_sql_quoted_or_comment(sql: str, index: int) -> int | None:
    """Return the first index after a quoted/comment token starting at *index*.

    SQLite table definitions may contain arbitrary text inside quoted
    identifiers, string literals, or comments.  Schema validation must not
    interpret a fake ``CHECK (...)`` embedded there as an enforced constraint.
    """
    if sql.startswith("--", index):
        newline = sql.find("\n", index + 2)
        return len(sql) if newline < 0 else newline + 1
    if sql.startswith("/*", index):
        end = sql.find("*/", index + 2)
        return len(sql) if end < 0 else end + 2

    opener = sql[index]
    if opener == "[":
        end = sql.find("]", index + 1)
        return len(sql) if end < 0 else end + 1
    if opener not in {"'", '"', "`"}:
        return None

    cursor = index + 1
    while cursor < len(sql):
        if sql[cursor] != opener:
            cursor += 1
            continue
        if cursor + 1 < len(sql) and sql[cursor + 1] == opener:
            cursor += 2
            continue
        return cursor + 1
    return len(sql)


def _extract_check_clauses(definition: str) -> tuple[str, ...]:
    """Extract real table-level ``CHECK`` clauses from SQLite DDL.

    The scanner recognizes ``CHECK`` only outside quoted identifiers, string
    literals, and comments, then balances the constraint parentheses while
    applying the same quoting rules.  It is deliberately small and SQLite
    specific; it is not a general SQL parser.
    """
    clauses: list[str] = []
    cursor = 0
    length = len(definition)
    while cursor < length:
        skipped = _skip_sql_quoted_or_comment(definition, cursor)
        if skipped is not None:
            cursor = skipped
            continue

        if definition[cursor : cursor + 5].upper() != "CHECK":
            cursor += 1
            continue
        before = definition[cursor - 1] if cursor else " "
        after_index = cursor + 5
        after = definition[after_index] if after_index < length else " "
        if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
            cursor += 1
            continue

        paren = after_index
        while paren < length and definition[paren].isspace():
            paren += 1
        if paren >= length or definition[paren] != "(":
            cursor += 5
            continue

        depth = 0
        scan = paren
        while scan < length:
            skipped = _skip_sql_quoted_or_comment(definition, scan)
            if skipped is not None:
                scan = skipped
                continue
            char = definition[scan]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    clauses.append(definition[cursor : scan + 1])
                    cursor = scan + 1
                    break
            scan += 1
        else:
            cursor = length

    return tuple(clauses)


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
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._closed: bool = False

    # -- Internal helpers ---------------------------------------------------

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a synchronous function in the private executor."""
        if self._closed:
            raise RuntimeError("SQLiteStorage is closed.")
        executor = self._executor
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1)
            self._executor = executor
        loop = asyncio.get_running_loop()
        if kwargs:
            func = functools.partial(func, **kwargs)
        return await loop.run_in_executor(executor, func, *args)

    def _require_db(self) -> sqlite3.Connection:
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
            (no silent schema transformation or reset).
        """
        self._closed = False
        try:
            mismatch = await asyncio.to_thread(
                sync_find_schema_shape_mismatch, self._db_path, _REQUIRED_COLUMNS
            )
        except sqlite3.Error as exc:
            raise StorageInitializationError(
                f"Storage database is unreadable or corrupt: {self._db_path}: {exc}"
            ) from exc
        if mismatch is not None:
            table, missing = mismatch
            raise PreReleaseSchemaMismatchError(
                path=self._db_path, table=table, missing_columns=missing
            )
        self._db = await self._run_in_thread(sync_open, self._db_path)

        try:
            # Verify schema version after DDL.
            await self._verify_schema_version()

            # Verify structural shape — catches old pre-release DBs that claim
            # schema_version=1 but predate current columns or constraints.
            await self._validate_schema_shape()
            await self._validate_schema_foreign_keys()
            await self._validate_schema_checks()

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
                "Resolve the mismatch manually — no automatic schema "
                "transformation is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                "Resolve the mismatch manually — no automatic schema transformation "
                "or "
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
            If any required table or column is missing.  No automatic schema
            transformation is performed; the operator must recreate the DB.
        """
        for table, required in _REQUIRED_COLUMNS.items():
            rows = await self._read_all(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in rows}
            missing = required - existing
            if missing:
                raise PreReleaseSchemaMismatchError(
                    path=self._db_path,
                    table=table,
                    missing_columns=sorted(missing),
                )

    async def _validate_schema_foreign_keys(self) -> None:
        """Reject pre-release tables that lack required foreign-key mappings."""
        for table, required in _REQUIRED_FOREIGN_KEYS.items():
            rows = await self._read_all(f"PRAGMA foreign_key_list({table})")
            existing = {
                (str(row["from"]), str(row["table"]), str(row["to"]))
                for row in rows
            }
            missing = required - existing
            if missing:
                formatted = [
                    f"{source} -> {target_table}.{target_column}"
                    for source, target_table, target_column in sorted(missing)
                ]
                raise PreReleaseSchemaConstraintMismatchError(
                    path=self._db_path,
                    table=table,
                    missing_constraints=formatted,
                )

    async def _validate_schema_checks(self) -> None:
        """Reject pre-release tables that omit required CHECK constraints."""
        for table, required in _REQUIRED_CHECK_CONSTRAINTS.items():
            row = await self._read_one(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            )
            definition = str(row["sql"]) if row is not None else ""
            check_clauses = _extract_check_clauses(definition)
            missing = [
                label
                for label, pattern in required
                if not any(
                    re.fullmatch(pattern, clause, flags=re.IGNORECASE)
                    for clause in check_clauses
                )
            ]
            if missing:
                raise PreReleaseSchemaConstraintMismatchError(
                    path=self._db_path,
                    table=table,
                    missing_constraints=missing,
                )

    async def _create_indexes(self) -> None:
        """Create targeted indexes for current query patterns.

        Called after :meth:`_validate_schema_shape` so that old-shape
        databases raise :class:`StorageInitializationError` *before*
        any index DDL references columns that may not exist.
        """
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
        instance._db = await instance._run_in_thread(
            sync_open_readonly, instance._db_path
        )

        try:
            # Validate metadata and shape without writing anything.
            await instance._verify_schema_version_readonly()
            await instance._validate_schema_shape()
            await instance._validate_schema_foreign_keys()
            await instance._validate_schema_checks()
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
                "Resolve the mismatch manually — no automatic schema "
                "transformation is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                "Resolve the mismatch manually — no automatic schema transformation "
                "or "
                f"silent reset is performed.  Options: export data, delete "
                f"the database file, and restart; or downgrade medre to "
                f"match the database version."
            )

    async def close(self) -> None:
        """Close the SQLite connection and private executor.

        Idempotent — safe to call multiple times.  ``_closed`` is set before
        I/O so concurrent callers cannot dispatch new work while shutdown is
        in progress.  Cancellation is deferred until executor-owned cleanup
        completes.  A genuine connection-close failure restores state so a
        later call can retry.
        """
        if self._closed:
            return
        self._closed = True

        db = self._db
        executor = self._executor
        if db is not None and executor is None:
            executor = ThreadPoolExecutor(max_workers=1)
            self._executor = executor

        deferred_cancel: asyncio.CancelledError | None = None
        close_error: BaseException | None = None
        shutdown_error: BaseException | None = None
        try:
            if db is not None:
                assert executor is not None
                self._db = None
                loop = asyncio.get_running_loop()
                close_future = loop.run_in_executor(
                    executor, sync_close, db, self._lock
                )
                close_error, cancelled = await self._await_deferring_cancel(
                    close_future
                )
                if deferred_cancel is None:
                    deferred_cancel = cancelled
                if close_error is not None:
                    self._db = db
                    self._closed = False
        finally:
            if executor is not None:
                self._executor = None
                shutdown_future = asyncio.ensure_future(
                    asyncio.to_thread(executor.shutdown, wait=True)
                )
                shutdown_error, cancelled = await self._await_deferring_cancel(
                    shutdown_future
                )
                if deferred_cancel is None:
                    deferred_cancel = cancelled

        if close_error is not None:
            if deferred_cancel is not None:
                raise close_error from deferred_cancel
            raise close_error
        if shutdown_error is not None:
            if deferred_cancel is not None:
                raise shutdown_error from deferred_cancel
            raise shutdown_error
        if deferred_cancel is not None:
            raise deferred_cancel

    @staticmethod
    async def _await_deferring_cancel(
        future: asyncio.Future[Any],
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        """Await *future* to completion while deferring caller cancellation."""
        deferred: asyncio.CancelledError | None = None
        error: BaseException | None = None
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                if deferred is None:
                    deferred = exc
            except BaseException as exc:
                error = exc
                break
        if error is None:
            try:
                future.result()
            except BaseException as exc:
                error = exc
        return error, deferred

    # -- Read / write primitives --------------------------------------------

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single write statement and commit."""
        db = self._require_db()
        try:
            await self._run_in_thread(sync_write, db, self._lock, sql, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Database write failed: {exc}") from exc

    async def _write_rowcount(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> int:
        """Execute one write and return its affected-row count."""
        db = self._require_db()
        try:
            return await self._run_in_thread(
                sync_write_rowcount, db, self._lock, sql, params
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Database write failed: {exc}") from exc

    async def _write_batch(self, ops: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple writes in one explicit transaction and commit."""
        db = self._require_db()
        try:
            await self._run_in_thread(sync_write_batch, db, self._lock, ops)
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
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
    _IngressMixin,
    _NativeRefMixin,
    _RelationMixin,
    _ConversationMixin,
    _ReceiptMixin,
    _OutboxMixin,
    _DeliveryFinalizationMixin,
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
    * Uses ``sqlite3`` through a private single-worker ``ThreadPoolExecutor``.
      The async API never changes implementation based on ambient packages.
    * The database is opened in WAL mode for safe concurrent reads.
    * All public methods are async and require ``initialize()`` to have been
      called first.
    """
