"""Exact prerelease schema identity derived from the authoritative SQLite DDL."""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    PreReleaseSchemaMismatchError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.schema import (
    _COLLECTION_LOG_DDL,
    _DIAGNOSTICS_DDL,
    _DIAGNOSTICS_INDEX_DDL,
    _EXPECTED_SCHEMA_VERSION,
    _INDEXES,
    _REQUIRED_COLUMNS,
    _REQUIRED_FOREIGN_KEYS,
    _REQUIRED_UNIQUE_CONSTRAINTS,
    _RETENTION_EPOCHS_DDL,
    _ROUTING_FACTS_DDL,
    _SCHEMA,
    _SNAPSHOT_OWNER_INDEX_DDL,
    _SNAPSHOTS_DDL,
)


def _definitions(db: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): str(row[2])
        for row in db.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
        )
    }


@lru_cache(maxsize=1)
def _expected_definitions() -> dict[tuple[str, str], str]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA + _INDEXES)
        return _definitions(reference)
    finally:
        reference.close()


@lru_cache(maxsize=1)
def _known_grant_table_forms() -> tuple[str, str]:
    """Exact old and ALTER-generated spellings of one safe nullable addition.

    The old form is derived from the authoritative DDL, not a permissive
    set-of-columns approximation. Constraints must match byte for byte.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(
            _SCHEMA.replace("    expired_at TEXT,\n", "") + _INDEXES
        )
        old = _definitions(reference)[("table", "event_content_access_grants")]
        reference.execute(
            "ALTER TABLE event_content_access_grants ADD COLUMN expired_at TEXT"
        )
        altered = _definitions(reference)[("table", "event_content_access_grants")]
        return old, altered
    finally:
        reference.close()


#: Catalog objects an approved additive predecessor may lack entirely.  They
#: are created atomically by :func:`apply_safe_additions` from the same
#: authoritative DDL; every other object must already match byte for byte.
_ADDITIVE_OBJECTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("table", "event_content_collection_log"),
        ("table", "content_retention_epochs"),
        ("table", "delivery_evidence_diagnostics"),
        ("table", "event_routing_facts"),
        ("table", "delivery_outbox_snapshots"),
        ("index", "idx_diagnostics_identity"),
        ("index", "idx_outbox_snapshot_owner_unique"),
    }
)


def safe_additive_state(db: sqlite3.Connection) -> bool:
    """Accept only the exact old grant table and allowlisted missing objects.

    Every unrelated table, index, view and trigger must already match. Called
    read-only at preflight and again after acquiring BEGIN IMMEDIATE.
    """
    actual = _definitions(db)
    expected = _expected_definitions()
    if ("table", "_medre_schema_meta") not in actual:
        return False
    version = db.execute(
        "SELECT value FROM _medre_schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version is None or str(version[0]) != str(_EXPECTED_SCHEMA_VERSION):
        return False
    if actual.keys() - expected.keys():
        return False
    missing = expected.keys() - actual.keys()
    if missing - _ADDITIVE_OBJECTS:
        return False
    old, altered = _known_grant_table_forms()
    for key, definition in actual.items():
        if key == ("table", "event_content_access_grants"):
            if definition not in {
                expected[key],
                old,
                altered,
            }:
                return False
        elif definition != expected[key]:
            return False
    return True


def apply_safe_additions(db: sqlite3.Connection) -> None:
    """Apply allowlisted additive DDL inside the caller's IMMEDIATE transaction."""
    if not safe_additive_state(db):
        raise ValueError("Schema is not an approved additive predecessor")
    current = _definitions(db)
    if ("table", "event_content_collection_log") not in current:
        execute_ddl(db, _COLLECTION_LOG_DDL)
    if ("table", "content_retention_epochs") not in current:
        execute_ddl(db, _RETENTION_EPOCHS_DDL)
    if ("table", "delivery_evidence_diagnostics") not in current:
        execute_ddl(db, _DIAGNOSTICS_DDL)
    if ("table", "event_routing_facts") not in current:
        execute_ddl(db, _ROUTING_FACTS_DDL)
    if ("table", "delivery_outbox_snapshots") not in current:
        execute_ddl(db, _SNAPSHOTS_DDL)
    if ("index", "idx_diagnostics_identity") not in current:
        execute_ddl(db, _DIAGNOSTICS_INDEX_DDL)
    if ("index", "idx_outbox_snapshot_owner_unique") not in current:
        execute_ddl(db, _SNAPSHOT_OWNER_INDEX_DDL)
    if (
        current[("table", "event_content_access_grants")]
        == _known_grant_table_forms()[0]
    ):
        db.execute("ALTER TABLE event_content_access_grants ADD COLUMN expired_at TEXT")


