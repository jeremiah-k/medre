"""Populated-predecessor historical-database migration matrix (merge gate).

Proves the full pre-merge ruling for REAL populated 0.1.0-shape databases —
not empty schemas — across every allowlisted predecessor layout:

- additive reopen from the populated predecessor succeeds transactionally;
  an injected DDL failure leaves the original database byte-identical;
- evidence backfill happens BEFORE any erase: legacy uncertainty carried in
  free-form diagnostics is copied into the typed side table while the legacy
  inline columns still exist, and a failure after the backfill still
  preserves the original bytes;
- restart mid-migration (close/reopen between opens) converges idempotently
  with no double backfill and no lost rows;
- ``PRAGMA foreign_keys=ON`` throughout, with no orphaned rows and writes
  still refused for orphaned references after the upgrade;
- an exact-attempt late acknowledgment reconciles on the UPGRADED populated
  database;
- both existing read-only verification paths pass on the upgraded database;
- the two policy guards hold on the upgraded database: automatic downgrade
  of the upgraded catalog is refused, and a stale backup (older delivery
  evidence) is refused as a restore replacement.

Predecessor layouts
-------------------
``released-0.1.0``: the sha-pinned verbatim released DDL fixture
(``tests/fixtures/released-storage-0.1.0.sql``), populated with legacy rows:
canonical events, retained attachment content, native references, receipts,
observations, ingress work, and legacy radio packet-acceptance plans riding
``radio_attempt_progress``. One test proves, when the local artifact cache
holds the pinned published 0.1.0 wheel, that the fixture catalog equals the
wheel's shipped catalog; without the cache that one test skips (CI runs it).
``released-0.2.2``: the 0.1.0 catalog plus the released handoff settlement
table and one historical settlement row.
``prerelease-stripped``: the authoritative-derived inline-diagnostic
predecessor — every catalog object present, retired inline diagnostic
columns still on the evidence tables, legacy uncertainty carried inline.
``prerelease-stripped-deep``: the same stripped forms with the additive
wave absent, so one open performs the additive DDL and the guarded
diagnostic backfill in a single transaction.

The suite derives the expected post-upgrade catalog from the same
authoritative DDL the continuity driver and ``apply_safe_additions`` use
(``_expected_definitions``), so extending the matrix over further additive
catalog objects at integration is a fixture update at most — not a redesign.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import medre.core.storage.sqlite._prerelease_migrate as prerelease_migrate
import medre.core.storage.sqlite.schema_validation as schema_validation
from medre.core.events.canonical import DeliveryObservation
from medre.core.storage.backend import StorageDowngradeRefusedError
from medre.core.storage.sqlite.catalog_assessment import (
    CatalogCompatibility,
    assess_storage_catalog,
)
from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.schema import _INDEXES
from medre.core.storage.sqlite.schema_validation import (
    _expected_definitions,
    validate_schema_definition,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.maintenance.stopped_snapshot import (
    SnapshotEntry,
    create_stopped_snapshot,
    read_delivery_evidence_watermark,
    restore_stopped_snapshot,
)

_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"
_SETTLEMENTS_SCHEMA = (
    Path(__file__).resolve().parents[1] / "docs/schemas/handoff-settlements.schema.sql"
)
_NOW = "2026-11-20T00:00:00+00:00"
_LATE_ACK_AT = datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)

_RELEASED_010_WHEEL = "medre-0.1.0-py3-none-any.whl"

LAYOUTS = (
    "released-0.1.0",
    "released-0.2.2",
    "prerelease-stripped",
    "prerelease-stripped-deep",
)

#: Legacy uncertainty in free-form diagnostics, identical after the upgrade
#: for every layout: in the released layouts it is seeded in the append-only
#: side table; in the prerelease layouts it lives in the retired inline
#: columns and the guarded migration must backfill it before erasing them.
#: The ``uncertain_native_handoff`` outbox detail is what the exact-attempt
#: late ACK reconciles against after the upgrade.
_LEGACY_DIAGNOSTICS: tuple[tuple[str, str, int, str, str], ...] = (
    ("receipt", "receipt-1", 1, "error", "radio timeout before native acceptance"),
    ("observation", "obs-1", 1, "error", "observation channel closed mid-scan"),
    ("outbox", "work-1", 1, "failure_kind_detail", "uncertain_native_handoff"),
    ("outbox", "work-2", 1, "error_summary", "delivered after one resend"),
    ("ingress", "event-1", 1, "last_error", "transient adapter reconnect"),
)

_RETIRED_INLINE_COLUMN = {
    "receipt": "error",
    "observation": "error",
    "outbox": "failure_kind_detail",
    "ingress": "last_error",
}


def _expected_side_table_rows() -> list[tuple[str, str, int, str, str]]:
    return sorted(
        (family, row_id, attempt, field, diagnostic)
        for family, row_id, attempt, field, diagnostic in _LEGACY_DIAGNOSTICS
    )


def _side_table_rows(db: sqlite3.Connection) -> list[tuple[str, str, int, str, str]]:
    return sorted(
        (str(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]))
        for r in db.execute(
            "SELECT family, row_id, attempt, field, diagnostic"
            " FROM delivery_evidence_diagnostics WHERE diagnostic <> ''"
        )
    )


# ---------------------------------------------------------------------------
# Populated predecessor builders
# ---------------------------------------------------------------------------


def _seed_rows(db: sqlite3.Connection, *, inline: bool) -> None:
    """Seed one populated predecessor with real legacy delivery state.

    ``inline`` selects where free-form diagnostics live: the retired inline
    columns for the prerelease layouts, the append-only side table for the
    released layouts. Both representations converge to the same post-upgrade
    typed side-table rows.
    """
    for event_id in ("event-1", "event-2"):
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES (?, 'message.text', 1, ?, 'radio', 'radio', ?)",
            (event_id, _NOW, _NOW),
        )
        db.execute(
            "INSERT INTO durable_ingress_work (event_id, provenance, status,"
            " attempts, created_at, updated_at)"
            " VALUES (?, 'radio', 'completed', 1, ?, ?)",
            (event_id, _NOW, _NOW),
        )
    content = b"legacy attachment bytes"
    ref = "sha256:" + hashlib.sha256(content).hexdigest()
    db.execute(
        "INSERT INTO attachment_blobs (content_ref, size_bytes, mime_type,"
        " created_at, data) VALUES (?, ?, 'application/octet-stream', ?, ?)",
        (ref, len(content), _NOW, content),
    )
    db.execute(
        "INSERT INTO event_attachment_associations VALUES ('event-1', ?, ?)",
        (ref, _NOW),
    )
    for event_id in ("event-1", "event-2"):
        db.execute(
            "INSERT INTO native_message_refs (id, event_id, adapter,"
            " native_message_id, direction, created_at)"
            " VALUES (?, ?, 'radio', ?, 'inbound', ?)",
            (f"ref-{event_id}", event_id, f"native-{event_id}", _NOW),
        )
    _insert_outbox(
        db,
        inline=inline,
        outbox_id="work-1",
        event_id="event-1",
        status="retry_wait",
        receipt_id="receipt-1",
        detail="uncertain_native_handoff" if inline else None,
    )
    _insert_outbox(
        db,
        inline=inline,
        outbox_id="work-2",
        event_id="event-2",
        status="sent",
        receipt_id="receipt-2",
        detail="delivered after one resend" if inline else None,
        detail_field="error_summary",
    )
    _insert_receipt(
        db,
        inline=inline,
        receipt_id="receipt-1",
        event_id="event-1",
        status="failed",
        outbox_id="work-1",
        error=_LEGACY_DIAGNOSTICS[0][4] if inline else None,
    )
    _insert_receipt(
        db,
        inline=inline,
        receipt_id="receipt-2",
        event_id="event-2",
        status="sent",
        outbox_id="work-2",
        error=None,
    )
    _insert_observation(
        db,
        inline=inline,
        observation_id="obs-1",
        error=_LEGACY_DIAGNOSTICS[1][4] if inline else None,
    )
    # Legacy radio packet acceptance: the wire plan riding the attempt.
    db.execute(
        "INSERT INTO radio_attempt_progress (outbox_id, attempt_number,"
        " plan_fingerprint, plan_json, provenance_json, parts_json, revision,"
        " created_at, updated_at) VALUES ('work-1', 1, ?, ?, ?, ?, 0, ?, ?)",
        (
            "a" * 64,
            '{"parts": 2}',
            '{"adapter": "radio"}',
            '[{"ordinal": 1}]',
            _NOW,
            _NOW,
        ),
    )
    if inline:
        db.execute(
            "UPDATE durable_ingress_work SET last_error = ? WHERE event_id = ?",
            (_LEGACY_DIAGNOSTICS[4][4], "event-1"),
        )
    else:
        for family, row_id, attempt, field, diagnostic in _LEGACY_DIAGNOSTICS:
            db.execute(
                "INSERT INTO delivery_evidence_diagnostics (family, row_id,"
                " attempt, field, diagnostic, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (family, row_id, attempt, field, diagnostic, _NOW),
            )


def _insert_outbox(
    db: sqlite3.Connection,
    *,
    inline: bool,
    outbox_id: str,
    event_id: str,
    status: str,
    receipt_id: str,
    detail: str | None,
    detail_field: str = "failure_kind_detail",
) -> None:
    columns = [
        "outbox_id",
        "event_id",
        "delivery_plan_id",
        "target_adapter",
        "attempt_number",
        "status",
        "receipt_id",
        "created_at",
        "updated_at",
    ]
    values: list[object] = [
        outbox_id,
        event_id,
        "plan",
        "radio",
        1,
        status,
        receipt_id,
        _NOW,
        _NOW,
    ]
    if inline:
        columns.append(detail_field)
        values.append(detail)
    db.execute(
        f"INSERT INTO delivery_outbox ({', '.join(columns)}) VALUES "  # nosec B608 # Declared schema names.
        f"({', '.join('?' for _ in columns)})",
        values,
    )


def _insert_receipt(
    db: sqlite3.Connection,
    *,
    inline: bool,
    receipt_id: str,
    event_id: str,
    status: str,
    outbox_id: str,
    error: str | None,
) -> None:
    columns = [
        "receipt_id",
        "event_id",
        "delivery_plan_id",
        "target_adapter",
        "status",
        "receipt_kind",
        "attempt_number",
        "outbox_id",
        "created_at",
    ]
    values: list[object] = [
        receipt_id,
        event_id,
        "plan",
        "radio",
        status,
        "attempt",
        1,
        outbox_id,
        _NOW,
    ]
    if inline:
        columns.append("error")
        values.append(error)
    db.execute(
        f"INSERT INTO delivery_receipts ({', '.join(columns)}) VALUES "  # nosec B608 # Declared schema names.
        f"({', '.join('?' for _ in columns)})",
        values,
    )


def _insert_observation(
    db: sqlite3.Connection, *, inline: bool, observation_id: str, error: str | None
) -> None:
    columns = [
        "observation_id",
        "event_id",
        "delivery_plan_id",
        "target_adapter",
        "outbox_id",
        "attempt_number",
        "adapter_message_id",
        "state",
        "confirmation_level",
        "metadata",
        "observed_at",
    ]
    values: list[object] = [
        observation_id,
        "event-1",
        "plan",
        "radio",
        "work-1",
        1,
        "nm-1",
        "failed",
        "unknown",
        "{}",
        _NOW,
    ]
    if inline:
        columns.append("error")
        values.append(error)
    db.execute(
        f"INSERT INTO delivery_observations ({', '.join(columns)}) VALUES "  # nosec B608 # Declared schema names.
        f"({', '.join('?' for _ in columns)})",
        values,
    )


def _build_layout(name: str, path: Path) -> None:
    """Create one populated predecessor database at *path*."""
    if name == "released-0.1.0":
        ddl = _fixture_ddl()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(ddl)
            db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
            _seed_rows(db, inline=False)
    elif name == "released-0.2.2":
        _build_layout("released-0.1.0", path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(_SETTLEMENTS_SCHEMA.read_text(encoding="utf-8"))
            db.execute(
                "INSERT INTO delivery_handoff_settlements VALUES ('work-1', 1, 2)"
            )
    elif name in {"prerelease-stripped", "prerelease-stripped-deep"}:
        predecessor = prerelease_migrate._stripped_diagnostic_predecessor_schema()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(predecessor + _INDEXES)
            db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
            _seed_rows(db, inline=True)
        if name == "prerelease-stripped-deep":
            _drop_additive_wave(path)
    else:  # pragma: no cover - the parametrize list is closed
        raise AssertionError(f"unknown layout {name}")


def _fixture_ddl() -> str:
    ddl = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(ddl).hexdigest() == _FIXTURE_SHA256
    return ddl.decode("utf-8")


def _drop_additive_wave(path: Path) -> None:
    """Remove the additive wave so one open performs DDL and backfill.

    Every dropped object is an allowlisted additive object; the remaining
    catalog is the deepest allowlisted predecessor: stripped inline columns
    AND missing additive objects at once.
    """
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for statement in (
            "DROP TRIGGER trg_scope_version_insert",
            "DROP TRIGGER trg_scope_version_update",
            "DROP TRIGGER trg_scope_version_delete",
            "DROP TABLE delivery_observation_scopes",
            "DROP TABLE delivery_observation_scope_versions",
            "DROP TABLE radio_accepted_part_facts",
            "DROP TABLE radio_attempt_evidence_summaries",
            "DROP TABLE delivery_handoff_uncertainty",
            "DROP TABLE delivery_handoff_settlements",
            "DROP TABLE event_content_ceiling_log",
            "DROP INDEX idx_ceiling_enforcement_order",
            "DROP INDEX idx_attachment_associations_content_ref",
        ):
            db.execute(statement)


def _snapshot_state(
    path: Path,
) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    """Dump the exact catalog and all rows of one database, FK-checked."""
    with closing(sqlite3.connect(path)) as db:
        catalog = [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
                " ORDER BY type,name"
            )
        ]
        rows = {}
        for kind, name, _sql in catalog:
            if kind != "table":
                continue
            rows[str(name)] = [
                tuple(row)
                for row in db.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'  # nosec B608 # The identifier comes from the persisted catalog enumeration, never external input.
                )
            ]
        assert list(db.execute("PRAGMA foreign_key_check")) == []
    return catalog, rows


def _assert_current_catalog(db: sqlite3.Connection, path: str) -> None:
    assert validate_schema_definition(db, path)
    expected_catalog = sorted(
        (kind, name, definition)
        for (kind, name), definition in _expected_definitions().items()
    )
    actual = [
        (str(kind), str(name), str(sql))
        for kind, name, sql in db.execute(
            "SELECT type,name,sql FROM sqlite_master"
            " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            " ORDER BY type,name"
        )
    ]
    assert actual == expected_catalog


@pytest.fixture(params=LAYOUTS, ids=LAYOUTS)
def legacy_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    path = tmp_path / f"{request.param}.sqlite"
    _build_layout(str(request.param), path)
    return path


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_upgrade_is_transactional_and_byte_identical_on_failure(
    legacy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected additive-DDL failure rolls back to the byte-identical original."""
    if legacy_path.stem == "prerelease-stripped":
        # No additive DDL is missing in this layout, so the additive-DDL seam
        # cannot fire; its transactionality is covered by the backfill-failure
        # test at the same open boundary.
        pytest.skip("no additive DDL to fail; covered by the backfill seam")
    before = legacy_path.read_bytes()
    execute_ddl = schema_validation.execute_ddl

    def fail_additive_ddl(db: sqlite3.Connection, ddl: str) -> None:
        if "CREATE INDEX IF NOT EXISTS idx_ceiling_enforcement_order" in ddl:
            assert db.in_transaction
            raise sqlite3.OperationalError("injected ceiling index failure")
        execute_ddl(db, ddl)

    monkeypatch.setattr(schema_validation, "execute_ddl", fail_additive_ddl)
    with pytest.raises(sqlite3.OperationalError, match="injected ceiling index"):
        with closing(sync_open(str(legacy_path))):
            pass
    assert legacy_path.read_bytes() == before
    monkeypatch.undo()
    with closing(sync_open(str(legacy_path))) as db:
        _assert_current_catalog(db, str(legacy_path))


