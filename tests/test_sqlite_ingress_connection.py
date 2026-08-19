"""Direct coverage for synchronous durable-ingress SQLite primitives."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from medre.core.storage.sqlite.connection import (
    sync_admit_ingress,
    sync_claim_ingress_work,
    sync_find_schema_shape_mismatch,
    sync_upsert_checkpoint,
    sync_write_rowcount,
)
from medre.core.storage.sqlite.schema import _SCHEMA


class _RollbackFailingConnection:
    def execute(
        self, _sql: str, _params: tuple[object, ...] = ()
    ) -> sqlite3.Cursor:
        raise sqlite3.OperationalError("execute failed")

    def rollback(self) -> None:
        raise sqlite3.OperationalError("rollback failed")


@pytest.fixture
def ingress_db() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # The production DDL: these primitives must run against the shipped
    # table shapes, never a local copy that can silently drift.
    db.executescript(_SCHEMA)
    try:
        yield db
    finally:
        db.close()


def _event_ops(event_id: str) -> list[tuple[str, tuple[str, ...]]]:
    # Satisfies every NOT NULL column of the production canonical_events
    # DDL (see schema._SCHEMA) so these primitives run against shipped
    # table shapes.
    return [
        (
            """
            INSERT INTO canonical_events
                (event_id, event_kind, schema_version, timestamp,
                 source_adapter, source_transport_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                "message.text",
                1,
                "2026-08-18T23:00:00+00:00",
                "matrix",
                "matrix-main",
                "2026-08-18T23:00:00+00:00",
            ),
        )
    ]


