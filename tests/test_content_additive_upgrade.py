"""Only the explicitly authorized prerelease additive DDL is applied."""

import sqlite3
from contextlib import closing

import pytest

from medre.core.storage.backend import StorageInitializationError
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage


def _strip_section(ddl: str, start: str, end: str) -> str:
    """Remove one authored DDL section, from its comment to the next object."""
    begin = ddl.index(start)
    stop = ddl.index(end, begin)
    return ddl[:begin] + ddl[stop:]


def _old_database(path, *, missing_expired_at=False, missing_diagnostics=False):
    ddl = _SCHEMA
    indexes = _INDEXES
    ddl = _strip_section(
        ddl,
        "-- Immutable logical-collection receipt.",
        "CREATE TABLE IF NOT EXISTS event_relations (",
    )
    ddl = _strip_section(
        ddl,
        "-- Typed admission routing facts",
        "-- One current, immutable admission fact per canonical body.",
    )
    ddl = _strip_section(
        ddl,
        "-- Frozen outbox dispatch work snapshot",
        "CREATE TABLE IF NOT EXISTS durable_ingress_work (",
    )
    indexes = _strip_section(
        indexes,
        "-- A frozen work snapshot has one owner:",
        "CREATE INDEX IF NOT EXISTS idx_outbox_event_created",
    )
    if missing_diagnostics:
        ddl = _strip_section(
            ddl,
            "-- Free-form delivery diagnostic text",
            "CREATE TABLE IF NOT EXISTS radio_attempt_progress (",
        )
        indexes = _strip_section(
            indexes,
            "-- Diagnostic coalescing reads",
            "CREATE INDEX IF NOT EXISTS idx_ingress_work_claim",
        )
    if missing_expired_at:
        ddl = ddl.replace("    expired_at TEXT,\n", "")
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ddl + indexes)
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
        assert (await store._read_one("SELECT COUNT(*) AS n FROM event_routing_facts"))[
            "n"
        ] == 0
        owner_index = await store._read_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_outbox_snapshot_owner_unique'"
        )
        assert owner_index is not None
    finally:
        await store.close()
    again = SQLiteStorage(path)
    await again.initialize()
    await again.close()


async def test_missing_diagnostics_table_is_upgraded_atomically(tmp_path):
    """A current predecessor lacking the diagnostics surface is accepted.

    ``_old_database`` strips four tables: the collection log, the routing
    facts and the outbox work snapshot unconditionally, and the diagnostics
    surface for this test. The assertions below pin the table set exactly —
    the upgrade adds those four tables and removes no table. The created
    index and the untouched legacy row are asserted separately in this test.
    """
    path = str(tmp_path / "upgrade-diagnostics.db")
    _old_database(path, missing_diagnostics=True)
    with closing(sqlite3.connect(path)) as db, db:
        before_tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        db.execute(
            "INSERT INTO delivery_receipts "
            "(receipt_id, event_id, delivery_plan_id, target_adapter, status, "
            "receipt_kind, error, created_at) "
            "VALUES ('legacy', 'preserve', 'plan', 'fake', 'failed', "
            "'attempt', 'HISTORICAL-INLINE', '2026-10-31')"
        )
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        columns = {
            r["name"]
            for r in await store._read_all(
                "PRAGMA table_info(delivery_evidence_diagnostics)"
            )
        }
        assert columns == {
            "diag_id",
            "family",
            "row_id",
            "attempt",
            "field",
            "diagnostic",
            "created_at",
        }
        assert (
            await store._read_one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_diagnostics_identity'"
            )
        ) is not None
        # The upgraded shape matches a fresh database exactly; the pre-existing
        # evidence row is untouched and its inline value is never rewritten.
        legacy = await store._read_one(
            "SELECT error FROM delivery_receipts WHERE receipt_id='legacy'"
        )
        assert legacy["error"] == "HISTORICAL-INLINE"
    finally:
        await store.close()
    with closing(sqlite3.connect(path)) as db:
        after_tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    before_names = {name for (name,) in before_tables}
    after_names = {name for (name,) in after_tables}
    assert after_names - before_names == {
        "delivery_evidence_diagnostics",
        "delivery_outbox_snapshots",
        "event_content_collection_log",
        "event_routing_facts",
    }
    assert before_names <= after_names
    again = SQLiteStorage(path)
    await again.initialize()
    await again.close()


async def test_additive_upgrade_rejects_unrelated_shape_drift_without_writes(tmp_path):
    path = str(tmp_path / "tampered.db")
    _old_database(path, missing_expired_at=True, missing_diagnostics=True)
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
        assert (
            db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='delivery_evidence_diagnostics'"
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='event_routing_facts'"
            ).fetchone()
            is None
        )
