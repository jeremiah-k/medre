"""Open a populated released catalog, independently of the current DDL.

The SQL fixture is the verbatim concatenation of _SCHEMA and _INDEXES from
0.1.0 (fd2927d1a518c792f85bf8c31c297dbe6e7e7cb8). The 0.1.1 tag has the
same DDL and version stamp. Do not regenerate the fixture from current code:
future migrations must continue to accept this released baseline. When a
new catalog is introduced, update the expected post-migration state, not
the fixture.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.catalog_assessment import (
    CatalogCompatibility,
    assess_storage_catalog,
)
from medre.core.storage.sqlite.connection import (
    sync_find_schema_shape_mismatch,
    sync_open,
    sync_open_readonly,
)
from medre.core.storage.sqlite.schema import _REQUIRED_COLUMNS
from medre.core.storage.sqlite.schema_validation import (
    safe_additive_state,
    validate_schema_definition,
)

_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"
_NOW = "2026-11-14T00:00:00+00:00"


def _populate(path: Path) -> None:
    ddl = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(ddl).hexdigest() == _FIXTURE_SHA256
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl.decode("utf-8"))
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.execute(
            """INSERT INTO canonical_events
               (event_id, event_kind, schema_version, timestamp, source_adapter,
                source_transport_id, created_at)
               VALUES ('event', 'message.file', 1, ?, 'radio', 'radio', ?)""",
            (_NOW, _NOW),
        )
        content = b"released attachment bytes"
        ref = "sha256:" + hashlib.sha256(content).hexdigest()
        db.execute(
            """INSERT INTO attachment_blobs
               (content_ref, size_bytes, mime_type, created_at, data)
               VALUES (?, ?, 'application/octet-stream', ?, ?)""",
            (ref, len(content), _NOW, content),
        )
        db.execute(
            "INSERT INTO event_attachment_associations VALUES ('event', ?, ?)",
            (ref, _NOW),
        )
        db.execute(
            """INSERT INTO native_message_refs
               (id,event_id,adapter,native_message_id,direction,created_at)
               VALUES ('ref','event','radio','native-event','inbound',?)""",
            (_NOW,),
        )
        db.execute(
            """INSERT INTO delivery_outbox
               (outbox_id,event_id,delivery_plan_id,target_adapter,created_at,updated_at)
               VALUES ('work','event','plan','matrix',?,?)""",
            (_NOW, _NOW),
        )
        db.execute(
            """INSERT INTO delivery_receipts
               (receipt_id,event_id,delivery_plan_id,target_adapter,status,
                receipt_kind,created_at)
               VALUES ('receipt','event','plan','matrix','failed','attempt',?)""",
            (_NOW,),
        )


def _state(
    db: sqlite3.Connection,
) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    catalog = [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL ORDER BY type,name"
        )
    ]
    rows = {
        name: [
            tuple(row)
            for row in db.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid'  # nosec B608 # The identifier comes from the persisted catalog enumeration, never external input.
            )
        ]
        for kind, name, _ in catalog
        if kind == "table"
    }
    assert list(db.execute("PRAGMA foreign_key_check")) == []
    return catalog, rows


def test_released_catalog_sql_boundary_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before = _state(db)
    assert {"attachment_blobs", "event_attachment_associations"} <= before[1].keys()
    assert sync_find_schema_shape_mismatch(str(path), _REQUIRED_COLUMNS) is None
    with closing(sync_open_readonly(str(path))) as db:
        assert validate_schema_definition(db, str(path))
        assert _state(db) == before
    for _ in range(2):
        with closing(sync_open(str(path))) as db:
            assert _state(db) == before
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("stamp", ["99", "not-an-integer"])
def test_version_refusal_sql_boundary_preserves_database(
    tmp_path: Path, stamp: str
) -> None:
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE _medre_schema_meta SET value=?", (stamp,))
    before = path.read_bytes()
    with pytest.raises(StorageInitializationError) as error:
        sync_open(str(path))
    assert "verify a backup" in str(error.value)
    assert "compatible build" in str(error.value)
    assert "explicit reset" in str(error.value)
    assert path.read_bytes() == before


async def test_released_catalog_runtime_reopens_without_reset(tmp_path: Path) -> None:
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before = _state(db)
    readonly = await SQLiteStorage.open_readonly(str(path))
    await readonly.close()
    for _ in range(2):
        storage = SQLiteStorage(str(path))
        try:
            await storage.initialize()
        finally:
            await storage.close()
        with closing(sqlite3.connect(path)) as db:
            assert _state(db) == before


@pytest.mark.parametrize("stamp", ["99", "not-an-integer"])
async def test_readonly_version_refusal_preserves_database(
    tmp_path: Path, stamp: str
) -> None:
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE _medre_schema_meta SET value=?", (stamp,))
    before = path.read_bytes()
    with pytest.raises(StorageInitializationError) as error:
        await SQLiteStorage.open_readonly(str(path))
    assert "verify a backup" in str(error.value)
    assert "compatible build" in str(error.value)
    assert "explicit reset" in str(error.value)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Tests: shared read-only catalog assessment on the released fixture
# ---------------------------------------------------------------------------


def _run_storage_status(path: Path) -> tuple[int, str]:
    """Run ``storage status`` for *path* and return (exit code, stdout)."""
    from medre.cli.main import main

    stdout = io.StringIO()
    code = 0
    try:
        with redirect_stdout(stdout):
            main(["storage", "status", "--storage-path", str(path)])
    except SystemExit as exit_error:
        code = exit_error.code if isinstance(exit_error.code, int) else 1
    assert "Traceback" not in stdout.getvalue()
    return code, stdout.getvalue()


def test_released_catalog_assessment_is_current_and_stable(tmp_path: Path) -> None:
    """The populated released fixture classifies current, repeatedly."""
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before = _state(db)
    for _ in range(2):
        assessment = assess_storage_catalog(str(path))
        assert assessment.classification is CatalogCompatibility.CURRENT
        assert assessment.stored_version == "1"
        assert assessment.detail is None
    with closing(sqlite3.connect(path)) as db:
        assert _state(db) == before


def test_missing_required_index_is_unsupported_for_status_and_startup(
    tmp_path: Path,
) -> None:
    """A dropped required index is refused by status and startup alike.

    Regression for the reviewed operator-facing defect: the status command
    once printed an unqualified healthy verdict for this database while
    startup rejected it. The exact assessment and the CLI now agree, and
    the original database bytes are preserved throughout.
    """
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP INDEX idx_outbox_active_adapter")
    before = path.read_bytes()

    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.stored_version == "1"
    assert assessment.detail is not None
    assert "idx_outbox_active_adapter" in assessment.detail

    with closing(sync_open_readonly(str(path))) as db:
        assert safe_additive_state(db) is False
        with pytest.raises(PreReleaseSchemaConstraintMismatchError) as error:
            validate_schema_definition(db, str(path))
        assert "idx_outbox_active_adapter" in str(error.value)

    code, stdout = _run_storage_status(path)
    assert code == 0
    assert "Catalog: unsupported" in stdout
    assert "Status: UNSUPPORTED" in stdout
    assert "HEALTHY" not in stdout
    assert path.read_bytes() == before


def test_assessment_preserves_stopped_fixture_files(tmp_path: Path) -> None:
    """Assessing a stopped fixture writes no file, row, or catalog change."""
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before_state = _state(db)
    before_bytes = path.read_bytes()
    before_listing = sorted(entry.name for entry in tmp_path.iterdir())

    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.CURRENT
    code, stdout = _run_storage_status(path)
    assert code == 0
    assert "Status: HEALTHY" in stdout

    assert path.read_bytes() == before_bytes
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_listing
    with closing(sqlite3.connect(path)) as db:
        assert _state(db) == before_state
