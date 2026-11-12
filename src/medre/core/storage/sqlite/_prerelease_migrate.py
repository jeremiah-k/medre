"""Allowlisted additive pre-release schema migration steps.

Each step upgrades one exact, explicitly allowlisted predecessor layout to
the current declared shape.  Detection reuses the same derivation scheme as
the prerelease identity validators: the predecessor definition is produced
from the authoritative DDL in this repository, never hand-written, and must
match the stored SQLite catalog byte for byte.  A database whose stored
layout matches no allowlisted predecessor is never touched here; strict
prerelease validation refuses it with recreation guidance.

Steps run inside the writable open path's existing ``BEGIN IMMEDIATE``
transaction (see :func:`medre.core.storage.sqlite.connection.sync_open`) and
are idempotent: a migrated database no longer matches any predecessor form,
so a second open detects nothing to do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

from medre.core.storage.backend import StorageInitializationError
from medre.core.storage.sqlite.schema import (
    _INDEXES,
    _OPERATOR_DECISIONS_DDL,
    _OPERATOR_DECISIONS_INDEX_DDL,
    _PRESSURE_DDL,
    _PRESSURE_OUTAGE_GAPS_DDL,
    _SCHEMA,
)

_DECISION_TABLE = "operator_delivery_decisions"
#: Temporary name holding the legacy rows between rename and copy.
_LEGACY_DECISION_TABLE = "operator_delivery_decisions__prerelease_legacy"

#: Exact current action-vocabulary CHECK line in the authoritative DDL.
_CURRENT_ACTION_CHECK = (
    "    CHECK (action IN ('cancel_unstarted', 'resolve_uncertain',"
    " 'resume_scheduled')),\n"
)
#: Exact predecessor CHECK line it widened (old values are a strict subset).
_LEGACY_ACTION_CHECK = "    CHECK (action = 'cancel_unstarted'),\n"

_WIDEN_DECISION_ACTIONS_STEP = "widen-operator-decision-action-vocabulary"

_PRESSURE_TABLE = "inbound_pressure_observations"
#: Temporary name holding the legacy rows between rename and copy.
_LEGACY_PRESSURE_TABLE = "inbound_pressure_observations__prerelease_legacy"

#: Exact current outcome-vocabulary CHECK line in the authoritative DDL.
_CURRENT_OUTCOME_CHECK = (
    "    outcome TEXT NOT NULL CHECK (outcome IN ('rejected', 'timed_out',"
    " 'deferred', 'scheduled', 'overflow', 'cancelled_before_admission')),\n"
)
#: Exact predecessor CHECK line it widened (old values are a strict subset).
_LEGACY_OUTCOME_CHECK = (
    "    outcome TEXT NOT NULL CHECK (outcome IN"
    " ('rejected', 'timed_out', 'deferred')),\n"
)

_WIDEN_PRESSURE_OUTCOMES_STEP = "widen-inbound-pressure-outcome-vocabulary"

_GAP_TABLE = "inbound_pressure_outage_gaps"
_LEGACY_GAP_TABLE = "inbound_pressure_outage_gaps__prerelease_legacy"
_CURRENT_GAP_TOKEN_COLUMN = (
    "    gap_token TEXT NOT NULL UNIQUE CHECK (length(gap_token) = 32),\n"
)
_ADD_GAP_RETRY_TOKEN_STEP = "add-inbound-pressure-gap-retry-token"


class _DecisionForms(NamedTuple):
    """Byte-exact legacy and current stored forms of one allowlisted change."""

    legacy: str
    current: str
    columns: tuple[str, ...]


def _stored_table_sql(db: sqlite3.Connection, table: str) -> str | None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def known_decision_current_table_sql(table: str) -> str | None:
    """Return the authoritative current CREATE TABLE text for *table*."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA + _INDEXES)
        return _stored_table_sql(reference, table)
    finally:
        reference.close()