def _native_insert(event_id: str, native_id: str) -> tuple[str, tuple[str, ...]]:
    return (
        """
        INSERT INTO native_message_refs
            (id, event_id, adapter, native_channel_id, native_message_id,
             native_thread_id, native_relation_id, direction, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"ref-{event_id}",
            event_id,
            "matrix-main",
            "!room",
            native_id,
            None,
            None,
            "inbound",
            "2026-08-18T23:00:00+00:00",
        ),
    )


def _admit(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    event_id: str,
    native_id: str | None,
) -> tuple[str, bool]:
    return sync_admit_ingress(
        db,
        lock,
        event_ops=_event_ops(event_id),
        native_identity=("matrix-main", "!room", native_id)
        if native_id is not None
        else None,
        native_insert=_native_insert(event_id, native_id)
        if native_id is not None
        else None,
        event_id=event_id,
        provenance="live",
        work_status="pending",
        now_iso="2026-08-18T23:00:00+00:00",
    )


def test_sync_admit_ingress_covers_new_duplicate_and_repair_paths(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()

    assert _admit(ingress_db, lock, event_id="evt-1", native_id="$same") == (
        "evt-1",
        True,
    )
    assert _admit(ingress_db, lock, event_id="evt-2", native_id="$same") == (
        "evt-1",
        False,
    )

    ingress_db.execute("DELETE FROM durable_ingress_work WHERE event_id='evt-1'")
    ingress_db.commit()
    assert _admit(ingress_db, lock, event_id="evt-3", native_id="$same") == (
        "evt-1",
        False,
    )
    repaired = ingress_db.execute(
        "SELECT status FROM durable_ingress_work WHERE event_id='evt-1'"
    ).fetchone()
    assert repaired is not None and repaired[0] == "pending"


def test_sync_admit_ingress_deduplicates_canonical_id_without_native_ref(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()

    assert _admit(ingress_db, lock, event_id="evt-canonical", native_id=None)[1]
    event_id, created = _admit(
        ingress_db, lock, event_id="evt-canonical", native_id=None
    )

    assert event_id == "evt-canonical"
    assert created is False


def test_sync_admit_ingress_rolls_back_failed_transaction(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()

    with pytest.raises(sqlite3.OperationalError):
        sync_admit_ingress(
            ingress_db,
            lock,
            event_ops=[("INSERT INTO missing_table(value) VALUES (?)", ("x",))],
            native_identity=None,
            native_insert=None,
            event_id="evt-broken",
            provenance="live",
            work_status="pending",
            now_iso="2026-08-18T23:00:00+00:00",
        )

    assert ingress_db.in_transaction is False
    count = ingress_db.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
    assert count == 0


def test_sync_checkpoint_upsert_inserts_and_updates(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()
    sync_upsert_checkpoint(
        ingress_db,
        lock,
        ("matrix-main", "classic_sync", "s1", "{}", "t1"),
    )
    sync_upsert_checkpoint(
        ingress_db,
        lock,
        ("matrix-main", "classic_sync", "s2", '{"recovered":true}', "t2"),
    )

    row = ingress_db.execute(
        "SELECT cursor, metadata, updated_at FROM adapter_checkpoints"
    ).fetchone()
    assert tuple(row) == ("s2", '{"recovered":true}', "t2")


def test_sync_checkpoint_upsert_rolls_back_driver_error(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()
    ingress_db.execute("DROP TABLE adapter_checkpoints")

    with pytest.raises(sqlite3.OperationalError):
        sync_upsert_checkpoint(
            ingress_db,
            lock,
            ("matrix-main", "classic_sync", "s1", "{}", "t1"),
        )

    assert ingress_db.in_transaction is False


def test_sync_claim_ingress_work_claims_pending_and_expired_only(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()
    rows = [
        ("evt-pending", "live", "pending", 0, None, "t0", "t0", None, None, None),
        (
            "evt-expired",
            "recovered",
            "processing",
            2,
            "retry",
            "t0",
            "t0",
            "t0",
            "2026-08-18T22:00:00+00:00",
            "old-worker",
        ),
        (
            "evt-active",
            "live",
            "processing",
            1,
            None,
            "t0",
            "t0",
            "t0",
            "2026-08-19T00:00:00+00:00",
            "active-worker",
        ),
        ("evt-done", "live", "completed", 1, None, "t0", "t0", None, None, None),
    ]
    ingress_db.executemany(
        """
        INSERT INTO durable_ingress_work
            (event_id, provenance, status, attempts, last_error, created_at,
             updated_at, locked_at, lease_until, worker_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    ingress_db.commit()

    claimed = sync_claim_ingress_work(
        ingress_db,
        lock,
        now_iso="2026-08-18T23:00:00+00:00",
        lease_until="2026-08-18T23:00:30+00:00",
        worker_id="worker-new",
        limit=5,
    )

    assert [row["event_id"] for row in claimed] == ["evt-expired", "evt-pending"]
    assert [row["attempts"] for row in claimed] == [3, 1]
    assert all(row["status"] == "processing" for row in claimed)
    assert all(row["worker_id"] == "worker-new" for row in claimed)
    assert sync_claim_ingress_work(
        ingress_db,
        lock,
        now_iso="2026-08-18T23:00:01+00:00",
        lease_until="2026-08-18T23:00:31+00:00",
        worker_id="worker-other",
        limit=5,
    ) == []


def test_sync_claim_ingress_work_rolls_back_driver_error(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()
    ingress_db.execute("DROP TABLE durable_ingress_work")

    with pytest.raises(sqlite3.OperationalError):
        sync_claim_ingress_work(
            ingress_db,
            lock,
            now_iso="2026-08-18T23:00:00+00:00",
            lease_until="2026-08-18T23:00:30+00:00",
            worker_id="worker",
            limit=1,
        )

    assert ingress_db.in_transaction is False


def test_sync_write_rowcount_reports_matches_and_rolls_back_errors(
    ingress_db: sqlite3.Connection,
) -> None:
    lock = threading.Lock()
    ingress_db.execute(
        "INSERT INTO canonical_events (event_id, event_kind, schema_version, timestamp, source_adapter, source_transport_id, created_at) "
        "VALUES ('evt-1', 'message.text', 1, '2026-08-18T23:00:00+00:00', 'matrix', 'matrix-main', '2026-08-18T23:00:00+00:00')"
    )
    ingress_db.commit()

    assert sync_write_rowcount(
        ingress_db,
        lock,
        "UPDATE canonical_events SET event_id=? WHERE event_id=?",
        ("evt-2", "evt-1"),
    ) == 1
    assert sync_write_rowcount(
        ingress_db,
        lock,
        "UPDATE canonical_events SET event_id=? WHERE event_id=?",
        ("evt-3", "missing"),
    ) == 0
    with pytest.raises(sqlite3.OperationalError):
        sync_write_rowcount(ingress_db, lock, "UPDATE missing_table SET value=1")
    assert ingress_db.in_transaction is False


def test_schema_shape_inspection_covers_unstamped_and_stamped_databases(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.db"
    assert sync_find_schema_shape_mismatch(
        str(missing_path), {"durable_ingress_work": frozenset({"status"})}
    ) is None
    assert sync_find_schema_shape_mismatch(
        ":memory:", {"durable_ingress_work": frozenset({"status"})}
    ) is None

    db_path = tmp_path / "shape.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE unrelated (value TEXT)")
    db.commit()
    db.close()
    required = {"durable_ingress_work": frozenset({"event_id", "status"})}
    assert sync_find_schema_shape_mismatch(str(db_path), required) is None

    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE _medre_schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    db.commit()
    db.close()
    assert sync_find_schema_shape_mismatch(str(db_path), required) is None

    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO _medre_schema_meta(key, value) VALUES ('schema_version', '1')"
    )
    db.execute("CREATE TABLE durable_ingress_work (event_id TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    assert sync_find_schema_shape_mismatch(str(db_path), required) == (
        "durable_ingress_work",
        ["status"],
    )

    db = sqlite3.connect(db_path)
    db.execute("ALTER TABLE durable_ingress_work ADD COLUMN status TEXT")
    db.commit()
    db.close()
    assert sync_find_schema_shape_mismatch(str(db_path), required) is None


def test_sync_ingress_helpers_preserve_original_error_when_rollback_fails() -> None:
    db = cast(sqlite3.Connection, _RollbackFailingConnection())
    lock = threading.Lock()

    with pytest.raises(sqlite3.OperationalError, match="execute failed"):
        sync_write_rowcount(db, lock, "UPDATE anything SET value=1")
    with pytest.raises(sqlite3.OperationalError, match="execute failed"):
        sync_admit_ingress(
            db,
            lock,
            event_ops=[],
            native_identity=None,
            native_insert=None,
            event_id="evt",
            provenance="live",
            work_status="pending",
            now_iso="now",
        )
    with pytest.raises(sqlite3.OperationalError, match="execute failed"):
        sync_upsert_checkpoint(db, lock, ("adapter", "stream", "cursor", "{}", "now"))
    with pytest.raises(sqlite3.OperationalError, match="execute failed"):
        sync_claim_ingress_work(
            db,
            lock,
            now_iso="now",
            lease_until="later",
            worker_id="worker",
            limit=1,
        )
