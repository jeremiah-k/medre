"""Shared read-only catalog assessment: classification and I/O discipline.

Covers the classification matrix against databases built from the
authoritative DDL (single anchored line replacements only — the same
derivation discipline the exact validators use, never a second fingerprint
inventory), the bounded ``unavailable`` verdict for assessment I/O
failures, the single-snapshot read discipline, and live-WAL visibility
without checkpointing or sidecar writes to application data.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.sqlite import catalog_assessment
from medre.core.storage.sqlite._prerelease_migrate import (
    known_outage_gap_table_forms,
    outage_gap_token_rebuild_applies,
)
from medre.core.storage.sqlite.catalog_assessment import (
    CatalogCompatibility,
    assess_catalog,
    assess_storage_catalog,
)
from medre.core.storage.sqlite.connection import sync_open, sync_open_readonly
from medre.core.storage.sqlite.schema import (
    _INDEXES,
    _PRESSURE_OUTAGE_GAPS_DDL,
    _SCHEMA,
)
from medre.core.storage.sqlite.schema_validation import validate_schema_definition

_GAP_TOKEN_COLUMN = (
    "    gap_token TEXT NOT NULL UNIQUE CHECK (length(gap_token) = 32),\n"
)
_LINEAGE_DEFAULT = "    lineage TEXT NOT NULL DEFAULT '[]',\n"
_GRANT_FK_LINE = (
    "    event_id TEXT NOT NULL REFERENCES event_content_access_policies(event_id),\n"
)


def _build_current(path: Path, *, ddl: str | None = None) -> Path:
    """Build a stamped database at the exact declared catalog."""
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(ddl if ddl is not None else _SCHEMA + _INDEXES)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
    return path


def _classify(path: Path) -> CatalogCompatibility:
    return assess_storage_catalog(str(path)).classification


# ---------------------------------------------------------------------------
# Tests: unsupported classification matrix
# ---------------------------------------------------------------------------


def test_current_catalog_classifies_current(tmp_path: Path) -> None:
    assessment = assess_storage_catalog(str(_build_current(tmp_path / "ok.sqlite")))
    assert assessment.classification is CatalogCompatibility.CURRENT
    assert assessment.stored_version == "1"
    assert assessment.detail is None


@pytest.mark.parametrize("stamp", [b"1", "01", " 1 "])
def test_current_stamp_parsing_agrees_with_startup(
    tmp_path: Path, stamp: str | bytes
) -> None:
    """Display serialization must not change the validator's version semantics."""
    path = _build_current(tmp_path / "stamp.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE _medre_schema_meta SET value=? WHERE key='schema_version'",
            (stamp,),
        )
        assert validate_schema_definition(db, str(path))
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.CURRENT
    assert assessment.stored_version == str(stamp)
    with closing(sync_open(str(path))) as db:
        assert validate_schema_definition(db, str(path))


def test_noncanonical_stamp_does_not_expand_predecessor_allowlist(
    tmp_path: Path,
) -> None:
    path = _build_current(tmp_path / "predecessor.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE event_routing_facts")
        db.execute(
            "UPDATE _medre_schema_meta SET value=? WHERE key='schema_version'",
            (b"1",),
        )
    assert _classify(path) is CatalogCompatibility.UNSUPPORTED


def test_changed_default_is_unsupported(tmp_path: Path) -> None:
    assert _SCHEMA.count(_LINEAGE_DEFAULT) == 1
    ddl = _SCHEMA.replace(_LINEAGE_DEFAULT, "    lineage TEXT NOT NULL DEFAULT '{}',\n")
    assessment = assess_storage_catalog(
        str(_build_current(tmp_path / "default.sqlite", ddl=ddl + _INDEXES))
    )
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.detail is not None
    assert "canonical_events" in assessment.detail


def test_changed_foreign_key_is_unsupported(tmp_path: Path) -> None:
    assert _SCHEMA.count(_GRANT_FK_LINE) == 1
    ddl = _SCHEMA.replace(
        _GRANT_FK_LINE,
        "    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),\n",
    )
    assessment = assess_storage_catalog(
        str(_build_current(tmp_path / "fk.sqlite", ddl=ddl + _INDEXES))
    )
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.detail is not None
    assert "event_content_access_grants" in assessment.detail


def test_extra_column_is_unsupported(tmp_path: Path) -> None:
    assert _SCHEMA.count(_LINEAGE_DEFAULT) == 1
    ddl = _SCHEMA.replace(_LINEAGE_DEFAULT, _LINEAGE_DEFAULT + "    probe TEXT,\n")
    assessment = assess_storage_catalog(
        str(_build_current(tmp_path / "extra-col.sqlite", ddl=ddl + _INDEXES))
    )
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.detail is not None
    assert "unexpected columns" in assessment.detail