@lru_cache(maxsize=1)
def known_decision_table_forms() -> _DecisionForms:
    """Derive the exact predecessor and current forms of the decision table.

    The legacy form is the authoritative DDL with the single allowlisted
    action-vocabulary widening reverted — the same derivation scheme as the
    grant-table forms — so detection cannot drift from the declared shape.
    Raises :class:`StorageInitializationError` when the authoritative DDL no
    longer contains the expected widening, so a future schema edit fails
    loudly instead of silently treating the current shape as a predecessor.
    """
    if _CURRENT_ACTION_CHECK not in _SCHEMA:
        raise StorageInitializationError(
            "Authoritative DDL no longer contains the expected operator "
            "decision action CHECK; the allowlisted additive predecessor "
            "cannot be derived."
        )
    legacy_db = sqlite3.connect(":memory:")
    try:
        legacy_db.executescript(
            _SCHEMA.replace(_CURRENT_ACTION_CHECK, _LEGACY_ACTION_CHECK) + _INDEXES
        )
        legacy = _stored_table_sql(legacy_db, _DECISION_TABLE)
        columns = tuple(
            str(row[1])
            for row in legacy_db.execute(
                "PRAGMA table_info(operator_delivery_decisions)"
            )
        )
    finally:
        legacy_db.close()
    current_db = sqlite3.connect(":memory:")
    try:
        current_db.executescript(_SCHEMA + _INDEXES)
        current = _stored_table_sql(current_db, _DECISION_TABLE)
    finally:
        current_db.close()
    if legacy is None or current is None or legacy == current or not columns:
        raise StorageInitializationError(
            "The allowlisted legacy operator decision form could not be "
            "derived from the authoritative DDL."
        )
    return _DecisionForms(legacy=legacy, current=current, columns=columns)


