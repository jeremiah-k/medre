"""Shared read-only storage catalog compatibility assessment.

One classification surface for operators and callers that must agree with
storage startup.  The verdicts reuse the exact authorities the open paths
already apply — :func:`validate_schema_definition` for the current declared
catalog and :func:`safe_additive_state` for recognized additive
predecessors — so a compatible report cannot drift from what a writable or
read-only open accepts.  No independent fingerprint inventory exists here.

The assessment is read-only: it opens with the same strict read-only opener
the inspection commands use, reads everything inside one consistent
snapshot, and never writes application data or schema, migrates, requests a
checkpoint, or touches native state. SQLite may create or update WAL
bookkeeping sidecars. A verdict describes catalog compatibility only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from medre.core.storage.backend import (
    PreReleaseSchemaConstraintMismatchError,
    PreReleaseSchemaMismatchError,
    StorageDowngradeRefusedError,
    StorageInitializationError,
)
from medre.core.storage.sqlite._prerelease_migrate import (
    known_decision_table_forms,
    known_outage_gap_table_forms,
    known_pressure_table_forms,
    known_stripped_diagnostic_table_forms,
    known_stripped_diagnostic_view_form,
)
from medre.core.storage.sqlite.connection import sync_open_readonly
from medre.core.storage.sqlite.schema import _EXPECTED_SCHEMA_VERSION
from medre.core.storage.sqlite.schema_validation import (
    _definitions,
    _expected_definitions,
    _known_grant_table_forms,
    safe_additive_state,
    validate_schema_definition,
)


class CatalogCompatibility(StrEnum):
    """What the stored catalog is, relative to this build's declared schema.

    The names are the assessment's operator-facing vocabulary:

    - ``current``: the catalog matches the declared schema exactly.
    - ``supported_upgrade``: a recognized additive predecessor; one normal
      writable open applies the guarded layout migration.
    - ``unsupported``: a readable catalog that matches neither the current
      schema nor a recognized predecessor.
    - ``empty_new``: no schema objects present; a writable open creates the
      declared schema as new state.
    - ``unavailable``: the assessment could not read the database, so no
      compatibility conclusion was reached.  Distinct from ``unsupported``:
      lock waits, unreadable files, or database corruption are assessment
      I/O failures, not catalog compatibility answers.
    """

    CURRENT = "current"
    SUPPORTED_UPGRADE = "supported_upgrade"
    UNSUPPORTED = "unsupported"
    EMPTY_NEW = "empty_new"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CatalogAssessment:
    """One completed read-only catalog assessment.

    ``stored_version`` is the schema version stamp text when one was
    readable.  ``detail`` is a single-line operator diagnostic when the
    classification carries one.
    """

    classification: CatalogCompatibility
    stored_version: str | None
    detail: str | None


def _unsupported(stored_version: str | None, detail: str) -> CatalogAssessment:
    return CatalogAssessment(CatalogCompatibility.UNSUPPORTED, stored_version, detail)


def _mismatch_diagnostic(exc: StorageInitializationError) -> str:
    """Render one startup authority refusal as a compact single line.

    Keeps the owning object name and the concrete difference; the prose
    recovery guidance stays in the report's status line instead of being
    duplicated per diagnostic.
    """
    if isinstance(exc, PreReleaseSchemaMismatchError):
        parts = []
        if exc.missing_columns:
            parts.append(f"missing columns {exc.missing_columns}")
        if exc.unexpected_columns:
            parts.append(f"unexpected columns {exc.unexpected_columns}")
        if exc.detail:
            parts.append(exc.detail)
        body = "; ".join(parts) or "differs from the declared catalog"
        return f"{exc.table}: {body}"
    if isinstance(exc, PreReleaseSchemaConstraintMismatchError):
        return f"{exc.table}: {', '.join(exc.missing_constraints)}"
    if isinstance(exc, StorageDowngradeRefusedError):
        return exc.summary
    return str(exc).split(". (", maxsplit=1)[0]


def _recognized_predecessor_detail(db: sqlite3.Connection) -> str:
    """Name the allowlisted gaps a recognized predecessor would migrate."""
    actual = _definitions(db)
    expected = _expected_definitions()
    parts = []
    missing = sorted(f"{kind} {name}" for kind, name in expected.keys() - actual.keys())
    if missing:
        parts.append("missing objects: " + ", ".join(missing))
    grant_old, _grant_altered = _known_grant_table_forms()
    predecessor_forms: dict[str, str] = {
        "event_content_access_grants": grant_old,
        "operator_delivery_decisions": known_decision_table_forms().legacy,
        "inbound_pressure_observations": known_pressure_table_forms().legacy,
        "inbound_pressure_outage_gaps": known_outage_gap_table_forms().legacy,
        **known_stripped_diagnostic_table_forms(),
    }
    legacy_names = sorted(
        name
        for name, definition in predecessor_forms.items()
        if actual.get(("table", name)) == definition
    )
    if actual.get(("view", "delivery_status")) == (
        known_stripped_diagnostic_view_form()
    ):
        legacy_names.append("delivery_status")
    if legacy_names:
        parts.append("predecessor forms: " + ", ".join(legacy_names))
    return "; ".join(parts) or "recognized additive predecessor layout"


def assess_catalog(db: sqlite3.Connection, *, path: str) -> CatalogAssessment:
    """Classify the catalog visible through one open SQLite connection.

    The caller owns the connection and its read snapshot: every catalog,
    version, and comparison read below must run inside one transaction so
    the verdict describes a single consistent point in time.  The current
    catalog is classified before :func:`safe_additive_state` is consulted —
    that predecessor predicate also accepts current catalogs, and a
    recognized predecessor is not already current.

    Read failures after a successful open (a lost lock race, corrupt
    pages) classify as ``unavailable`` with the underlying error text.
    """
    stored_version: str | None = None
    try:
        actual = _definitions(db)
        if (
            not actual
            and not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
            ).fetchone()
        ):
            return CatalogAssessment(
                CatalogCompatibility.EMPTY_NEW,
                None,
                None,
            )
        if ("table", "_medre_schema_meta") not in actual:
            return _unsupported(
                None, "unstamped database; missing schema_version metadata"
            )
        meta_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(_medre_schema_meta)")
        }
        if not {"key", "value"} <= meta_columns:
            return _unsupported(
                None, "schema metadata table lacks the key/value columns"
            )
        row = db.execute(
            "SELECT value FROM _medre_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return _unsupported(None, "schema_version stamp is missing")
        stored_version = str(row[0])
        try:
            stored = int(row[0])
        except (TypeError, ValueError):
            return _unsupported(
                stored_version,
                f"schema_version is not an integer: {stored_version!r}",
            )
        if stored != _EXPECTED_SCHEMA_VERSION:
            return _unsupported(
                stored_version,
                f"schema_version {stored} does not match the expected "
                f"{_EXPECTED_SCHEMA_VERSION}",
            )
        diagnostic: str | None = None
        try:
            if validate_schema_definition(db, path):
                return CatalogAssessment(
                    CatalogCompatibility.CURRENT, stored_version, None
                )
        except sqlite3.Error:
            raise
        except StorageInitializationError as exc:
            diagnostic = _mismatch_diagnostic(exc)
        if safe_additive_state(db):
            return CatalogAssessment(
                CatalogCompatibility.SUPPORTED_UPGRADE,
                stored_version,
                _recognized_predecessor_detail(db),
            )
        assert diagnostic is not None  # validate refuses before this point
        return _unsupported(stored_version, diagnostic)
    except sqlite3.Error as exc:
        return CatalogAssessment(
            CatalogCompatibility.UNAVAILABLE, stored_version, str(exc)
        )


def assess_storage_catalog(path: str) -> CatalogAssessment:
    """Assess the catalog of the database at *path* over one read snapshot.

    Opens an existing database with the read-only inspection opener (no schema
    or application-data writes, bounded busy timeout, never ``immutable=1``).
    SQLite may create or update WAL bookkeeping sidecars. Pins one
    deferred read transaction so the verdict describes a single consistent
    snapshot, and closes the connection.  Committed WAL state is visible
    like any other read.  Opening failures propagate to the caller as
    ``sqlite3.Error``; read failures after a successful open classify as
    ``unavailable``.
    """
    db = sync_open_readonly(path)
    try:
        db.execute("BEGIN")
        try:
            return assess_catalog(db, path=path)
        finally:
            db.rollback()
    finally:
        db.close()
