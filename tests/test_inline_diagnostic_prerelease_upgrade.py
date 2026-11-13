"""Upgrade the exact populated, pre-release inline-diagnostic SQLite layout."""

from __future__ import annotations

import sqlite3

from medre.core.storage.sqlite._prerelease_migrate import (
    known_stripped_diagnostic_view_form,
    _DIAGNOSTIC_COLUMN_ANCHORS,
)
from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA


# Build predecessor tables through declared insertion anchors, but reconstruct
# the historical view independently to catch incorrect view derivation.
def _retired_schema() -> str:
    schema = _SCHEMA
    for anchors in _DIAGNOSTIC_COLUMN_ANCHORS.values():
        for anchor, column in anchors:
            assert schema.count(anchor) == 1
            schema = schema.replace(anchor, anchor + column, 1)
    for current, predecessor in (
        (
            "       receipt_kind, failure_kind,\n",
            "       receipt_kind, error, failure_kind,\n",
        ),
        (
            "       capability_level, capability_field, delivery_strategy,\n",
            "       capability_level, capability_field, capability_reason, delivery_strategy,\n",
        ),
        (
            "       retry_max_delay, retry_jitter,\n",
            "       retry_max_delay, retry_jitter, rendering_evidence,\n",
        ),
    ):
        assert schema.count(current) == 1
        schema = schema.replace(current, predecessor, 1)
    return schema


def test_predecessor_delivery_status_view_projects_retired_columns() -> None:
    """The historical view projected diagnostic columns, not just tables."""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(_retired_schema() + _INDEXES)
        expected = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='delivery_status'"
        ).fetchone()[0]
        assert known_stripped_diagnostic_view_form() == expected
        assert "receipt_kind, error, failure_kind" in expected
        assert "capability_field, capability_reason" in expected
        assert "retry_jitter, rendering_evidence" in expected
    finally:
        db.close()


def test_populated_predecessor_upgrades_without_losing_fk_or_clear(tmp_path) -> None:
    """Preserve foreign-key children and newer side-table clear markers."""
    path = tmp_path / "before-strip.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(_retired_schema() + _INDEXES)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, lineage, payload,"
            " metadata, created_at) VALUES ('ev', 'text', 1, '2026-11-12',"
            " 'fake', 'n', '[]', '{}', '{}', '2026-11-12')"
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, status, attempt_number, failure_kind_detail,"
            " error_summary, created_at, updated_at) VALUES"
            " ('ob', 'ev', 'plan', 'fake', 'retry_wait', 1, 'old-kind',"
            " 'old-summary', '2026-11-12', '2026-11-12')"
        )
        db.execute(
            "INSERT INTO delivery_observations (observation_id, event_id,"
            " delivery_plan_id, target_adapter, outbox_id, attempt_number,"
            " state, error, observed_at) VALUES"
            " ('obs', 'ev', 'plan', 'fake', 'ob', 1, 'failed',"
            " 'old-observation', '2026-11-12')"
        )
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id, delivery_plan_id,"
            " target_adapter, status, receipt_kind, error, capability_reason,"
            " rendering_evidence, created_at) VALUES"
            " ('rec', 'ev', 'plan', 'fake', 'failed', 'attempt', 'old-receipt',"
            " 'old-cap', 'old-render', '2026-11-12')"
        )
        db.execute(
            "INSERT INTO durable_ingress_work (event_id, provenance, status,"
            " attempts, last_error, created_at, updated_at) VALUES"
            " ('ev', '{}', 'pending', 1, 'old-ingress', '2026-11-12', '2026-11-12')"
        )
        # Append-only diagnostics from a newer write supersede inline values.
        db.executemany(
            "INSERT INTO delivery_evidence_diagnostics"
            " (family, row_id, attempt, field, diagnostic, created_at)"
            " VALUES (?, ?, ?, ?, ?, '2026-11-12')",
            [
                ("outbox", "ob", 1, "error_summary", ""),
                ("receipt", "rec", 1, "error", "newer-receipt"),
                ("ingress", "ev", 2, "last_error", ""),
                ("outbox", "ob", 0, "failure_kind_detail", "previous-attempt"),
            ],
        )
        db.commit()
    finally:
        db.close()

    migrated = sync_open(str(path))
    try:
        assert migrated.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            migrated.execute(
                "SELECT outbox_id FROM delivery_observations WHERE observation_id='obs'"
            ).fetchone()[0]
            == "ob"
        )
        for table, column in (
            ("delivery_receipts", "error"),
            ("delivery_observations", "error"),
            ("delivery_outbox", "error_summary"),
            ("durable_ingress_work", "last_error"),
        ):
            assert column not in {
                row[1] for row in migrated.execute(f"PRAGMA table_info({table})")
            }
        rows = migrated.execute(
            "SELECT family, field, diagnostic FROM delivery_evidence_diagnostics"
            " ORDER BY diag_id"
        ).fetchall()
        diagnostics = {(row[0], row[1]): row[2] for row in rows}
        assert diagnostics["outbox", "error_summary"] == ""
        assert diagnostics["receipt", "error"] == "newer-receipt"
        assert diagnostics["ingress", "last_error"] == ""
        assert diagnostics["outbox", "failure_kind_detail"] == "old-kind"
        assert [
            row[2]
            for row in rows
            if row[0] == "outbox" and row[1] == "failure_kind_detail"
        ] == ["previous-attempt", "old-kind"]
        assert diagnostics["observation", "error"] == "old-observation"
        assert diagnostics["receipt", "capability_reason"] == "old-cap"
        assert diagnostics["receipt", "rendering_evidence"] == "old-render"
        assert len(rows) == 8
    finally:
        migrated.close()

    reopened = sync_open(str(path))
    try:
        assert (
            reopened.execute(
                "SELECT COUNT(*) FROM delivery_evidence_diagnostics"
            ).fetchone()[0]
            == 8
        )
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


async def test_queued_predecessor_clear_survives_later_cancellation(tmp_path) -> None:
    """An old inline error must not reappear after queued becomes cancelled."""
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = tmp_path / "queued-before-strip.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(_retired_schema() + _INDEXES)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, lineage, payload,"
            " metadata, created_at) VALUES ('queued-event', 'text', 1, '2026-11-12',"
            " 'fake', 'n', '[]', '{}', '{}', '2026-11-12')"
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, status, attempt_number, failure_kind_detail,"
            " error_summary, created_at, updated_at) VALUES"
            " ('queued-outbox', 'queued-event', 'plan', 'fake', 'queued', 1,"
            " 'old-kind', 'old-error', '2026-11-12', '2026-11-12')"
        )
        db.commit()
    finally:
        db.close()

    storage = SQLiteStorage(str(path))
    await storage.initialize()
    try:
        queued = await storage.get_outbox_item("queued-outbox")
        assert queued is not None
        assert queued.status == "queued"
        assert queued.error_summary is None
        assert await storage.mark_outbox_cancelled("queued-outbox")
        cancelled = await storage.get_outbox_item("queued-outbox")
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.error_summary is None
        assert cancelled.failure_kind_detail is None
    finally:
        await storage.close()

    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT field, diagnostic FROM delivery_evidence_diagnostics"
            " WHERE family='outbox' AND row_id='queued-outbox' AND attempt=1"
            " ORDER BY diag_id"
        ).fetchall()
        assert rows == [
            ("failure_kind_detail", "old-kind"),
            ("error_summary", "old-error"),
            ("failure_kind_detail", ""),
            ("error_summary", ""),
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