def operator_decision_rebuild_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored decision table is exactly the predecessor."""
    return _stored_table_sql(db, _DECISION_TABLE) == (
        known_decision_table_forms().legacy
    )


def rebuild_operator_delivery_decisions(db: sqlite3.Connection) -> None:
    """Widen the decision action CHECK with a guarded table rebuild.

    SQLite cannot ALTER a CHECK constraint, so the widening rewrites the
    table: the legacy rows are renamed aside, the table is recreated from
    the authoritative DDL slice (keeping the stored definition byte-identical
    to the declared catalog), rows are copied with an explicit column list,
    the legacy table is dropped, and its dependent index is recreated from
    the authoritative DDL slice.

    Nothing references ``operator_delivery_decisions`` by foreign key, so
    the rename and drop cannot orphan inbound references; the copy preserves
    the table's own outbound foreign keys to ``canonical_events`` and
    ``delivery_outbox``.

    Raises :class:`StorageInitializationError` when the rebuilt row count
    differs from the original or the scoped
    ``PRAGMA foreign_key_check(operator_delivery_decisions)`` reports a
    violation. The caller rolls back the transaction, leaving the database
    unchanged.
    """
    if not operator_decision_rebuild_applies(db):
        return
    before_row = db.execute(
        "SELECT COUNT(*) FROM operator_delivery_decisions"
    ).fetchone()
    assert before_row is not None  # COUNT(*) always yields one row
    before = int(before_row[0])
    db.execute(f"ALTER TABLE {_DECISION_TABLE} RENAME TO {_LEGACY_DECISION_TABLE}")
    db.execute(_OPERATOR_DECISIONS_DDL)
    columns = ", ".join(known_decision_table_forms().columns)
    db.execute(
        f"INSERT INTO {_DECISION_TABLE} ({columns}) "
        f"SELECT {columns} FROM {_LEGACY_DECISION_TABLE}"
    )
    db.execute(f"DROP TABLE {_LEGACY_DECISION_TABLE}")
    db.execute(_OPERATOR_DECISIONS_INDEX_DDL)
    after_row = db.execute(
        "SELECT COUNT(*) FROM operator_delivery_decisions"
    ).fetchone()
    assert after_row is not None  # COUNT(*) always yields one row
    after = int(after_row[0])
    if after != before:
        raise StorageInitializationError(
            f"Pre-release migration '{_WIDEN_DECISION_ACTIONS_STEP}' "
            f"changed the operator decision row count: {before} before, "
            f"{after} after; the rebuild was rolled back."
        )
    violations = db.execute(f"PRAGMA foreign_key_check({_DECISION_TABLE})").fetchall()
    if violations:
        raise StorageInitializationError(
            f"Pre-release migration '{_WIDEN_DECISION_ACTIONS_STEP}' left "
            f"{len(violations)} foreign-key violation(s); the rebuild was "
            "rolled back."
        )


@lru_cache(maxsize=1)
def known_pressure_table_forms() -> _DecisionForms:
    """Derive the exact predecessor and current forms of the pressure table.

    The legacy form is the authoritative DDL with the single allowlisted
    outcome-vocabulary widening reverted — the same derivation scheme as the
    decision-table forms — so detection cannot drift from the declared shape.
    Raises :class:`StorageInitializationError` when the authoritative DDL no
    longer contains the expected widening, so a future schema edit fails
    loudly instead of silently treating the current shape as a predecessor.
    """
    if _CURRENT_OUTCOME_CHECK not in _SCHEMA:
        raise StorageInitializationError(
            "Authoritative DDL no longer contains the expected inbound "
            "pressure outcome CHECK; the allowlisted additive predecessor "
            "cannot be derived."
        )
    legacy_db = sqlite3.connect(":memory:")
    try:
        legacy_db.executescript(
            _SCHEMA.replace(_CURRENT_OUTCOME_CHECK, _LEGACY_OUTCOME_CHECK) + _INDEXES
        )
        legacy = _stored_table_sql(legacy_db, _PRESSURE_TABLE)
        columns = tuple(
            str(row[1])
            for row in legacy_db.execute(f"PRAGMA table_info({_PRESSURE_TABLE})")
        )
    finally:
        legacy_db.close()
    current_db = sqlite3.connect(":memory:")
    try:
        current_db.executescript(_SCHEMA + _INDEXES)
        current = _stored_table_sql(current_db, _PRESSURE_TABLE)
    finally:
        current_db.close()
    if legacy is None or current is None or legacy == current or not columns:
        raise StorageInitializationError(
            "The allowlisted legacy inbound pressure form could not be "
            "derived from the authoritative DDL."
        )
    return _DecisionForms(legacy=legacy, current=current, columns=columns)


def inbound_pressure_rebuild_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored pressure table is exactly the predecessor."""
    return _stored_table_sql(db, _PRESSURE_TABLE) == (
        known_pressure_table_forms().legacy
    )


def rebuild_inbound_pressure_observations(db: sqlite3.Connection) -> None:
    """Widen the pressure outcome CHECK with a guarded table rebuild.

    SQLite cannot ALTER a CHECK constraint, so the widening rewrites the
    table: the legacy rows are renamed aside, the table is recreated from
    the authoritative DDL slice (keeping the stored definition byte-identical
    to the declared catalog), rows are copied with an explicit column list,
    and the legacy table is dropped.  Nothing references
    ``inbound_pressure_observations`` by foreign key and the table declares
    no dependent index, so the rename and drop cannot orphan catalog objects
    and no index recreation is required.

    Raises :class:`StorageInitializationError` when the rebuilt row count
    differs from the original. The caller rolls back the transaction,
    leaving the database unchanged.
    """
    if not inbound_pressure_rebuild_applies(db):
        return
    before_row = db.execute(f"SELECT COUNT(*) FROM {_PRESSURE_TABLE}").fetchone()
    assert before_row is not None  # COUNT(*) always yields one row
    before = int(before_row[0])
    db.execute(f"ALTER TABLE {_PRESSURE_TABLE} RENAME TO {_LEGACY_PRESSURE_TABLE}")
    db.execute(_PRESSURE_DDL)
    columns = ", ".join(known_pressure_table_forms().columns)
    db.execute(
        f"INSERT INTO {_PRESSURE_TABLE} ({columns}) "
        f"SELECT {columns} FROM {_LEGACY_PRESSURE_TABLE}"
    )
    db.execute(f"DROP TABLE {_LEGACY_PRESSURE_TABLE}")
    after_row = db.execute(f"SELECT COUNT(*) FROM {_PRESSURE_TABLE}").fetchone()
    assert after_row is not None  # COUNT(*) always yields one row
    after = int(after_row[0])
    if after != before:
        raise StorageInitializationError(
            f"Pre-release migration '{_WIDEN_PRESSURE_OUTCOMES_STEP}' "
            f"changed the inbound pressure row count: {before} before, "
            f"{after} after; the rebuild was rolled back."
        )


