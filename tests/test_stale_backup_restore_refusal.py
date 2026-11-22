"""Never restore a stale backup over newer delivery evidence.

The stopped-state restore tool cannot overwrite a live installation (it
refuses any existing destination), but the documented recovery workflow
restores into a new directory and then switches the installation root. That
switch is where a stale backup can erase newer delivery evidence. The
restore seam therefore accepts an explicit live database and refuses while
its delivery-evidence watermark is newer than the backup's. The watermark
is ``MAX(sequence)`` over ``delivery_receipts`` — the same content-free,
monotone authority the receipt-less handoff settlement fence already uses.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.sqlite.connection import sync_open
from medre.maintenance.stopped_cli import main as stopped_cli_main
from medre.maintenance.stopped_snapshot import (
    SnapshotEntry,
    create_stopped_snapshot,
    read_delivery_evidence_watermark,
    restore_stopped_snapshot,
)

_NOW = "2026-11-20T00:00:00+00:00"


def _add_receipt(db_path: Path, receipt_id: str) -> None:
    """Append one delivered-attempt receipt through the guarded writable open."""
    with closing(sync_open(str(db_path))) as db:
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id,"
            " delivery_plan_id, target_adapter, status, receipt_kind, created_at)"
            " VALUES (?, 'event', 'plan', 'radio', 'sent', 'attempt', ?)",
            (receipt_id, _NOW),
        )
        db.commit()


def _make_core_database(path: Path, *, receipts: int) -> None:
    """Create a current catalog with a small delivery-evidence history."""
    with closing(sync_open(str(path))) as db:
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('event', 'message.text', 1, ?, 'radio', 'radio', ?)",
            (_NOW, _NOW),
        )
        for index in range(receipts):
            db.execute(
                "INSERT INTO delivery_receipts (receipt_id, event_id,"
                " delivery_plan_id, target_adapter, status, receipt_kind,"
                " created_at)"
                " VALUES (?, 'event', 'plan', 'radio', 'failed', 'attempt', ?)",
                (f"receipt-{index}", _NOW),
            )
        db.commit()


def _capture(root: Path, snapshot: Path, *, db_name: str = "medre.sqlite") -> Path:
    """Capture a stopped snapshot of one installation root's core database."""
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / db_name
    if not database.exists():
        _make_core_database(database, receipts=0)
    create_stopped_snapshot(
        root,
        snapshot,
        [SnapshotEntry(f"state/{db_name}", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    return database


def test_watermark_grows_with_receipts_and_never_shrinks(tmp_path: Path) -> None:
    path = tmp_path / "core.sqlite"
    _make_core_database(path, receipts=0)
    assert read_delivery_evidence_watermark(path) == 0
    _add_receipt(path, "receipt-a")
    first = read_delivery_evidence_watermark(path)
    assert first >= 1
    _add_receipt(path, "receipt-b")
    second = read_delivery_evidence_watermark(path)
    assert second > first
    # Deleting the newest receipt must not lower the watermark: sequence
    # identities are AUTOINCREMENT and are never reused.
    with closing(sqlite3.connect(path)) as db:
        db.execute("DELETE FROM delivery_receipts WHERE receipt_id = 'receipt-b'")
        db.commit()
    assert read_delivery_evidence_watermark(path) == second


def test_watermark_is_content_free(tmp_path: Path) -> None:
    """Equal evidence counts yield equal watermarks regardless of content."""
    first = tmp_path / "a.sqlite"
    second = tmp_path / "b.sqlite"
    _make_core_database(first, receipts=3)
    _make_core_database(second, receipts=3)
    assert read_delivery_evidence_watermark(first) == (
        read_delivery_evidence_watermark(second)
    )


def test_watermark_of_non_sqlite_file_fails_closed(tmp_path: Path) -> None:
    impostor = tmp_path / "impostor.sqlite"
    impostor.write_bytes(b"SQLite format 3\x00" + b"not really a database" * 8)
    with pytest.raises(ValueError, match="watermark is unreadable"):
        read_delivery_evidence_watermark(impostor)


def test_restore_refuses_backup_older_than_live(tmp_path: Path) -> None:
    live_root = tmp_path / "live-install"
    live_db = _capture(live_root, tmp_path / "snapshots" / "old")
    # The live installation keeps delivering while the snapshot ages.
    _add_receipt(live_db, "receipt-newer")
    assert read_delivery_evidence_watermark(live_db) > read_delivery_evidence_watermark(
        tmp_path / "snapshots" / "old" / "files" / "state" / "medre.sqlite"
    )
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="refusing to restore a stale backup"):
        restore_stopped_snapshot(
            tmp_path / "snapshots" / "old",
            destination,
            runtime_stopped=True,
            live_database=live_db,
        )
    assert not destination.exists()


def test_restore_proceeds_when_live_is_not_newer(tmp_path: Path) -> None:
    live_root = tmp_path / "live-install"
    live_db = _capture(live_root, tmp_path / "snapshots" / "old")
    destination = tmp_path / "restored"
    restore_stopped_snapshot(
        tmp_path / "snapshots" / "old",
        destination,
        runtime_stopped=True,
        live_database=live_db,
    )
    assert (destination / "state" / "medre.sqlite").is_file()


def test_restore_proceeds_without_a_live_database(tmp_path: Path) -> None:
    live_root = tmp_path / "live-install"
    _capture(live_root, tmp_path / "snapshots" / "old")
    destination = tmp_path / "restored"
    restore_stopped_snapshot(
        tmp_path / "snapshots" / "old",
        destination,
        runtime_stopped=True,
        live_database=tmp_path / "does-not-exist.sqlite",
    )
    assert (destination / "state" / "medre.sqlite").is_file()


def test_restore_refuses_unreadable_live_database(tmp_path: Path) -> None:
    live_root = tmp_path / "live-install"
    _capture(live_root, tmp_path / "snapshots" / "old")
    impostor = tmp_path / "impostor.sqlite"
    impostor.write_bytes(b"SQLite format 3\x00" + b"corrupt" * 16)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="unreadable delivery evidence"):
        restore_stopped_snapshot(
            tmp_path / "snapshots" / "old",
            destination,
            runtime_stopped=True,
            live_database=impostor,
        )
    assert not destination.exists()


def test_stale_restore_cli_reports_refused_and_keeps_exit_convention(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_root = tmp_path / "live-install"
    live_db = _capture(live_root, tmp_path / "snapshots" / "old")
    _add_receipt(live_db, "receipt-newer")
    destination = tmp_path / "restored"
    exit_code = stopped_cli_main(
        [
            "restore",
            "--snapshot",
            str(tmp_path / "snapshots" / "old"),
            "--destination",
            str(destination),
            "--against-live",
            str(live_db),
            "--stopped",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["operation"] == "restore"
    assert payload["status"] == "refused"
    assert payload["error_type"] == "ValueError"
    assert not destination.exists()


def test_current_restore_cli_succeeds_against_not_newer_live(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_root = tmp_path / "live-install"
    live_db = _capture(live_root, tmp_path / "snapshots" / "old")
    destination = tmp_path / "restored"
    exit_code = stopped_cli_main(
        [
            "restore",
            "--snapshot",
            str(tmp_path / "snapshots" / "old"),
            "--destination",
            str(destination),
            "--against-live",
            str(live_db),
            "--stopped",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["operation"] == "restore"
    assert payload["status"] == "verified"
    assert (destination / "state" / "medre.sqlite").is_file()


def test_capture_rejects_existing_destination_unaffected(tmp_path: Path) -> None:
    """The structural refusal of live-destination restore remains in force."""
    live_root = tmp_path / "live-install"
    live_db = _capture(live_root, tmp_path / "snapshots" / "old")
    destination = tmp_path / "not-absent"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        restore_stopped_snapshot(
            tmp_path / "snapshots" / "old",
            destination,
            runtime_stopped=True,
        )
    assert live_db.exists()