def test_extra_table_is_unsupported(tmp_path: Path) -> None:
    path = _build_current(tmp_path / "extra-table.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE foreign_app_data (value TEXT)")
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.detail is not None
    assert "foreign_app_data" in assessment.detail


@pytest.mark.parametrize("stamp", ["99", "not-an-integer"])
def test_invalid_version_stamps_are_unsupported(tmp_path: Path, stamp: str) -> None:
    path = _build_current(tmp_path / "version.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE _medre_schema_meta SET value=? WHERE key='schema_version'",
            (stamp,),
        )
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.stored_version == stamp


def test_missing_version_stamp_is_unsupported(tmp_path: Path) -> None:
    path = _build_current(tmp_path / "stamp.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM _medre_schema_meta")
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.detail is not None
    assert "schema_version stamp is missing" in assessment.detail


def test_foreign_database_is_unsupported(tmp_path: Path) -> None:
    """A readable foreign application catalog is unsupported, not new state."""
    path = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE address_book (name TEXT NOT NULL)")
        db.execute("INSERT INTO address_book VALUES ('someone')")
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.stored_version is None
    assert assessment.detail is not None
    assert "unstamped" in assessment.detail


def test_gap_token_predecessor_classifies_supported_upgrade(tmp_path: Path) -> None:
    """The exact no-retry-token outage-gap layout is a recognized upgrade.

    The predecessor DDL is derived from the authoritative outage-gap slice
    by removing the single retry-token column line, and the built table is
    asserted identical to the authority's own predecessor derivation, so
    this fixture cannot drift from the allowlisted family.
    """
    assert _PRESSURE_OUTAGE_GAPS_DDL.count(_GAP_TOKEN_COLUMN) == 1
    legacy_gap_ddl = _PRESSURE_OUTAGE_GAPS_DDL.replace(_GAP_TOKEN_COLUMN, "")
    assert _SCHEMA.count(_PRESSURE_OUTAGE_GAPS_DDL) == 1
    path = _build_current(
        tmp_path / "gap.sqlite",
        ddl=_SCHEMA.replace(_PRESSURE_OUTAGE_GAPS_DDL, legacy_gap_ddl) + _INDEXES,
    )
    with closing(sqlite3.connect(path)) as db:
        stored = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='inbound_pressure_outage_gaps'"
        ).fetchone()
        assert stored is not None
        assert stored[0] == known_outage_gap_table_forms().legacy
        assert outage_gap_token_rebuild_applies(db)

    before = path.read_bytes()
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
    assert assessment.detail is not None
    assert "inbound_pressure_outage_gaps" in assessment.detail
    assert path.read_bytes() == before


def test_missing_allowlisted_objects_classify_supported_upgrade(
    tmp_path: Path,
) -> None:
    """A current catalog missing allowlisted additions still upgrades."""
    path = _build_current(tmp_path / "additive.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE event_routing_facts")
        db.execute("DROP INDEX idx_events_admission_order")
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
    assert assessment.detail is not None
    assert "event_routing_facts" in assessment.detail
    assert "idx_events_admission_order" in assessment.detail


# ---------------------------------------------------------------------------
# Tests: empty/new, absent path, and bounded unavailable behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["zero-bytes.sqlite", "fresh-file.sqlite"])
def test_empty_database_classifies_empty_new(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    if name == "zero-bytes.sqlite":
        path.write_bytes(b"")
    else:
        sqlite3.connect(path).close()
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.EMPTY_NEW
    assert assessment.stored_version is None


def test_absent_path_raises_for_the_caller(tmp_path: Path) -> None:
    """The read-only opener refuses a missing file without creating one."""
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        assess_storage_catalog(str(missing))
    assert not missing.exists()


def test_corrupt_file_is_unavailable(tmp_path: Path) -> None:
    """An unreadable file yields a distinct verdict, never a crash."""
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not a database at all" * 32)
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNAVAILABLE
    assert assessment.detail is not None
    assert assessment.detail != ""


def test_locked_database_is_unavailable_within_busy_bound(tmp_path: Path) -> None:
    """A held exclusive lock bounds the assessment instead of hanging."""
    path = _build_current(tmp_path / "locked.sqlite")
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        reader.execute("PRAGMA busy_timeout=50")
        reader.execute("BEGIN")
        try:
            started = time.monotonic()
            assessment = assess_catalog(reader, path=str(path))
            elapsed = time.monotonic() - started
        finally:
            reader.close()
        assert assessment.classification is CatalogCompatibility.UNAVAILABLE
        assert assessment.detail is not None
        assert "locked" in assessment.detail.lower() or "busy" in (
            assessment.detail.lower()
        )
        assert elapsed < 5
    finally:
        writer.rollback()
        writer.close()


# ---------------------------------------------------------------------------
# Tests: snapshot discipline and live-WAL visibility
# ---------------------------------------------------------------------------


def test_assessment_reads_one_consistent_snapshot(tmp_path: Path) -> None:
    """One read transaction answers for the whole verdict.

    A concurrent writer commits catalog and metadata changes between two
    assessment reads; the pinned snapshot keeps the verdict stable, and a
    fresh reader observes the committed live-WAL state afterwards. The
    database runs in WAL mode so the writer can commit while the reader
    holds its snapshot.
    """
    path = _build_current(tmp_path / "snapshot.sqlite")
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        reader = sync_open_readonly(str(path))
        try:
            reader.execute("BEGIN")
            pinned = assess_catalog(reader, path=str(path))
            assert pinned.classification is CatalogCompatibility.CURRENT

            writer.execute("CREATE TABLE later_probe (value TEXT)")
            writer.execute(
                "INSERT INTO _medre_schema_meta VALUES ('probed_later', '1')"
            )
            writer.commit()

            again = assess_catalog(reader, path=str(path))
            assert again == pinned
            assert (
                reader.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='later_probe'"
                ).fetchone()[0]
                == 0
            )
            reader.rollback()
        finally:
            reader.close()

        # A fresh assessment observes the committed live-WAL catalog: the
        # extra table is now a real, unsupported catalog difference.
        after = assess_storage_catalog(str(path))
        assert after.classification is CatalogCompatibility.UNSUPPORTED
        assert after.detail is not None
        assert "later_probe" in after.detail
        fresh = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert (
                fresh.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='later_probe'"
                ).fetchone()[0]
                == 1
            )
            assert (
                fresh.execute(
                    "SELECT COUNT(*) FROM _medre_schema_meta WHERE key='probed_later'"
                ).fetchone()[0]
                == 1
            )
        finally:
            fresh.close()
    finally:
        writer.close()