@lru_cache(maxsize=1)
def known_outage_gap_table_forms() -> _DecisionForms:
    """Derive the exact predecessor lacking the immutable retry token."""
    if _CURRENT_GAP_TOKEN_COLUMN not in _PRESSURE_OUTAGE_GAPS_DDL:
        raise StorageInitializationError(
            "Authoritative outage-gap DDL no longer has the retry-token "
            "column; the prerelease predecessor cannot be derived."
        )
    old_db = sqlite3.connect(":memory:")
    current_db = sqlite3.connect(":memory:")
    try:
        old_db.execute(_PRESSURE_OUTAGE_GAPS_DDL.replace(_CURRENT_GAP_TOKEN_COLUMN, ""))
        current_db.execute(_PRESSURE_OUTAGE_GAPS_DDL)
        legacy = _stored_table_sql(old_db, _GAP_TABLE)
        current = _stored_table_sql(current_db, _GAP_TABLE)
        columns = tuple(
            str(row[1]) for row in old_db.execute(f"PRAGMA table_info({_GAP_TABLE})")
        )
    finally:
        old_db.close()
        current_db.close()
    if legacy is None or current is None or legacy == current or not columns:
        raise StorageInitializationError(
            "The allowlisted legacy outage-gap table form could not be derived."
        )
    return _DecisionForms(legacy=legacy, current=current, columns=columns)


def outage_gap_token_rebuild_applies(db: sqlite3.Connection) -> bool:
    """Detect only the exact prerelease table shape without retry tokens."""
    return _stored_table_sql(db, _GAP_TABLE) == (known_outage_gap_table_forms().legacy)


def rebuild_inbound_pressure_outage_gaps(db: sqlite3.Connection) -> None:
    """Preserve legacy gap records while assigning stable content-free tokens.

    This guard runs under BEGIN IMMEDIATE. No other table references the gap
    table, and its only identity is its autoincrement integer primary key.
    Backfilled tokens use that original key padded to 32 lowercase hex digits;
    unlike runtime UUIDv4 tokens, they cannot have a version-4 nibble.
    """
    if not outage_gap_token_rebuild_applies(db):
        return
    columns = known_outage_gap_table_forms().columns
    rows = db.execute(
        f"SELECT {', '.join(columns)} FROM {_GAP_TABLE} ORDER BY gap_id"
    ).fetchall()
    db.execute(f"ALTER TABLE {_GAP_TABLE} RENAME TO {_LEGACY_GAP_TABLE}")
    db.execute(_PRESSURE_OUTAGE_GAPS_DDL)
    for row in rows:
        gap_id = int(row[0])
        db.execute(
            f"INSERT INTO {_GAP_TABLE} (gap_token, {', '.join(columns)}) "
            f"VALUES (?, {', '.join('?' for _ in columns)})",
            (f"{gap_id:032x}", *row),
        )
    db.execute(f"DROP TABLE {_LEGACY_GAP_TABLE}")
    after = db.execute(f"SELECT COUNT(*) FROM {_GAP_TABLE}").fetchone()
    if after is None or int(after[0]) != len(rows):
        raise StorageInitializationError(
            f"Pre-release migration '{_ADD_GAP_RETRY_TOKEN_STEP}' "
            "changed the outage-gap record count; the rebuild was rolled back."
        )


# ---------------------------------------------------------------------------
# Strip of the retired inline diagnostic columns (delivery evidence families)
# ---------------------------------------------------------------------------

#: Evidence families whose free-form diagnostic columns were removed from the
#: declared schema.  Every diagnostic value lives only in the append-only
#: ``delivery_evidence_diagnostics`` side table; these allowlisted predecessor
#: layouts still carry the retired inline columns and are rebuilt without
#: them, with their historical inline text copied into the side table.
_STRIP_DIAGNOSTIC_COLUMNS_STEP = "strip-inline-diagnostic-columns"

_DIAGNOSTICS_TABLE = "delivery_evidence_diagnostics"

#: Per family: (table, row_id column, attempt column, timestamp column,
#: retired inline columns).  ``attempt`` is the side-table attempt key the
#: family's writes use.
_DIAGNOSTIC_FAMILY_TABLES: dict[str, tuple[str, str, str, str, tuple[str, ...]]] = {
    "receipt": (
        "delivery_receipts",
        "receipt_id",
        "attempt_number",
        "created_at",
        ("error", "capability_reason", "rendering_evidence"),
    ),
    "observation": (
        "delivery_observations",
        "observation_id",
        "attempt_number",
        "observed_at",
        ("error",),
    ),
    "outbox": (
        "delivery_outbox",
        "outbox_id",
        "attempt_number",
        "updated_at",
        ("failure_kind_detail", "error_summary"),
    ),
    "ingress": (
        "durable_ingress_work",
        "event_id",
        "attempts",
        "updated_at",
        ("last_error",),
    ),
}

#: Exact insertion anchors in the authoritative DDL.  Each anchor is unique to
#: its table after the strip; inserting the retired column lines reproduces
#: the exact predecessor CREATE TABLE text byte for byte.
_DIAGNOSTIC_COLUMN_ANCHORS: dict[str, tuple[tuple[str, str], ...]] = {
    "delivery_receipts": (
        ("    receipt_kind TEXT NOT NULL,\n", "    error TEXT,\n"),
        ("    capability_field TEXT,\n", "    capability_reason TEXT,\n"),
        ("    retry_jitter INTEGER,\n", "    rendering_evidence TEXT,\n"),
    ),
    "delivery_outbox": (
        (
            "    active_attempt INTEGER,\n"
            "    status TEXT NOT NULL DEFAULT 'pending',\n"
            "    failure_kind TEXT,\n",
            "    failure_kind_detail TEXT,\n",
        ),
        (
            (
                "    payload_hash TEXT,\n"
                "    receipt_id TEXT,\n"
                "    parent_receipt_id TEXT,\n"
            ),
            "    error_summary TEXT,\n",
        ),
    ),
    "delivery_observations": (
        (
            "    adapter_message_id TEXT,\n"
            "    state TEXT NOT NULL,\n"
            "    confirmation_level TEXT NOT NULL DEFAULT 'unknown',\n",
            "    error TEXT,\n",
        ),
    ),
    "durable_ingress_work": (
        (
            "    attempts INTEGER NOT NULL DEFAULT 0,\n",
            "    last_error TEXT,\n",
        ),
    ),
}


@lru_cache(maxsize=1)
def known_stripped_diagnostic_table_forms() -> dict[str, str]:
    """Derive the exact predecessor CREATE TABLE text per evidence family.

    The predecessor form is the authoritative DDL with the retired inline
    diagnostic columns re-inserted at their declared anchors — the same
    derivation scheme as the other allowlisted forms — so detection cannot
    drift from the declared shape.  Raises :class:`StorageInitializationError`
    when an anchor no longer matches, so a future schema edit fails loudly
    instead of silently treating the current shape as a predecessor.
    """
    legacy_schema = _SCHEMA
    for table, anchors in _DIAGNOSTIC_COLUMN_ANCHORS.items():
        for anchor, column_line in anchors:
            if anchor not in legacy_schema:
                raise StorageInitializationError(
                    f"Authoritative DDL no longer contains the expected anchor "
                    f"for the {table} inline-diagnostic predecessor; the "
                    "allowlisted predecessor cannot be derived."
                )
            legacy_schema = legacy_schema.replace(
                anchor, anchor + column_line, 1
            )
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(legacy_schema + _INDEXES)
        forms: dict[str, str] = {}
        for _family, (table, *_rest) in _DIAGNOSTIC_FAMILY_TABLES.items():
            stored = _stored_table_sql(reference, table)
            if stored is None:
                raise StorageInitializationError(
                    f"The allowlisted inline-diagnostic predecessor form of "
                    f"{table} could not be derived from the authoritative DDL."
                )
            forms[table] = stored
        return forms
    finally:
        reference.close()


@lru_cache(maxsize=1)
def known_stripped_diagnostic_view_form() -> str:
    """Return the exact predecessor ``delivery_status`` view definition.

    The predecessor view enumerates the retired receipt columns directly; the
    receipt rebuild recreates the view from the authoritative slice.  Derived
    from the same authoritative DDL as the table forms so detection cannot
    drift.
    """
    legacy_schema = _SCHEMA
    for anchors in _DIAGNOSTIC_COLUMN_ANCHORS.values():
        for anchor, column_line in anchors:
            legacy_schema = legacy_schema.replace(anchor, anchor + column_line, 1)
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(legacy_schema + _INDEXES)
        row = reference.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='delivery_status'"
        ).fetchone()
        if row is None:
            raise StorageInitializationError(
                "The allowlisted delivery_status view predecessor could not be "
                "derived from the authoritative DDL."
            )
        return str(row[0])
    finally:
        reference.close()


def _stripped_diagnostic_table_applies(db: sqlite3.Connection, table: str) -> bool:
    """Report whether the stored table is exactly the retired-column predecessor."""
    legacy = known_stripped_diagnostic_table_forms().get(table)
    return legacy is not None and _stored_table_sql(db, table) == legacy


def _copy_inline_diagnostics_to_side_table(
    db: sqlite3.Connection, family: str, table: str
) -> None:
    """Copy the legacy table's inline diagnostic text into the side table.

    Runs before the predecessor table is dropped so historical inline values
    keep resolving through the same side-table reads every current row uses.
    Empty strings never copy: they carry no diagnostic content.
    """
    _family_table, row_id_column, attempt_column, created_column, fields = (
        _DIAGNOSTIC_FAMILY_TABLES[family]
    )
    for field in fields:
        db.execute(
            "INSERT INTO delivery_evidence_diagnostics"
            " (family, row_id, attempt, field, diagnostic, created_at)"
            f" SELECT '{family}', {row_id_column}, {attempt_column},"
            f" '{field}', {field}, {created_column}"
            f" FROM {table}"
            f" WHERE {field} IS NOT NULL AND {field} <> ''"
        )


def _rebuild_stripped_diagnostic_table(db: sqlite3.Connection, family: str) -> None:
    """Rebuild one evidence table without its retired inline diagnostic columns.

    The rebuild avoids ``ALTER TABLE ... RENAME`` on the live table name
    because three tables hold foreign-key references to ``delivery_outbox``:
    the current-shape table is created under a temporary name, the legacy
    rows and their inline diagnostic text are copied, the legacy table is
    dropped, and the temporary table takes the real name.  The table's own
    indexes are recreated verbatim from the stored catalog.

    Raises :class:`StorageInitializationError` when the rebuilt row count
    differs from the original.  The caller rolls back the transaction,
    leaving the database unchanged.
    """
    table, _row_id, _attempt, _created, retired_columns = (
        _DIAGNOSTIC_FAMILY_TABLES[family]
    )
    if not _stripped_diagnostic_table_applies(db, table):
        return
    current = known_decision_current_table_sql(table)
    if current is None:
        raise StorageInitializationError(
            f"The current declared form of {table} could not be derived."
        )
    before_row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert before_row is not None  # COUNT(*) always yields one row
    before = int(before_row[0])
    legacy_columns = [
        str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")
    ]
    kept_columns = [c for c in legacy_columns if c not in retired_columns]
    columns = ", ".join(kept_columns)
    index_rows = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    index_sql = [str(row[0]) for row in index_rows]
    # The delivery_status view enumerates receipt columns and joins the
    # outbox; SQLite re-parses views after every rename, so the view is
    # dropped before the table swap and recreated from the authoritative
    # slice afterwards.
    db.execute("DROP VIEW IF EXISTS delivery_status")
    temp_table = f"{table}__strip_tmp"
    db.execute(f"DROP TABLE IF EXISTS {temp_table}")
    temp_sql = current.replace(
        f"CREATE TABLE IF NOT EXISTS {table} ",
        f"CREATE TABLE IF NOT EXISTS {temp_table} ",
        1,
    )
    if temp_sql == current:
        temp_sql = current.replace(
            f"CREATE TABLE {table} ",
            f"CREATE TABLE {temp_table} ",
            1,
        )
    if temp_sql == current:
        raise StorageInitializationError(
            f"The declared CREATE TABLE statement for {table} could not be "
            "renamed for the strip rebuild."
        )
    # The rows round-trip through the temporary table so the final stored
    # CREATE TABLE text is the declared DDL verbatim: an ALTER TABLE rename
    # stores the name quoted, which the prerelease identity contract rejects.
    db.execute(temp_sql)
    _copy_inline_diagnostics_to_side_table(db, family, table)
    db.execute(f"INSERT INTO {temp_table} ({columns}) SELECT {columns} FROM {table}")
    db.execute(f"DROP TABLE {table}")
    db.execute(current)
    db.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp_table}")
    db.execute(f"DROP TABLE {temp_table}")
    for sql in index_sql:
        db.execute(sql)
    _recreate_delivery_status_view(db)
    after_row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert after_row is not None  # COUNT(*) always yields one row
    after = int(after_row[0])
    if after != before:
        raise StorageInitializationError(
            f"Pre-release migration '{_STRIP_DIAGNOSTIC_COLUMNS_STEP}' "
            f"changed the {table} record count; the rebuild was rolled back."
        )


def _recreate_delivery_status_view(db: sqlite3.Connection) -> None:
    """Recreate the ``delivery_status`` view from the authoritative DDL slice.

    The view enumerates receipt columns directly, so stripping the retired
    inline diagnostic columns from ``delivery_receipts`` leaves the stored
    view stale.  Recreated from the same authoritative slice the fresh-database
    path executes, byte for byte.
    """
    drop_start = _SCHEMA.index("DROP VIEW IF EXISTS delivery_status;")
    create_start = _SCHEMA.index("CREATE VIEW delivery_status AS", drop_start)
    end = _SCHEMA.index("WHERE authority_rank = 1;")
    end = _SCHEMA.index(";", end) + 1
    db.execute(_SCHEMA[drop_start:_SCHEMA.index(";", drop_start) + 1])
    db.execute(_SCHEMA[create_start:end])


def rebuild_delivery_receipts_without_inline_diagnostics(
    db: sqlite3.Connection,
) -> None:
    """Strip the retired receipt diagnostic columns."""
    _rebuild_stripped_diagnostic_table(db, "receipt")


def rebuild_delivery_observations_without_inline_diagnostics(
    db: sqlite3.Connection,
) -> None:
    """Strip the retired observation diagnostic column."""
    _rebuild_stripped_diagnostic_table(db, "observation")


def rebuild_delivery_outbox_without_inline_diagnostics(
    db: sqlite3.Connection,
) -> None:
    """Strip the retired outbox diagnostic columns."""
    _rebuild_stripped_diagnostic_table(db, "outbox")


def rebuild_durable_ingress_work_without_inline_diagnostics(
    db: sqlite3.Connection,
) -> None:
    """Strip the retired ingress diagnostic column."""
    _rebuild_stripped_diagnostic_table(db, "ingress")


def delivery_receipts_strip_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored receipt table is the retired-column predecessor."""
    return _stripped_diagnostic_table_applies(db, "delivery_receipts")


def delivery_observations_strip_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored observation table is the retired-column predecessor."""
    return _stripped_diagnostic_table_applies(db, "delivery_observations")


def delivery_outbox_strip_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored outbox table is the retired-column predecessor."""
    return _stripped_diagnostic_table_applies(db, "delivery_outbox")


def durable_ingress_work_strip_applies(db: sqlite3.Connection) -> bool:
    """Report whether the stored ingress table is the retired-column predecessor."""
    return _stripped_diagnostic_table_applies(db, "durable_ingress_work")



@dataclass(frozen=True)
class _MigrationStep:
    """One named, self-detecting, idempotent additive migration step."""

    name: str
    applies: Callable[[sqlite3.Connection], bool]
    apply: Callable[[sqlite3.Connection], None]


#: Ordered allowlisted pre-release migration steps.  A step must detect its
#: exact predecessor layout, transform it additively, and be a no-op on the
#: resulting current shape.
_MIGRATION_STEPS: tuple[_MigrationStep, ...] = (
    _MigrationStep(
        name=_WIDEN_DECISION_ACTIONS_STEP,
        applies=operator_decision_rebuild_applies,
        apply=rebuild_operator_delivery_decisions,
    ),
    _MigrationStep(
        name=_WIDEN_PRESSURE_OUTCOMES_STEP,
        applies=inbound_pressure_rebuild_applies,
        apply=rebuild_inbound_pressure_observations,
    ),
    _MigrationStep(
        name=_ADD_GAP_RETRY_TOKEN_STEP,
        applies=outage_gap_token_rebuild_applies,
        apply=rebuild_inbound_pressure_outage_gaps,
    ),    _MigrationStep(
        name=_STRIP_DIAGNOSTIC_COLUMNS_STEP + "-receipts",
        applies=delivery_receipts_strip_applies,
        apply=rebuild_delivery_receipts_without_inline_diagnostics,
    ),
    _MigrationStep(
        name=_STRIP_DIAGNOSTIC_COLUMNS_STEP + "-observations",
        applies=delivery_observations_strip_applies,
        apply=rebuild_delivery_observations_without_inline_diagnostics,
    ),
    _MigrationStep(
        name=_STRIP_DIAGNOSTIC_COLUMNS_STEP + "-outbox",
        applies=delivery_outbox_strip_applies,
        apply=rebuild_delivery_outbox_without_inline_diagnostics,
    ),
    _MigrationStep(
        name=_STRIP_DIAGNOSTIC_COLUMNS_STEP + "-ingress",
        applies=durable_ingress_work_strip_applies,
        apply=rebuild_durable_ingress_work_without_inline_diagnostics,
    ),
)



def apply_prerelease_migrations(db: sqlite3.Connection) -> None:
    """Apply every allowlisted additive step the stored layout still needs.

    Runs inside the caller's ``BEGIN IMMEDIATE`` transaction; each step
    re-detects its predecessor immediately before rewriting anything, so a
    migrated database is a no-op on the next open.
    """
    for step in _MIGRATION_STEPS:
        if step.applies(db):
            step.apply(db)
