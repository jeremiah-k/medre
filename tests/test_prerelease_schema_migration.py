"""Additive pre-release schema migration: known old shapes open again.

Builds databases at the exact ``add93ab8af``-era shape — the stored decision
table is asserted byte-identical to the verbatim ``CREATE`` statement
extracted from ``git show add93ab8af:src/medre/core/storage/sqlite/schema.py``
— and verifies that opening them applies the allowlisted additive layout
migrations in one transaction while preserving rows and foreign-key
integrity.  Mismatches outside the additive policy must still fail closed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    StorageInitializationError,
)
import medre.core.storage.sqlite._prerelease_migrate as _prerelease_migrate
from medre.core.storage.sqlite._prerelease_migrate import (
    apply_prerelease_migrations,
    known_decision_table_forms,
)
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA
from medre.core.storage.sqlite.schema_validation import safe_additive_state
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CURRENT_ACTION_CHECK = (
    "    CHECK (action IN ('cancel_unstarted', 'resolve_uncertain',"
    " 'resume_scheduled')),\n"
)
_LEGACY_ACTION_CHECK = "    CHECK (action = 'cancel_unstarted'),\n"

#: Verbatim predecessor ``CREATE TABLE`` extracted from
#: ``git show add93ab8af:src/medre/core/storage/sqlite/schema.py``.
_GIT_SHOW_OLD_DECISION_DDL = """CREATE TABLE IF NOT EXISTS operator_delivery_decisions (
    decision_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    observed_generation INTEGER NOT NULL,
    observed_updated_at TEXT NOT NULL,
    observed_status TEXT NOT NULL,
    action TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (action = 'cancel_unstarted'),
    CHECK (observed_status IN ('pending', 'retry_wait')),
    CHECK (observed_generation >= 0)
);"""


def _create_old_shape_db(path: Path) -> Path:
    """Create a stamped database at the exact add93ab8af-era shape.

    The decision table carries the predecessor action CHECK and the
    deadline-cleanup index (which did not exist in that era) is dropped.
    The fixture's stored decision definition is asserted byte-identical to
    the stored form of the verbatim git-show ``CREATE`` statement, so the
    fixture is the true predecessor layout rather than an approximation.
    """
    raw = sqlite3.connect(path)
    try:
        assert _CURRENT_ACTION_CHECK in _SCHEMA
        raw.executescript(
            _SCHEMA.replace(_CURRENT_ACTION_CHECK, _LEGACY_ACTION_CHECK) + _INDEXES
        )
        raw.execute("DROP INDEX IF EXISTS idx_outbox_deadline_cleanup_order")
        raw.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute(_GIT_SHOW_OLD_DECISION_DDL)
            git_show_sql = probe.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name='operator_delivery_decisions'"
            ).fetchone()
        finally:
            probe.close()
        fixture_sql = raw.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='operator_delivery_decisions'"
        ).fetchone()
        assert git_show_sql is not None and fixture_sql is not None
        assert fixture_sql[0] == git_show_sql[0]
        raw.commit()
    finally:
        raw.close()
    return path


def _populate_delivery_history(db_path: Path, *, decisions: int) -> str:
    """Insert one event, its outbox generation, and cancel_unstarted decisions.

    Returns the canonical event ID shared by every inserted row.
    """
    event_id = "event-migration-0001"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES (?, 'radio_message', 1, '2026-01-01T00:00:00+00:00',"
            " 'test-adapter', 'native-1', '2026-01-01T00:00:00+00:00')",
            (event_id,),
        )
        raw.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, created_at, updated_at)"
            " VALUES ('outbox-migration-0001', ?, 'plan-1', 'test-adapter',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (event_id,),
        )
        for index in range(decisions):
            raw.execute(
                "INSERT INTO operator_delivery_decisions (decision_id, actor_id,"
                " event_id, delivery_plan_id, target_adapter, target_channel,"
                " outbox_id, observed_generation, observed_updated_at,"
                " observed_status, action, recorded_at)"
                " VALUES (?, 'operator', ?, 'plan-1', 'test-adapter', NULL,"
                " 'outbox-migration-0001', 0, '2026-01-01T00:00:00+00:00',"
                " 'pending', 'cancel_unstarted',"
                " '2026-01-01T00:00:00+00:00')",
                (f"decision-{index:04d}", event_id),
            )
        raw.commit()
    finally:
        raw.close()
    return event_id


def _catalog(db_path: Path) -> dict[tuple[str, str], str]:
    """Return the persisted catalog as ``(kind, name) -> stored sql``."""
    raw = sqlite3.connect(db_path)
    try:
        return {
            (str(row[0]), str(row[1])): str(row[2])
            for row in raw.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            )
        }
    finally:
        raw.close()


def _table_counts(db_path: Path) -> dict[str, int]:
    """Return one row count per catalog table."""
    raw = sqlite3.connect(db_path)
    try:
        names = [
            str(row[0])
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT GLOB 'sqlite_*'"
            )
        ]
        return {
            name: int(raw.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in names
        }
    finally:
        raw.close()


def _expected_catalog() -> dict[tuple[str, str], str]:
    """Return the authoritative catalog from the declared DDL."""
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(_SCHEMA + _INDEXES)
        return {
            (str(row[0]), str(row[1])): str(row[2])
            for row in probe.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            )
        }
    finally:
        probe.close()


def _assert_foreign_keys_clean(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Tests: predecessor derivation
# ---------------------------------------------------------------------------


def test_derived_legacy_form_matches_git_show_ddl() -> None:
    """The migrated-from predecessor is the exact add93ab8af decision table."""
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute(_GIT_SHOW_OLD_DECISION_DDL)
        stored = probe.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()
    finally:
        probe.close()
    assert stored is not None
    forms = known_decision_table_forms()
    assert forms.legacy == stored[0]
    assert forms.legacy != forms.current
    assert "cancel_unstarted" in forms.legacy
    assert "resolve_uncertain" not in forms.legacy
    assert "resolve_uncertain" in forms.current


# ---------------------------------------------------------------------------
# Tests: migration opens old-shape databases
# ---------------------------------------------------------------------------


async def test_populated_old_shape_database_migrates_and_reads_decisions(
    tmp_path: Path,
) -> None:
    """A populated old-shape DB opens, migrates, and keeps its evidence."""
    db_path = _create_old_shape_db(tmp_path / "populated.sqlite")
    event_id = _populate_delivery_history(db_path, decisions=2)
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
        rows = await storage.list_operator_decisions(event_id)
        assert [row["action"] for row in rows] == [
            "cancel_unstarted",
            "cancel_unstarted",
        ]
        assert [row["decision_id"] for row in rows] == [
            "decision-0000",
            "decision-0001",
        ]
    finally:
        await storage.close()
    assert (
        _catalog(db_path)[("table", "operator_delivery_decisions")]
        == known_decision_table_forms().current
    )
    _assert_foreign_keys_clean(db_path)


async def test_empty_old_shape_database_migrates(tmp_path: Path) -> None:
    """An empty old-shape database migrates without any rows to preserve."""
    db_path = _create_old_shape_db(tmp_path / "empty.sqlite")
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
        assert await storage.count_events() == 0
    finally:
        await storage.close()
    assert ("index", "idx_outbox_deadline_cleanup_order") in _catalog(db_path)


async def test_row_counts_and_foreign_keys_survive_the_rebuild(
    tmp_path: Path,
) -> None:
    """Every table keeps its exact row count; foreign-key check stays clean."""
    db_path = _create_old_shape_db(tmp_path / "counts.sqlite")
    _populate_delivery_history(db_path, decisions=5)
    before = _table_counts(db_path)
    assert before["operator_delivery_decisions"] == 5
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    assert _table_counts(db_path) == before
    _assert_foreign_keys_clean(db_path)


# ---------------------------------------------------------------------------
# Tests: idempotence
# ---------------------------------------------------------------------------


async def test_reopen_after_migration_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    """A second open detects nothing to migrate and rewrites nothing."""
    db_path = _create_old_shape_db(tmp_path / "reopen.sqlite")
    event_id = _populate_delivery_history(db_path, decisions=1)
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    migrated_catalog = _catalog(db_path)
    migrated_counts = _table_counts(db_path)

    calls: list[str] = []
    real_rebuild = _prerelease_migrate.rebuild_operator_delivery_decisions

    def spy_apply(db: sqlite3.Connection) -> None:
        calls.append("rebuild")
        real_rebuild(db)

    monkeypatch.setattr(
        _prerelease_migrate,
        "_MIGRATION_STEPS",
        (
            _prerelease_migrate._MigrationStep(
                name="spy-widen-operator-decision-action-vocabulary",
                applies=_prerelease_migrate.operator_decision_rebuild_applies,
                apply=spy_apply,
            ),
        ),
    )
    reopened = SQLiteStorage(db_path=str(db_path))
    try:
        await reopened.initialize()
        rows = await reopened.list_operator_decisions(event_id)
        assert [row["decision_id"] for row in rows] == ["decision-0000"]
    finally:
        await reopened.close()
    assert calls == []
    assert _catalog(db_path) == migrated_catalog
    assert _table_counts(db_path) == migrated_counts

    # Direct idempotence: applying the steps again rewrites nothing.
    raw = sqlite3.connect(db_path)
    try:
        assert safe_additive_state(raw) is True
        apply_prerelease_migrations(raw)
        apply_prerelease_migrations(raw)
    finally:
        raw.close()
    assert _catalog(db_path) == migrated_catalog


async def test_current_shape_database_reopens_unchanged(tmp_path: Path) -> None:
    """A database already at the current shape never enters a migration."""
    db_path = tmp_path / "current.sqlite"
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    catalog = _catalog(db_path)
    counts = _table_counts(db_path)
    reopened = SQLiteStorage(db_path=str(db_path))
    try:
        await reopened.initialize()
    finally:
        await reopened.close()
    assert _catalog(db_path) == catalog
    assert _table_counts(db_path) == counts


# ---------------------------------------------------------------------------
# Tests: fail closed outside the additive policy
# ---------------------------------------------------------------------------


async def test_non_additive_narrowing_still_fails_closed(tmp_path: Path) -> None:
    """A narrowed CHECK matches no predecessor; the database is refused."""
    db_path = tmp_path / "narrowed.sqlite"
    narrowed_status_check = "    CHECK (observed_status IN ('pending')),\n"
    current_status_check = "    CHECK (observed_status IN ('pending', 'retry_wait')),\n"
    raw = sqlite3.connect(db_path)
    try:
        assert _CURRENT_ACTION_CHECK in _SCHEMA
        assert current_status_check in _SCHEMA
        ddl = _SCHEMA.replace(_CURRENT_ACTION_CHECK, _LEGACY_ACTION_CHECK)
        ddl = ddl.replace(current_status_check, narrowed_status_check)
        raw.executescript(ddl + _INDEXES)
        raw.execute("DROP INDEX IF EXISTS idx_outbox_deadline_cleanup_order")
        raw.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        raw.commit()
    finally:
        raw.close()
    original_bytes = db_path.read_bytes()
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        with pytest.raises(PreReleaseSchemaConstraintMismatchError) as exc_info:
            await storage.initialize()
    finally:
        await storage.close()
    message = str(exc_info.value)
    assert "recreate" in message
    assert "additive" in message
    assert "does not automatically transform" not in message
    assert db_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Tests: drift-window objects exist after migration
# ---------------------------------------------------------------------------


async def test_drift_window_objects_exist_after_migration(tmp_path: Path) -> None:
    """The new index is installed and the widened vocabulary is live."""
    db_path = _create_old_shape_db(tmp_path / "objects.sqlite")
    event_id = _populate_delivery_history(db_path, decisions=1)
    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    expected = _expected_catalog()
    catalog = _catalog(db_path)
    assert (
        catalog[("index", "idx_outbox_deadline_cleanup_order")]
        == expected[("index", "idx_outbox_deadline_cleanup_order")]
    )
    assert "WHERE status IN ('pending', 'retry_wait')" in (
        catalog[("index", "idx_outbox_deadline_cleanup_order")]
    )
    assert (
        catalog[("table", "operator_delivery_decisions")]
        == expected[("table", "operator_delivery_decisions")]
    )
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "INSERT INTO operator_delivery_decisions (decision_id, actor_id,"
            " event_id, delivery_plan_id, target_adapter, target_channel,"
            " outbox_id, observed_generation, observed_updated_at,"
            " observed_status, action, recorded_at)"
            " VALUES ('decision-new', 'operator', ?, 'plan-1', 'test-adapter',"
            " NULL, 'outbox-migration-0001', 0, '2026-01-01T00:00:00+00:00',"
            " 'pending', 'resolve_uncertain', '2026-01-02T00:00:00+00:00')",
            (event_id,),
        )
        raw.commit()
        actions = [
            str(row[0])
            for row in raw.execute(
                "SELECT action FROM operator_delivery_decisions ORDER BY decision_id"
            )
        ]
    finally:
        raw.close()
    assert actions == ["cancel_unstarted", "resolve_uncertain"]
    _assert_foreign_keys_clean(db_path)


async def test_readonly_known_additive_layout_requires_writable_migration(
    tmp_path: Path,
) -> None:
    """Inspection refuses a legacy layout without suggesting evidence deletion."""
    db_path = _create_old_shape_db(tmp_path / "readonly-legacy.sqlite")
    event_id = _populate_delivery_history(db_path, decisions=1)
    before = _catalog(db_path)
    with pytest.raises(StorageInitializationError) as exc_info:
        await SQLiteStorage.open_readonly(str(db_path))
    message = str(exc_info.value)
    assert "recognized additive" in message
    assert "writable" in message
    assert "instead of resetting" in message
    assert _catalog(db_path) == before

    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
    finally:
        await storage.close()
    reader = await SQLiteStorage.open_readonly(str(db_path))
    try:
        assert len(await reader.list_operator_decisions(event_id)) == 1
    finally:
        await reader.close()


def test_rebuild_scopes_foreign_key_check_to_rebuilt_table(tmp_path: Path) -> None:
    """An unrelated pre-existing orphan is not caused by a local rebuild."""
    db_path = _create_old_shape_db(tmp_path / "unrelated-fk.sqlite")
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, created_at, updated_at)"
            " VALUES ('unrelated-orphan', 'missing-event', 'plan-1',"
            " 'test-adapter', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
        raw.commit()
        assert raw.execute("PRAGMA foreign_key_check").fetchall()
        raw.execute("BEGIN IMMEDIATE")
        _prerelease_migrate.rebuild_operator_delivery_decisions(raw)
        raw.commit()
        assert (
            raw.execute(
                "PRAGMA foreign_key_check(operator_delivery_decisions)"
            ).fetchall()
            == []
        )
        assert raw.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        raw.close()
    assert (
        _catalog(db_path)[("table", "operator_delivery_decisions")]
        == known_decision_table_forms().current
    )


def test_rebuild_foreign_key_failure_rolls_back_legacy_rows(tmp_path: Path) -> None:
    """An orphan in the rebuilt table leaves the old catalog and rows intact."""
    db_path = _create_old_shape_db(tmp_path / "decision-orphan.sqlite")
    _populate_delivery_history(db_path, decisions=1)
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "UPDATE operator_delivery_decisions SET event_id='missing-event' "
            "WHERE decision_id='decision-0000'"
        )
        raw.commit()
        before_sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()[0]
        before_rows = raw.execute(
            "SELECT * FROM operator_delivery_decisions"
        ).fetchall()
        raw.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageInitializationError, match="foreign-key violation"):
            _prerelease_migrate.rebuild_operator_delivery_decisions(raw)
        raw.rollback()
        after_sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()[0]
        assert after_sql == before_sql
        assert (
            raw.execute("SELECT * FROM operator_delivery_decisions").fetchall()
            == before_rows
        )
        assert (
            raw.execute(
                "SELECT name FROM sqlite_master WHERE name=?",
                (_prerelease_migrate._LEGACY_DECISION_TABLE,),
            ).fetchone()
            is None
        )
    finally:
        raw.close()
