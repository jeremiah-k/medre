"""Standalone synchronous I/O functions for SQLite storage.

These functions are dispatched through
:class:`~medre.core.storage.sqlite.storage.SQLiteStorage`'s private
``ThreadPoolExecutor`` via ``loop.run_in_executor``.  Each function is
pure with respect to the connection — no instance state is accessed.

Internal authority:
  - sync_open / sync_open_readonly: infrastructure (connection lifecycle).
  - sync_create_indexes: infrastructure (DDL).
  - sync_write / sync_write_batch: **internal write primitives** — all
    domain authority is enforced by the calling mixin methods, not here.
  - sync_finalize_queued_delivery: guarded cross-table transaction primitive.
  - sync_read_one / sync_read_all: **internal read primitives**.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from medre.core.storage.sqlite.ingress_sql import (
    CLAIM_INGRESS_SELECT,
    CLAIM_INGRESS_UPDATE,
    INSERT_INGRESS_WORK,
    SELECT_CANONICAL_EVENT_ID,
    SELECT_NATIVE_EVENT_ID,
    claimed_ingress_row,
)
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA


def sync_open(db_path: str) -> sqlite3.Connection:
    """Open a writable SQLite connection with WAL mode and full schema."""
    db = sqlite3.connect(db_path, check_same_thread=False)
    try:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        db.commit()
    except BaseException:
        db.close()
        raise
    return db


def sync_open_readonly(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    db = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
    except BaseException:
        db.close()
        raise
    return db


def sync_create_indexes(db: sqlite3.Connection) -> None:
    """Execute index DDL."""
    db.executescript(_INDEXES)
    db.commit()


def sync_write(
    db: sqlite3.Connection,
    lock: threading.Lock,
    sql: str,
    params: tuple[Any, ...] = (),
) -> None:
    """Execute a write, thread-safe via lock."""
    with lock:
        try:
            db.execute(sql, params)
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_write_rowcount(
    db: sqlite3.Connection,
    lock: threading.Lock,
    sql: str,
    params: tuple[Any, ...] = (),
) -> int:
    """Execute a write and return the number of rows affected."""
    with lock:
        try:
            cursor = db.execute(sql, params)
            db.commit()
            return int(cursor.rowcount)
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_find_schema_shape_mismatch(
    db_path: str, required_columns: dict[str, frozenset[str]]
) -> tuple[str, list[str]] | None:
    """Inspect a stamped existing database before any schema DDL runs."""
    if db_path == ":memory:" or not os.path.exists(db_path):
        return None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_medre_schema_meta'"
        ).fetchone()
        if meta is None:
            return None
        version = db.execute(
            "SELECT value FROM _medre_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None:
            return None
        for table, required in required_columns.items():
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {str(row[1]) for row in rows}
            missing = sorted(required - existing)
            if missing:
                return table, missing
        return None
    finally:
        db.close()


def sync_write_batch(
    db: sqlite3.Connection,
    lock: threading.Lock,
    ops: list[tuple[str, tuple[Any, ...]]],
) -> None:
    """Execute multiple writes in a single transaction.

    Atomicity is explicit: ``BEGIN IMMEDIATE`` is issued before the
    loop and the final ``COMMIT`` happens at the end.  This guards
    against future changes to the connection's
    :attr:`sqlite3.Connection.isolation_level` (autocommit / deferred /
    exclusive modes) silently breaking batch atomicity.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            for sql, params in ops:
                db.execute(sql, params)
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_finalize_queued_delivery(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    native_identity: tuple[str, str | None, str],
    native_event_id: str,
    native_insert_params: tuple[object, ...],
    receipt_insert_params: tuple[object, ...],
    outbox_update_params: tuple[object, ...],
) -> tuple[bool, str | None]:
    """Atomically finalize one queue-backed delivery attempt.

    Returns ``(committed, conflicting_event_id)``.  A false commit with no
    conflict means the guarded outbox attempt was no longer finalizable.
    """
    from medre.core.storage.sqlite.statements import (
        _FINALIZE_QUEUED_OUTBOX_SENT,
        _INSERT_NATIVE_REF_STRICT,
        _INSERT_RECEIPT,
        _RESOLVE_NATIVE_REF,
    )

    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(_RESOLVE_NATIVE_REF, native_identity).fetchone()
            existing_event_id = str(row[0]) if row is not None else None
            if existing_event_id is not None and existing_event_id != native_event_id:
                db.rollback()
                return False, existing_event_id

            cursor = db.execute(_FINALIZE_QUEUED_OUTBOX_SENT, outbox_update_params)
            if int(cursor.rowcount) != 1:
                db.rollback()
                return False, None

            if existing_event_id is None:
                db.execute(_INSERT_NATIVE_REF_STRICT, native_insert_params)
            db.execute(_INSERT_RECEIPT, receipt_insert_params)
            db.commit()
            return True, None
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_read_one(
    db: sqlite3.Connection,
    lock: threading.Lock,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    """Read one row, return dict or None."""
    with lock:
        row = db.execute(sql, params).fetchone()
    return dict(row) if row else None


def sync_read_all(
    db: sqlite3.Connection,
    lock: threading.Lock,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Read all rows as dicts."""
    with lock:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def sync_admit_ingress(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    event_ops: list[tuple[str, tuple[Any, ...]]],
    native_identity: tuple[str, str | None, str] | None,
    native_insert: tuple[str, tuple[Any, ...]] | None,
    event_id: str,
    provenance: str,
    work_status: str,
    now_iso: str,
) -> tuple[str, bool]:
    """Atomically admit an event/native ref/work marker.

    Existing native identities are repaired with a missing work row in the
    same transaction so an incomplete prior admission cannot silently mark an
    event complete without ever routing it.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            existing_event_id: str | None = None
            if native_identity is not None:
                row = db.execute(SELECT_NATIVE_EVENT_ID, native_identity).fetchone()
                if row is not None:
                    existing_event_id = str(row[0])
            else:
                row = db.execute(SELECT_CANONICAL_EVENT_ID, (event_id,)).fetchone()
                if row is not None:
                    existing_event_id = event_id
            if existing_event_id is not None:
                work_row = db.execute(
                    "SELECT 1 FROM durable_ingress_work WHERE event_id = ?",
                    (existing_event_id,),
                ).fetchone()
                if work_row is None:
                    db.execute(
                        INSERT_INGRESS_WORK,
                        (existing_event_id, provenance, work_status, now_iso, now_iso),
                    )
                db.commit()
                return existing_event_id, False

            for sql, params in event_ops:
                db.execute(sql, params)
            if native_insert is not None:
                db.execute(*native_insert)
            db.execute(
                INSERT_INGRESS_WORK,
                (event_id, provenance, work_status, now_iso, now_iso),
            )
            db.commit()
            return event_id, True
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_upsert_checkpoint(
    db: sqlite3.Connection,
    lock: threading.Lock,
    params: tuple[Any, ...],
) -> None:
    """Persist one application-owned adapter checkpoint."""
    with lock:
        try:
            db.execute(
                """
                INSERT INTO adapter_checkpoints
                    (adapter_id, stream, cursor, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(adapter_id, stream) DO UPDATE SET
                    cursor=excluded.cursor,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                params,
            )
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_claim_ingress_work(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    now_iso: str,
    lease_until: str,
    worker_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Atomically claim pending or stale durable-ingress work."""
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(CLAIM_INGRESS_SELECT, (now_iso, limit)).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                event_id = str(row[0])
                db.execute(
                    CLAIM_INGRESS_UPDATE,
                    (now_iso, lease_until, worker_id, now_iso, event_id),
                )
                claimed.append(
                    claimed_ingress_row(
                        row,
                        now_iso=now_iso,
                        lease_until=lease_until,
                        worker_id=worker_id,
                    )
                )
            db.commit()
            return claimed
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise
