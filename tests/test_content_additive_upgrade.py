"""Only the explicitly authorized prerelease additive DDL is applied."""

import sqlite3
from contextlib import closing

import pytest

from medre.core.storage.backend import StorageInitializationError
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage


def _old_database(path, *, missing_expired_at):
    ddl = _SCHEMA
    begin = ddl.index("-- Immutable logical-collection receipt.")
    end = ddl.index("CREATE TABLE IF NOT EXISTS event_relations (", begin)
    ddl = ddl[:begin] + ddl[end:]
    if missing_expired_at:
        ddl = ddl.replace("    expired_at TEXT,\n", "")
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ddl + _INDEXES)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version','1')")
        db.execute(
            "INSERT INTO canonical_events "
            "(event_id,event_kind,schema_version,timestamp,source_adapter,"
            "source_transport_id,lineage,payload,metadata,created_at) "
            "VALUES ('preserve','text',1,'2026-10-31','fake','id','[]','{}','{}','2026-10-31')"
        )
        db.execute(
            "INSERT INTO event_body_identity VALUES ('preserve',1,1,'2026-10-31')"
        )
        db.execute(
            "INSERT INTO event_body_content VALUES ('preserve','body','KEEP ME','2026-10-31')"
        )


@pytest.mark.parametrize("missing_expired_at", [False, True])
async def test_explicit_additions_are_atomic_and_idempotent(
    tmp_path, missing_expired_at
):
    path = str(tmp_path / "upgrade.db")
    _old_database(path, missing_expired_at=missing_expired_at)
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        row = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='preserve'"
        )
        assert row["body"] == "KEEP ME"
        columns = await store._read_all(
            "PRAGMA table_info(event_content_access_grants)"
        )
        assert "expired_at" in {r["name"] for r in columns}
        assert (
            await store._read_one(
                "SELECT COUNT(*) AS n FROM event_content_collection_log"
            )
        )["n"] == 0
    finally:
        await store.close()
    again = SQLiteStorage(path)
    await again.initialize()
    await again.close()


async def test_additive_upgrade_rejects_unrelated_shape_drift_without_writes(tmp_path):
    path = str(tmp_path / "tampered.db")
    _old_database(path, missing_expired_at=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE unexpected_stale_state (value TEXT)")
    store = SQLiteStorage(path)
    with pytest.raises(StorageInitializationError):
        await store.initialize()
    with closing(sqlite3.connect(path)) as db, db:
        columns = [
            r[1] for r in db.execute("PRAGMA table_info(event_content_access_grants)")
        ]
        assert "expired_at" not in columns
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='event_content_collection_log'"
            ).fetchone()
            is None
        )
