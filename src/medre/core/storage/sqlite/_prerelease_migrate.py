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
