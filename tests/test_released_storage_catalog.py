"""Open a populated released catalog, independently of the current DDL.

The SQL fixture is the verbatim concatenation of _SCHEMA and _INDEXES from
0.1.0 (fd2927d1a518c792f85bf8c31c297dbe6e7e7cb8). Releases through 0.2.1
have the same catalog and version stamp. The 0.2.2 release adds the handoff
settlement table. Do not regenerate the fixture from current code:
future migrations must continue to accept this released baseline. When a
new catalog is introduced, update the expected post-migration state, not
the fixture.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import medre.core.storage.sqlite.schema_validation as schema_validation
from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    PreReleaseSchemaMismatchError,
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
    _expected_definitions,
    safe_additive_state,
    validate_schema_definition,
)

_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"
_NOW = "2026-11-14T00:00:00+00:00"
_HANDOFF_SETTLEMENTS = "delivery_handoff_settlements"
_CEILING_ADDITIONS = (
    ("table", "event_content_ceiling_log"),
    ("table", "delivery_handoff_uncertainty"),
    ("table", "radio_accepted_part_facts"),
    ("table", "delivery_observation_scopes"),
    ("table", "delivery_observation_scope_versions"),
    ("trigger", "trg_scope_version_insert"),
    ("trigger", "trg_scope_version_update"),
    ("trigger", "trg_scope_version_delete"),
    ("table", "radio_attempt_evidence_summaries"),
    ("index", "idx_ceiling_enforcement_order"),
    ("index", "idx_attachment_associations_content_ref"),
)
_RELEASED_010_ADDITIONS = (("table", _HANDOFF_SETTLEMENTS), *_CEILING_ADDITIONS)
# SQLite catalog from tag 0.2.2 (dd2b50899c7bc1f6fff2abe7594d8ea3dc7809d5).
_RELEASED_022_CATALOG_SHA256 = (
    "4b46e00fa039ab1a5a5108a5dd61bbd5a34435fb4130f466a067191e924eb280"
)
_HANDOFF_SETTLEMENT_SCHEMA = (
    _FIXTURE.parents[2] / "docs" / "schemas" / "handoff-settlements.schema.sql"
)


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


def _expected_upgraded_state(
    original: tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]],
    added_catalog_objects: tuple[tuple[str, str], ...] = _RELEASED_010_ADDITIONS,
) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    catalog, rows = original
    original_objects = {(kind, name) for kind, name, _sql in catalog}
    assert original_objects.isdisjoint(added_catalog_objects)
    entries = [
        (kind, name, _expected_definitions()[(kind, name)])
        for kind, name in added_catalog_objects
    ]
    return sorted([*catalog, *entries]), {
        **rows,
        **{name: [] for kind, name in added_catalog_objects if kind == "table"},
    }


def test_released_022_ceiling_upgrade_preserves_handoff_settlements(
    tmp_path: Path,
) -> None:
    path = tmp_path / "released-022.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_HANDOFF_SETTLEMENT_SCHEMA.read_text(encoding="utf-8"))
        with db:
            db.execute("INSERT INTO delivery_handoff_settlements VALUES ('work', 1, 1)")
        before = _state(db)
    assert (
        hashlib.sha256(json.dumps(before[0]).encode()).hexdigest()
        == _RELEASED_022_CATALOG_SHA256
    )
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
    assert assessment.stored_version == "1"
    expected = _expected_upgraded_state(before, _CEILING_ADDITIONS)
    for _ in range(2):
        with closing(sync_open(str(path))) as db:
            assert _state(db) == expected
            assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_released_upgrade_rolls_back_if_ceiling_index_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "released.sqlite"
    _populate(path)
    before = path.read_bytes()
    with closing(sqlite3.connect(path)) as db:
        expected = _expected_upgraded_state(_state(db))
    execute_ddl = schema_validation.execute_ddl

    def fail_index(db: sqlite3.Connection, ddl: str) -> None:
        if "CREATE INDEX IF NOT EXISTS idx_ceiling_enforcement_order" in ddl:
            assert db.in_transaction
            assert (
                db.execute("SELECT * FROM event_content_ceiling_log").fetchall() == []
            )
            raise sqlite3.OperationalError("injected ceiling index failure")
        execute_ddl(db, ddl)

    with monkeypatch.context() as patch:
        patch.setattr(schema_validation, "execute_ddl", fail_index)
        with pytest.raises(sqlite3.OperationalError, match="injected ceiling index"):
            sync_open(str(path))
    assert path.read_bytes() == before
    with closing(sync_open(str(path))) as db:
        assert _state(db) == expected
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_released_catalog_sql_boundary_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before = _state(db)
    assert {"attachment_blobs", "event_attachment_associations"} <= before[1].keys()
    assert sync_find_schema_shape_mismatch(str(path), _REQUIRED_COLUMNS) is None
    with closing(sync_open_readonly(str(path))) as db:
        assert safe_additive_state(db)
        # The read-only validation names the first missing additive object in
        # the required-column walk; the released predecessor lacks both the
        # handoff settlement and the ceiling receipt tables.
        with pytest.raises(
            PreReleaseSchemaMismatchError,
            match="event_content_ceiling_log|delivery_handoff_settlements",
        ):
            validate_schema_definition(db, str(path))
        assert _state(db) == before
    expected = _expected_upgraded_state(before)
    for _ in range(2):
        with closing(sync_open(str(path))) as db:
            assert validate_schema_definition(db, str(path))
            assert _state(db) == expected
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
    before_bytes = path.read_bytes()
    with pytest.raises(StorageInitializationError, match="recognized additive"):
        await SQLiteStorage.open_readonly(str(path))
    assert path.read_bytes() == before_bytes
    expected = _expected_upgraded_state(before)
    for _ in range(2):
        storage = SQLiteStorage(str(path))
        try:
            await storage.initialize()
        finally:
            await storage.close()
        with closing(sqlite3.connect(path)) as db:
            assert _state(db) == expected
    after_bytes = path.read_bytes()
    readonly = await SQLiteStorage.open_readonly(str(path))
    await readonly.close()
    assert path.read_bytes() == after_bytes


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


def test_released_catalog_assessment_reports_upgrade_then_current(
    tmp_path: Path,
) -> None:
    """Read-only assessment recognizes the precursor to the writable update."""
    path = tmp_path / "released.sqlite"
    _populate(path)
    with closing(sqlite3.connect(path)) as db:
        before = _state(db)
    for _ in range(2):
        assessment = assess_storage_catalog(str(path))
        assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
        assert assessment.stored_version == "1"
        assert assessment.detail == (
            "missing objects: index idx_attachment_associations_content_ref, "
            "index idx_ceiling_enforcement_order, "
            "table delivery_handoff_settlements, table delivery_handoff_uncertainty, "
            "table delivery_observation_scope_versions, table delivery_observation_scopes, "
            "table event_content_ceiling_log, table radio_accepted_part_facts, "
            "table radio_attempt_evidence_summaries, trigger trg_scope_version_delete, "
            "trigger trg_scope_version_insert, trigger trg_scope_version_update"
        )
    with closing(sqlite3.connect(path)) as db:
        assert _state(db) == before
    with closing(sync_open(str(path))) as db:
        assert _state(db) == _expected_upgraded_state(before)
    upgraded = assess_storage_catalog(str(path))
    assert upgraded.classification is CatalogCompatibility.CURRENT
    assert upgraded.stored_version == "1"
    assert upgraded.detail is None


@pytest.mark.parametrize("upgrade_first", [False, True])
def test_missing_required_index_is_unsupported_for_status_and_startup(
    tmp_path: Path, upgrade_first: bool
) -> None:
    """A dropped required index is refused by status and startup alike.

    Regression for the reviewed operator-facing defect: the status command
    once printed an unqualified healthy verdict for this database while
    startup rejected it. The exact assessment and the CLI now agree, and
    the original database bytes are preserved throughout.
    """
    path = tmp_path / "released.sqlite"
    _populate(path)
    if upgrade_first:
        sync_open(str(path)).close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP INDEX idx_outbox_active_adapter")
    before = path.read_bytes()

    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.stored_version == "1"
    assert assessment.detail is not None
    if upgrade_first:
        assert "idx_outbox_active_adapter" in assessment.detail

    with closing(sync_open_readonly(str(path))) as db:
        assert safe_additive_state(db) is False
    with pytest.raises(StorageInitializationError) as error:
        sync_open(str(path))
    if upgrade_first:
        assert isinstance(error.value, PreReleaseSchemaConstraintMismatchError)
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
    assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
    code, stdout = _run_storage_status(path)
    assert code == 0
    assert "Catalog: supported_upgrade" in stdout
    assert "Status: SUPPORTED UPGRADE" in stdout

    assert path.read_bytes() == before_bytes
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_listing
    with closing(sqlite3.connect(path)) as db:
        assert _state(db) == before_state
