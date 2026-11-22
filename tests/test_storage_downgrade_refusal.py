"""Automatic-downgrade refusal: a newer catalog is never silently downgraded.

A database whose catalog declares objects this build does not declare may
hold a NEWER catalog written by a newer build of medre. Every open path —
the writable startup migration path, the read-only inspection open, the
read-only startup preflight, and the shared catalog assessment — must fail
closed with an explicit refusal naming both catalog states. No path may
plan, tolerate, or execute a schema downgrade, and a refused open must
leave the database bytes untouched.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.backend import (
    StorageDowngradeRefusedError,
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
from medre.core.storage.sqlite.schema_validation import validate_schema_definition
from medre.core.storage.sqlite.storage import SQLiteStorage

_NOW = "2026-11-20T00:00:00+00:00"

#: Objects a hypothetical NEWER build adds on top of this build's catalog.
#: Names are chosen so they cannot collide with any declared catalog object.
_NEWER_TABLE_DDL = (
    "CREATE TABLE future_confirmation_dispositions ("
    " event_id TEXT PRIMARY KEY, disposition TEXT NOT NULL)"
)
_NEWER_INDEX_DDL = (
    "CREATE INDEX idx_future_confirmation_dispositions_disposition "
    "ON future_confirmation_dispositions(disposition)"
)


def _populate_current(path: Path) -> None:
    """Create an exact current catalog with a small populated delivery state."""
    with closing(sync_open(str(path))) as db:
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('event', 'message.text', 1, ?, 'radio', 'radio', ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, created_at, updated_at)"
            " VALUES ('work', 'event', 'plan', 'radio', ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id,"
            " delivery_plan_id, target_adapter, status, receipt_kind, created_at)"
            " VALUES ('receipt', 'event', 'plan', 'radio', 'failed', 'attempt', ?)",
            (_NOW,),
        )
        db.commit()


def _simulate_newer_catalog(path: Path, *, with_index: bool = False) -> None:
    """Add objects this build does not declare, leaving the stamp at 1."""
    with closing(sqlite3.connect(path)) as db:
        db.execute(_NEWER_TABLE_DDL)
        if with_index:
            db.execute(_NEWER_INDEX_DDL)
        db.commit()


def test_writable_open_refuses_newer_catalog_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "newer.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    before = path.read_bytes()
    with pytest.raises(StorageDowngradeRefusedError) as excinfo:
        with closing(sync_open(str(path))):
            pass
    message = str(excinfo.value)
    assert "downgrade refused" in message
    assert "no automatic schema downgrade is performed" in message
    # Both catalog states are named: the stored object inventory (which
    # includes the undeclared object) and this build's declared inventory.
    assert "future_confirmation_dispositions" in message
    assert "schema version 1" in message
    assert excinfo.value.expected_version == 1
    assert excinfo.value.unexpected == ["table future_confirmation_dispositions"]
    assert path.read_bytes() == before


def test_writable_open_refuses_newer_catalog_with_extra_index_only(
    tmp_path: Path,
) -> None:
    """A newer build may add only an index; the refusal is equally explicit."""
    path = tmp_path / "newer-index.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path, with_index=True)
    before = path.read_bytes()
    with pytest.raises(StorageDowngradeRefusedError) as excinfo:
        with closing(sync_open(str(path))):
            pass
    assert "idx_future_confirmation_dispositions_disposition" in str(excinfo.value)
    assert path.read_bytes() == before


def test_startup_preflight_refuses_newer_catalog(tmp_path: Path) -> None:
    """The operator-visible startup preflight returns the explicit refusal.

    ``SQLiteStorage.initialize`` raises whatever the preflight returns, so
    the returned object is the operator-facing refusal on the normal path.
    """
    path = tmp_path / "startup.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    mismatch = sync_find_schema_shape_mismatch(str(path), _REQUIRED_COLUMNS)
    assert isinstance(mismatch, StorageDowngradeRefusedError)
    assert "downgrade refused" in str(mismatch)
    assert "future_confirmation_dispositions" in str(mismatch)


async def test_runtime_initialize_refuses_newer_catalog(tmp_path: Path) -> None:
    """SQLiteStorage.initialize surfaces the explicit refusal to the runtime."""
    path = tmp_path / "runtime.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    storage = SQLiteStorage(db_path=str(path))
    with pytest.raises(StorageDowngradeRefusedError):
        await storage.initialize()
    await storage.close()


def test_readonly_open_refuses_newer_catalog(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    with closing(sync_open_readonly(str(path))) as db:
        with pytest.raises(StorageDowngradeRefusedError):
            validate_schema_definition(db, str(path))


async def test_readonly_verify_path_refuses_newer_catalog(tmp_path: Path) -> None:
    path = tmp_path / "readonly-verify.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    with pytest.raises(StorageDowngradeRefusedError):
        await SQLiteStorage.open_readonly(str(path))


def test_catalog_assessment_reports_newer_catalog_explicitly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "assessment.sqlite"
    _populate_current(path)
    _simulate_newer_catalog(path)
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.UNSUPPORTED
    assert assessment.stored_version == "1"
    assert assessment.detail is not None
    assert "automatic downgrade refused" in assessment.detail
    assert "future_confirmation_dispositions" in assessment.detail


def test_stamp_mismatch_still_refuses_with_established_language(
    tmp_path: Path,
) -> None:
    """A newer stamp keeps its dedicated refusal; it is not relabeled."""
    path = tmp_path / "stamp.sqlite"
    _populate_current(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE _medre_schema_meta SET value='99' WHERE key='schema_version'"
        )
        db.commit()
    with pytest.raises(StorageInitializationError) as excinfo:
        with closing(sync_open(str(path))):
            pass
    message = str(excinfo.value)
    assert "Storage schema version mismatch" in message
    assert "database has version 99" in message
    assert not isinstance(excinfo.value, StorageDowngradeRefusedError)


def test_recognized_predecessor_is_still_supported_upgrade(
    tmp_path: Path,
) -> None:
    """Missing allowlisted objects remain a supported upgrade, not a refusal."""
    path = tmp_path / "predecessor.sqlite"
    ddl = (
        (Path(__file__).parent / "fixtures/released-storage-0.1.0.sql")
        .read_bytes()
        .replace(b"\r\n", b"\n")
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl.decode("utf-8"))
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
    assessment = assess_storage_catalog(str(path))
    assert assessment.classification is CatalogCompatibility.SUPPORTED_UPGRADE
    expected = sync_find_schema_shape_mismatch(str(path), _REQUIRED_COLUMNS)
    assert expected is None
    with closing(sync_open(str(path))) as db:
        assert validate_schema_definition(db, str(path))
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
