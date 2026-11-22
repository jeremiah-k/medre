"""Populated-predecessor upgrade preserves the predecessor's own rows.

The first open must not silently turn a data loss into the upgrade baseline:
the predecessor state is captured before any migration runs and every seeded
row is compared afterwards, so a dropped attachment association, receipt,
observation, or native reference cannot pass as the new baseline. An upgraded
database also still refuses orphaned evidence with the extended foreign-key
code.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.schema_validation import (
    _expected_definitions,
    validate_schema_definition,
)

_NOW = "2026-11-20T00:00:00+00:00"
_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"

_LEGACY_DIAGNOSTICS = (
    ("receipt", "receipt-1", 1, "error", "radio timeout before native acceptance"),
    ("observation", "obs-1", 1, "error", "observation channel closed mid-scan"),
    ("outbox", "work-1", 1, "failure_kind_detail", "uncertain_native_handoff"),
    ("outbox", "work-2", 1, "error_summary", "delivered after one resend"),
    ("ingress", "event-1", 1, "last_error", "transient adapter reconnect"),
)


def _build_predecessor(path: Path) -> None:
    ddl = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(ddl).hexdigest() == _FIXTURE_SHA256
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl.decode("utf-8"))
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
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, status, receipt_id, created_at,"
            " updated_at) VALUES ('work-1', 'event-1', 'plan', 'radio', 1,"
            " 'retry_wait', 'receipt-1', ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, status, receipt_id, created_at,"
            " updated_at) VALUES ('work-2', 'event-2', 'plan', 'radio', 1,"
            " 'sent', 'receipt-2', ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id, delivery_plan_id,"
            " target_adapter, status, receipt_kind, attempt_number, outbox_id,"
            " created_at) VALUES ('receipt-1', 'event-1', 'plan', 'radio',"
            " 'failed', 'attempt', 1, 'work-1', ?)",
            (_NOW,),
        )
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id, delivery_plan_id,"
            " target_adapter, status, receipt_kind, attempt_number, outbox_id,"
            " created_at) VALUES ('receipt-2', 'event-2', 'plan', 'radio',"
            " 'sent', 'attempt', 1, 'work-2', ?)",
            (_NOW,),
        )
        db.execute(
            "INSERT INTO delivery_observations (observation_id, event_id,"
            " delivery_plan_id, target_adapter, outbox_id, attempt_number,"
            " adapter_message_id, state, confirmation_level, metadata, observed_at)"
            " VALUES ('obs-1', 'event-1', 'plan', 'radio', 'work-1', 1, 'nm-1',"
            " 'failed', 'unknown', '{}', ?)",
            (_NOW,),
        )
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
        for family, row_id, attempt, field, diagnostic in _LEGACY_DIAGNOSTICS:
            db.execute(
                "INSERT INTO delivery_evidence_diagnostics (family, row_id,"
                " attempt, field, diagnostic, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (family, row_id, attempt, field, diagnostic, _NOW),
            )


def _snapshot(
    path: Path,
) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    with closing(sqlite3.connect(path)) as db:
        catalog = [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
                " ORDER BY type,name"
            )
        ]
        tables = {
            str(name): [
                tuple(row)
                for row in db.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'  # nosec B608 # Catalog-enumerated identifier.
                )
            ]
            for (kind, name, _sql) in catalog
            if kind == "table"
        }
        assert list(db.execute("PRAGMA foreign_key_check")) == []
    return catalog, tables


def test_first_open_preserves_every_predecessor_row(tmp_path: Path) -> None:
    path = tmp_path / "predecessor.sqlite"
    _build_predecessor(path)
    before_catalog, before_tables = _snapshot(path)

    with closing(sync_open(str(path))) as db:
        assert validate_schema_definition(db, str(path))
        after_catalog, _after_tables = _snapshot(path)

    after_names = {(str(kind), str(name)) for kind, name, _sql in after_catalog}
    for kind, name, _sql in before_catalog:
        assert (str(kind), str(name)) in after_names, f"lost object {kind} {name}"

    with closing(sqlite3.connect(path)) as db:
        for table, rows in before_tables.items():
            after_rows = [
                tuple(row)
                for row in db.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'  # nosec B608 # Predecessor-owned names.
                )
            ]
            assert after_rows == rows, f"rows changed in {table}"
        association = list(
            db.execute(
                "SELECT event_id, content_ref, created_at"
                " FROM event_attachment_associations"
            )
        )
    assert len(association) == 1
    assert association[0][0] == "event-1"

    expected_catalog = sorted(
        (kind, name, definition)
        for (kind, name), definition in _expected_definitions().items()
    )
    assert after_catalog == expected_catalog


def test_upgraded_database_refuses_orphans_with_fk_code(tmp_path: Path) -> None:
    path = tmp_path / "predecessor.sqlite"
    _build_predecessor(path)
    with closing(sync_open(str(path))) as db:
        assert int(db.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        try:
            db.execute(
                "INSERT INTO delivery_receipts (receipt_id, event_id,"
                " delivery_plan_id, target_adapter, status, receipt_kind,"
                " attempt_number, outbox_id, created_at)"
                " VALUES ('orphan', 'missing-event', 'plan', 'radio', 'failed',"
                " 'attempt', 1, 'work-1', ?)",
                (_NOW,),
            )
            raise AssertionError("orphan receipt was accepted")
        except sqlite3.IntegrityError as exc:
            assert exc.sqlite_errorcode == 787
            assert exc.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
