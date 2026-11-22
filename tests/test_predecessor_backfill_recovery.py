"""A failed upgrade reopens cleanly and converges without duplicate backfill.

After an injected migration failure rolls back, the same database must open
on the next attempt with the current catalog and exactly the legacy
diagnostic rows, and a further open must not duplicate them.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import medre.core.storage.sqlite._prerelease_migrate as prerelease_migrate
from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.schema import _INDEXES
from medre.core.storage.sqlite.schema_validation import validate_schema_definition

_NOW = "2026-11-20T00:00:00+00:00"

_EXPECTED_DIAGNOSTICS = sorted(
    [
        ("receipt", "receipt-1", 1, "error", "radio timeout before acceptance"),
        ("observation", "obs-1", 1, "error", "channel closed mid-scan"),
        ("outbox", "work-1", 1, "failure_kind_detail", "uncertain_native_handoff"),
        ("outbox", "work-2", 1, "error_summary", "delivered after one resend"),
        ("ingress", "event-1", 1, "last_error", "transient adapter reconnect"),
    ]
)


def _build_stripped_predecessor(path: Path) -> None:
    predecessor = prerelease_migrate._stripped_diagnostic_predecessor_schema()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(predecessor + _INDEXES)
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        for event_id in ("event-1", "event-2"):
            db.execute(
                "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
                " timestamp, source_adapter, source_transport_id, created_at)"
                " VALUES (?, 'message.text', 1, ?, 'radio', 'radio', ?)",
                (event_id, _NOW, _NOW),
            )
            db.execute(
                "INSERT INTO durable_ingress_work (event_id, provenance, status,"
                " attempts, created_at, updated_at, last_error)"
                " VALUES (?, 'radio', 'completed', 1, ?, ?, ?)",
                (
                    event_id,
                    _NOW,
                    _NOW,
                    "transient adapter reconnect" if event_id == "event-1" else None,
                ),
            )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, status, receipt_id, created_at,"
            " updated_at, failure_kind_detail, error_summary)"
            " VALUES ('work-1', 'event-1', 'plan', 'radio', 1, 'retry_wait',"
            " 'receipt-1', ?, ?, ?, NULL)",
            (_NOW, _NOW, "uncertain_native_handoff"),
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, status, receipt_id, created_at,"
            " updated_at, failure_kind_detail, error_summary)"
            " VALUES ('work-2', 'event-2', 'plan', 'radio', 1, 'sent',"
            " 'receipt-2', ?, ?, NULL, ?)",
            (_NOW, _NOW, "delivered after one resend"),
        )
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id, delivery_plan_id,"
            " target_adapter, status, receipt_kind, attempt_number, outbox_id,"
            " created_at, error) VALUES ('receipt-1', 'event-1', 'plan', 'radio',"
            " 'failed', 'attempt', 1, 'work-1', ?, ?)",
            (_NOW, "radio timeout before acceptance"),
        )
        db.execute(
            "INSERT INTO delivery_observations (observation_id, event_id,"
            " delivery_plan_id, target_adapter, outbox_id, attempt_number,"
            " adapter_message_id, state, confirmation_level, metadata, observed_at,"
            " error) VALUES ('obs-1', 'event-1', 'plan', 'radio', 'work-1', 1,"
            " 'nm-1', 'failed', 'unknown', '{}', ?, ?)",
            (_NOW, "channel closed mid-scan"),
        )


def _diagnostic_rows(db: sqlite3.Connection) -> list[tuple[str, str, int, str, str]]:
    return sorted(
        (str(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]))
        for r in db.execute(
            "SELECT family, row_id, attempt, field, diagnostic"
            " FROM delivery_evidence_diagnostics WHERE diagnostic <> ''"
        )
    )


def test_failed_upgrade_reopens_with_exact_backfill_and_no_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "stripped.sqlite"
    _build_stripped_predecessor(path)
    before = path.read_bytes()

    def fail_view_recreation(db: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected view recreation failure")

    monkeypatch.setattr(
        prerelease_migrate, "_recreate_delivery_status_view", fail_view_recreation
    )
    with pytest.raises(sqlite3.OperationalError, match="injected view recreation"):
        with closing(sync_open(str(path))):
            pass
    monkeypatch.undo()

    # First successful reopen: the rollback left the original bytes, and the
    # migration now completes with the exact legacy diagnostics moved once.
    assert path.read_bytes() == before
    with closing(sync_open(str(path))) as db:
        assert validate_schema_definition(db, str(path))
        assert _diagnostic_rows(db) == _EXPECTED_DIAGNOSTICS
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(delivery_receipts)")
        }
        assert "error" not in columns
        first_rows = sorted(
            tuple(row)
            for row in db.execute("SELECT * FROM delivery_evidence_diagnostics")
        )
    with closing(sqlite3.connect(path)) as raw:
        second_rows = sorted(
            tuple(row)
            for row in raw.execute("SELECT * FROM delivery_evidence_diagnostics")
        )
    assert second_rows == first_rows