def test_backfill_failure_after_copy_preserves_original_bytes(
    legacy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the diagnostic backfill still preserves the original."""
    if not legacy_path.name.startswith("prerelease"):
        pytest.skip("inline-column erase only exists in prerelease layouts")
    before = legacy_path.read_bytes()

    def fail_view_recreation(db: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected view recreation failure")

    monkeypatch.setattr(
        prerelease_migrate, "_recreate_delivery_status_view", fail_view_recreation
    )
    with pytest.raises(sqlite3.OperationalError, match="injected view recreation"):
        with closing(sync_open(str(legacy_path))):
            pass
    assert legacy_path.read_bytes() == before
    with closing(sqlite3.connect(legacy_path)) as db:
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(delivery_receipts)")
        }
        assert "error" in columns  # legacy inline uncertainty not yet erased
        assert (
            db.execute("SELECT COUNT(*) FROM delivery_evidence_diagnostics").fetchone()[
                0
            ]
            == 0
        )  # the backfill rolled back: no partial erase


def test_backfill_happens_before_erase(
    legacy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy uncertainty is copied while the legacy columns still exist."""
    if not legacy_path.name.startswith("prerelease"):
        pytest.skip("inline-column erase only exists in prerelease layouts")
    observed_families: list[str] = []
    legacy_columns_alive: list[bool] = []
    original = prerelease_migrate._copy_inline_diagnostics_to_side_table

    def observing_copy(db: sqlite3.Connection, family: str, table: str) -> None:
        columns = {
            str(row[1])
            for row in db.execute(
                f"PRAGMA table_info({table})"
            )  # nosec B608 # Declared schema names.
        }
        legacy_columns_alive.append(_RETIRED_INLINE_COLUMN[family] in columns)
        observed_families.append(family)
        original(db, family, table)

    monkeypatch.setattr(
        prerelease_migrate, "_copy_inline_diagnostics_to_side_table", observing_copy
    )
    with closing(sync_open(str(legacy_path))) as db:
        assert sorted(observed_families) == [
            "ingress",
            "observation",
            "outbox",
            "receipt",
        ]
        assert all(legacy_columns_alive), "backfill ran after the legacy erase"
        assert _side_table_rows(db) == _expected_side_table_rows()
        assert validate_schema_definition(db, str(legacy_path))


def test_restart_mid_migration_converges_idempotently(legacy_path: Path) -> None:
    """Close/reopen between opens converges with no double backfill."""
    with closing(sync_open(str(legacy_path))) as db:
        _assert_current_catalog(db, str(legacy_path))
        first_catalog, first_rows = _snapshot_state(str(legacy_path))
        assert _side_table_rows(db) == _expected_side_table_rows()
    with closing(sync_open(str(legacy_path))) as db:
        second_catalog, second_rows = _snapshot_state(str(legacy_path))
        assert second_catalog == first_catalog
        assert second_rows == first_rows
        assert _side_table_rows(db) == _expected_side_table_rows()
    with closing(sync_open(str(legacy_path))):
        third_catalog, third_rows = _snapshot_state(str(legacy_path))
        assert third_catalog == first_catalog
        assert third_rows == first_rows


def test_foreign_keys_enforced_and_no_orphans(legacy_path: Path) -> None:
    """FK enforcement stays on and the upgrade leaves no orphaned rows."""
    with closing(sync_open(str(legacy_path))) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert list(db.execute("PRAGMA foreign_key_check")) == []
        assert db.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0] == 2
        assert (
            db.execute("SELECT COUNT(*) FROM delivery_observations").fetchone()[0] == 1
        )
        assert (
            db.execute("SELECT COUNT(*) FROM radio_attempt_progress").fetchone()[0] == 1
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO delivery_receipts (receipt_id, event_id,"
                " delivery_plan_id, target_adapter, status, receipt_kind,"
                " attempt_number, outbox_id, created_at)"
                " VALUES ('orphan', 'missing-event', 'plan', 'radio', 'failed',"
                " 'attempt', 1, 'work-1', ?)",
                (_NOW,),
            )


async def test_delayed_ack_reconciles_on_upgraded_db(legacy_path: Path) -> None:
    """An exact-attempt late ACK reconciles against the upgraded evidence."""
    with closing(sync_open(str(legacy_path))):
        pass
    storage = SQLiteStorage(db_path=str(legacy_path))
    try:
        await storage.initialize()
        observation = DeliveryObservation(
            observation_id="obs-late-ack",
            event_id="event-1",
            delivery_plan_id="plan",
            target_adapter="radio",
            target_channel=None,
            native_channel_id=None,
            outbox_id="work-1",
            attempt_number=1,
            adapter_message_id="native-ack-1",
            state="delivered",
            confirmation_level="end_to_end",
            error=None,
            metadata={},
            observed_at=_LATE_ACK_AT,
        )
        assert await storage.append_delivery_observation(observation) is True
        rows = await storage.list_delivery_observations_for_outbox("work-1")
        assert [row.observation_id for row in rows] == ["obs-1", "obs-late-ack"]
        late = rows[-1]
        assert late.state == "delivered"
        assert late.attempt_number == 1
        assert late.adapter_message_id == "native-ack-1"
    finally:
        await storage.close()


def test_readonly_assessment_passes_on_upgraded_db(legacy_path: Path) -> None:
    """The shared read-only catalog assessment accepts the upgraded database."""
    with closing(sync_open(str(legacy_path))):
        pass
    assessment = assess_storage_catalog(str(legacy_path))
    assert assessment.classification is CatalogCompatibility.CURRENT
    assert assessment.stored_version == "1"


async def test_readonly_inspection_open_passes_on_upgraded_db(
    legacy_path: Path,
) -> None:
    """The read-only inspection opener verifies the upgraded database."""
    with closing(sync_open(str(legacy_path))):
        pass
    storage = await SQLiteStorage.open_readonly(str(legacy_path))
    try:
        db = storage._require_db()
        rows = {
            str(row[0]) for row in db.execute("SELECT event_id FROM canonical_events")
        }
        assert rows == {"event-1", "event-2"}
    finally:
        await storage.close()


def test_downgrade_guard_holds_on_upgraded_db(legacy_path: Path) -> None:
    """An upgraded populated database refuses automatic downgrade."""
    with closing(sync_open(str(legacy_path))):
        pass
    with closing(sqlite3.connect(legacy_path)) as db:
        db.execute(
            "CREATE TABLE future_confirmation_dispositions ("
            " event_id TEXT PRIMARY KEY, disposition TEXT NOT NULL)"
        )
        db.commit()
    before = legacy_path.read_bytes()
    with pytest.raises(StorageDowngradeRefusedError):
        with closing(sync_open(str(legacy_path))):
            pass
    assert legacy_path.read_bytes() == before


def test_stale_backup_restore_guard_holds_on_upgraded_db(legacy_path: Path) -> None:
    """A backup older than the upgraded evidence is refused as a replacement."""
    install_root = legacy_path.parent / "install"
    install_root.mkdir()
    live = install_root / "state" / "medre.sqlite"
    live.parent.mkdir()
    shutil.copyfile(legacy_path, live)
    backup = legacy_path.parent / "backup"
    create_stopped_snapshot(
        install_root,
        backup,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    backup_watermark = read_delivery_evidence_watermark(
        backup / "files" / "state" / "medre.sqlite"
    )
    # Upgrade the live database and let it accumulate one more receipt.
    with closing(sync_open(str(live))) as db:
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id,"
            " delivery_plan_id, target_adapter, status, receipt_kind,"
            " attempt_number, outbox_id, created_at)"
            " VALUES ('receipt-3', 'event-1', 'plan', 'radio', 'sent',"
            " 'attempt', 1, 'work-1', ?)",
            (_NOW,),
        )
        db.commit()
    assert read_delivery_evidence_watermark(live) > backup_watermark
    destination = legacy_path.parent / "restored"
    with pytest.raises(ValueError, match="refusing to restore a stale backup"):
        restore_stopped_snapshot(
            backup,
            destination,
            runtime_stopped=True,
            live_database=live,
        )
    assert not destination.exists()
    # The remedy is a fresh snapshot: its watermark matches the live state,
    # and restoring it against the same live database is allowed.
    fresh_backup = legacy_path.parent / "backup-fresh"
    create_stopped_snapshot(
        install_root,
        fresh_backup,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    assert read_delivery_evidence_watermark(live) == read_delivery_evidence_watermark(
        fresh_backup / "files" / "state" / "medre.sqlite"
    )
    restore_stopped_snapshot(
        fresh_backup,
        destination,
        runtime_stopped=True,
        live_database=live,
    )
    assert (destination / "state" / "medre.sqlite").is_file()


# ---------------------------------------------------------------------------
# Artifact-cache continuity (skips cleanly without the local cache)
# ---------------------------------------------------------------------------


def test_released_fixture_matches_cached_published_wheel(tmp_path: Path) -> None:
    """The matrix predecessor is the published 0.1.0 wheel's exact catalog.

    Uses the same sha-pinned artifact cache the released-artifact continuity
    driver consumes; the digest is re-verified here before use.
    """
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts/verify_released_artifact_continuity.py"
    )
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "medre_verify_released_artifact_continuity", script
    )
    assert spec is not None and spec.loader is not None
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    cache = driver.default_artifact_cache()
    found = driver.cache_status(cache)
    if _RELEASED_010_WHEEL not in found:
        pytest.skip("released 0.1.0 wheel absent from the local artifact cache")
    wheel = cache / _RELEASED_010_WHEEL
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == driver._PINNED_ARTIFACTS[_RELEASED_010_WHEEL]
    with zipfile.ZipFile(wheel) as archive:
        source = archive.read("medre/core/storage/sqlite/schema.py").decode("utf-8")
    namespace: dict[str, object] = {"__name__": "released_schema"}
    exec(
        compile(source, "released_schema.py", "exec"), namespace
    )  # nosec B102 # Pinned, digest-verified released wheel source.
    wheel_catalog = _catalog_object_set(
        str(namespace["_SCHEMA"]) + str(namespace["_INDEXES"])
    )
    assert wheel_catalog == _catalog_object_set(_fixture_ddl())
    # Spot-check the exact released receipt DDL the matrix populates.
    assert _definition_of(str(namespace["_SCHEMA"]), "delivery_receipts") == (
        _definition_of(_fixture_ddl(), "delivery_receipts")
    )


def _catalog_object_set(ddl: str) -> set[tuple[str, str]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(ddl)
        return {
            (str(row[0]), str(row[1]))
            for row in reference.execute(
                "SELECT type,name FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            )
        }
    finally:
        reference.close()


def _definition_of(ddl: str, table: str) -> str:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(ddl)
        row = reference.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        reference.close()