def test_public_assessment_owns_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit between the first catalog read and subsequent metadata reads."""
    path = _build_current(tmp_path / "public-snapshot.sqlite")
    original = catalog_assessment._definitions
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        committed = False

        def definitions_then_commit(db: sqlite3.Connection):
            nonlocal committed
            definitions = original(db)
            if not committed:
                committed = True
                writer.execute("DROP INDEX idx_outbox_active_adapter")
                writer.execute(
                    "UPDATE _medre_schema_meta SET value='99'"
                    " WHERE key='schema_version'"
                )
                writer.commit()
            return definitions

        monkeypatch.setattr(catalog_assessment, "_definitions", definitions_then_commit)
        pinned = assess_storage_catalog(str(path))
        assert committed
        assert pinned.classification is CatalogCompatibility.CURRENT
        assert pinned.stored_version == "1"
        fresh = assess_storage_catalog(str(path))
        assert fresh.classification is CatalogCompatibility.UNSUPPORTED
        assert fresh.stored_version == "99"


def test_committed_live_wal_state_is_visible_without_writes(tmp_path: Path) -> None:
    """Committed WAL frames are readable and never rewritten or checkpointed.

    A writer connection stays open with committed frames still in the WAL;
    the assessment observes the current catalog, the main database and the
    WAL file keep their exact bytes (creating the ``-shm`` bookkeeping file
    is SQLite shared memory, not an application-data write), and no
    checkpoint is performed.
    """
    path = tmp_path / "wal.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(_SCHEMA + _INDEXES)
        writer.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        writer.commit()
        writer.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('wal-event', 'text', 1, '2026-11-14', 'radio', 'n',"
            " '2026-11-14')"
        )
        writer.commit()

        main_before = path.read_bytes()
        wal_path = tmp_path / "wal.sqlite-wal"
        wal_before = wal_path.read_bytes()

        assessment = assess_storage_catalog(str(path))
        assert assessment.classification is CatalogCompatibility.CURRENT

        fresh = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert (
                fresh.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE event_id='wal-event'"
                ).fetchone()[0]
                == 1
            )
        finally:
            fresh.close()

        # No checkpoint, no WAL rewrite, no application-data change.
        assert path.read_bytes() == main_before
        assert wal_path.read_bytes() == wal_before
    finally:
        writer.close()


def test_stopped_wal_fixture_retains_bytes_and_catalog(tmp_path: Path) -> None:
    """A cleanly stopped WAL database keeps its bytes through assessment."""
    path = _build_current(tmp_path / "stopped.sqlite")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
    stopped_bytes = path.read_bytes()
    stopped_listing = sorted(entry.name for entry in tmp_path.iterdir())

    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.CURRENT

    assert path.read_bytes() == stopped_bytes
    after_listing = sorted(entry.name for entry in tmp_path.iterdir())
    # Any difference is SQLite shared-memory bookkeeping only; application
    # data and the main database file are byte-identical.
    assert set(after_listing) - set(stopped_listing) <= {
        "stopped.sqlite-shm",
        "stopped.sqlite-wal",
    }
    with closing(sqlite3.connect(path)) as db:
        names = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "canonical_events" in names