def validate_schema_definition(db: sqlite3.Connection, path: str) -> bool:
    """Validate without writes; return False only for an empty database.

    SQLite's own catalog representation is the fingerprint. Comparing the full
    definitions covers defaults, types, nullability, collations, foreign-key
    actions, CHECK/UNIQUE constraints, expression/partial indexes, views and
    triggers. Semantically equivalent hand-written DDL is deliberately outside
    the prerelease contract; no independent fingerprint inventory can drift.
    """
    actual = _definitions(db)
    # SQLite writes an ALTER-added nullable column at the end of the stored
    # CREATE TABLE text. That exact known variant has the same constraints.
    grant_key = ("table", "event_content_access_grants")
    if actual.get(grant_key) == _known_grant_table_forms()[1]:
        actual[grant_key] = _expected_definitions()[grant_key]
    if not actual:
        return False
    if ("table", "_medre_schema_meta") not in actual:
        raise PreReleaseSchemaMismatchError(
            path,
            "_medre_schema_meta",
            detail="unstamped database; missing schema_version metadata",
        )
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_xinfo({table})")}
        missing = sorted(required - columns)
        extra = sorted(columns - required)
        if missing or extra:
            raise PreReleaseSchemaMismatchError(
                path,
                table,
                missing,
                unexpected_columns=extra,
            )
    version = db.execute(
        "SELECT value FROM _medre_schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version is None:
        raise PreReleaseSchemaMismatchError(
            path, "_medre_schema_meta", detail="schema_version stamp is missing"
        )
    try:
        stored = int(version[0])
    except (TypeError, ValueError):
        raise StorageInitializationError(
            f"Storage schema version is not an integer: {version[0]!r}. "
            f"Expected {_EXPECTED_SCHEMA_VERSION}. Recreate prerelease state."
        ) from None
    if stored != _EXPECTED_SCHEMA_VERSION:
        raise StorageInitializationError(
            f"Storage schema version mismatch: database has version {stored}, "
            f"but this version of medre expects version {_EXPECTED_SCHEMA_VERSION}. "
            "Resolve the mismatch manually; no automatic schema transformation "
            "or silent reset is performed."
        )
    expected = _expected_definitions()
    # Inspect table constraints before missing indexes so diagnostics identify
    # the owning invariant even when an incompatible fixture has no indexes.
    for key, definition in expected.items():
        if key[0] != "table" or actual.get(key) == definition:
            continue
        table = key[1]
        unique_keys = set()
        for index in db.execute(
            'SELECT name, "unique" FROM pragma_index_list(?)', (table,)
        ):
            if index[1] == 1:
                unique_keys.add(
                    tuple(
                        str(row[0])
                        for row in db.execute(
                            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                            (index[0],),
                        )
                    )
                )
        missing_unique = (
            _REQUIRED_UNIQUE_CONSTRAINTS.get(table, frozenset()) - unique_keys
        )
        labels = [
            "UNIQUE(" + ", ".join(columns) + ")" for columns in sorted(missing_unique)
        ]
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in db.execute(f"PRAGMA foreign_key_list({table})")
        }
        labels.extend(
            f"{source} -> {target_table}.{target_column}"
            for source, target_table, target_column in sorted(
                _REQUIRED_FOREIGN_KEYS.get(table, frozenset()) - foreign_keys
            )
        )
        raise PreReleaseSchemaConstraintMismatchError(
            path, table, labels or ["declared SQL definition"]
        )
    for kind, name in sorted(actual.keys() | expected.keys()):
        if actual.get((kind, name)) != expected.get((kind, name)):
            raise PreReleaseSchemaConstraintMismatchError(
                path, name, [f"declared {kind} definition"]
            )
    return True


def execute_ddl(db: sqlite3.Connection, script: str) -> None:
    """Execute complete DDL statements without executescript's implicit COMMIT."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        db.execute(statement)
