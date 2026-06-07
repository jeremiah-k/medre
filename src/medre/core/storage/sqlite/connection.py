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
  - sync_read_one / sync_read_all: **internal read primitives**.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA


def sync_open(db_path: str) -> sqlite3.Connection:
    """Open a writable SQLite connection with WAL mode and full schema."""
    db = sqlite3.connect(db_path, check_same_thread=False)
    try:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
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


def sync_write_batch(
    db: sqlite3.Connection,
    lock: threading.Lock,
    ops: list[tuple[str, tuple[Any, ...]]],
) -> None:
    """Execute multiple writes in a single transaction."""
    with lock:
        try:
            for sql, params in ops:
                db.execute(sql, params)
            db.commit()
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
