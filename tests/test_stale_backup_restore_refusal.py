"""Never restore a stale or divergent backup over existing delivery evidence.

The stopped-state restore tool cannot overwrite a live installation (it
refuses any existing destination), but the documented recovery workflow
restores into a new directory and then switches the installation root. That
switch is where a stale backup can erase newer delivery evidence, and where
an equal receipt watermark can still conceal a divergent history: different
branches can allocate the same receipt sequence to different records, and
operator decisions need not allocate receipts at all. The restore seam
therefore accepts an explicit live database and refuses unless the verified
staged database carries at least the live delivery-evidence watermark AND an
identical logical catalog and row history. The watermark is the
``sqlite_sequence`` high-water of ``delivery_receipts`` — the same
content-free, monotone authority the receipt-less handoff settlement fence
already uses. Physical-only maintenance on the live database (``VACUUM``,
``ANALYZE``) preserves logical state and must not refuse.
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


def test_equal_receipt_watermarks_do_not_prove_same_history(tmp_path: Path) -> None:
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    _add_receipt(live, "receipt-a")
    snapshot = tmp_path / "snapshot-current"
    create_stopped_snapshot(
        root,
        snapshot,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    with closing(sqlite3.connect(live)) as db:
        db.execute(
            "UPDATE delivery_receipts SET receipt_id='receipt-b' "
            "WHERE receipt_id='receipt-a'"
        )
        db.commit()
    backup = snapshot / "files" / "state" / "medre.sqlite"
    assert read_delivery_evidence_watermark(live) == read_delivery_evidence_watermark(
        backup
    )
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="divergent backup"):
        restore_stopped_snapshot(
            snapshot, destination, runtime_stopped=True, live_database=live
        )
    assert not destination.exists()


def test_receiptless_delivery_state_change_refuses_restore(tmp_path: Path) -> None:
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "initial-snapshot")
    with closing(sqlite3.connect(live)) as db:
        db.execute(
            "INSERT INTO delivery_outbox "
            "(outbox_id,event_id,delivery_plan_id,target_adapter,created_at,updated_at) "
            "VALUES ('pending-work','event','plan','radio',?,?)",
            (_NOW, _NOW),
        )
        db.commit()
    snapshot = tmp_path / "snapshot-current"
    create_stopped_snapshot(
        root,
        snapshot,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    with closing(sqlite3.connect(live)) as db:
        db.execute(
            "UPDATE delivery_outbox SET status='queued', updated_at=? "
            "WHERE outbox_id='pending-work'",
            ("2026-11-21T01:00:00+00:00",),
        )
        db.commit()
    assert read_delivery_evidence_watermark(live) == 0
    with pytest.raises(ValueError, match="divergent backup"):
        restore_stopped_snapshot(
            snapshot, tmp_path / "restored", runtime_stopped=True, live_database=live
        )
    assert not (tmp_path / "restored").exists()


def test_restore_compares_the_verified_staged_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.maintenance import stopped_snapshot as subject

    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    checked: list[Path] = []
    original = subject.read_delivery_evidence_watermark

    def recording_watermark(path: Path) -> int:
        checked.append(Path(path))
        return original(path)

    monkeypatch.setattr(
        subject, "read_delivery_evidence_watermark", recording_watermark
    )
    restore_stopped_snapshot(
        tmp_path / "snapshot",
        tmp_path / "restored",
        runtime_stopped=True,
        live_database=live,
    )
    assert any(
        path.parent.parent.name.startswith(".medre-restore-") for path in checked
    )
    assert not any("snapshot/files" in str(path) for path in checked)


def _restore_refusal_message(snapshot: Path, destination: Path, live: Path) -> str:
    """Run a refused restore and return the ValueError text for inspection."""
    with pytest.raises(ValueError, match="divergent backup") as exc_info:
        restore_stopped_snapshot(
            snapshot, destination, runtime_stopped=True, live_database=live
        )
    return str(exc_info.value)


def test_physical_only_maintenance_still_restores(tmp_path: Path) -> None:
    """VACUUM and ANALYZE change layout, not logical state; they must not refuse."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    with closing(sqlite3.connect(live)) as db:
        db.execute("VACUUM")
    with closing(sqlite3.connect(live)) as db:
        db.execute("ANALYZE")
    destination = tmp_path / "restored"
    restore_stopped_snapshot(
        tmp_path / "snapshot", destination, runtime_stopped=True, live_database=live
    )
    assert (destination / "state" / "medre.sqlite").is_file()


def test_ceiling_purge_log_row_refuses_restore(tmp_path: Path) -> None:
    """A receiptless ceiling-enforcement log row is divergent evidence."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    with closing(sync_open(str(live))) as db:
        db.execute(
            "INSERT INTO event_content_ceiling_log (event_id, enforced_at,"
            " ceiling, rows_removed, logical_bytes_removed, blobs_removed,"
            " blob_bytes_removed)"
            " VALUES ('event', ?, 'relay-only', 1, 10, 0, 0)",
            (_NOW,),
        )
        db.commit()
    assert read_delivery_evidence_watermark(live) == 0
    message = _restore_refusal_message(
        tmp_path / "snapshot", tmp_path / "restored", live
    )
    assert (
        "row counts differ for: event_content_ceiling_log (staged rows 0,"
        " live rows 1)"
    ) in message


def test_operator_decision_row_refuses_restore(tmp_path: Path) -> None:
    """A receiptless operator decision changes logical state without receipts."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    with closing(sync_open(str(live))) as db:
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, created_at, updated_at)"
            " VALUES ('work-1', 'event', 'plan', 'radio', ?, ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO operator_delivery_decisions (decision_id, actor_id,"
            " event_id, delivery_plan_id, target_adapter, outbox_id,"
            " observed_generation, observed_updated_at, observed_status, action,"
            " recorded_at)"
            " VALUES ('decision-1', 'operator', 'event', 'plan', 'radio',"
            " 'work-1', 0, ?, 'pending', 'resolve_uncertain', ?)",
            (_NOW, _NOW),
        )
        db.commit()
    assert read_delivery_evidence_watermark(live) == 0
    message = _restore_refusal_message(
        tmp_path / "snapshot", tmp_path / "restored", live
    )
    assert "row counts differ for:" in message
    assert "operator_delivery_decisions" in message
    assert "delivery_outbox" in message
    # The breakdown stays content-free: identities and decisions never appear.
    assert "decision-1" not in message
    assert "resolve_uncertain" not in message


def test_equal_count_row_rewrite_reports_equal_count_divergence(
    tmp_path: Path,
) -> None:
    """Same row counts, different rows: named, counted, never valued."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    _add_receipt(live, "receipt-a")
    snapshot = tmp_path / "snapshot-current"
    create_stopped_snapshot(
        root,
        snapshot,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    with closing(sqlite3.connect(live)) as db:
        db.execute(
            "UPDATE delivery_receipts SET receipt_id='receipt-b' "
            "WHERE receipt_id='receipt-a'"
        )
        db.commit()
    assert read_delivery_evidence_watermark(live) == read_delivery_evidence_watermark(
        snapshot / "files" / "state" / "medre.sqlite"
    )
    message = _restore_refusal_message(snapshot, tmp_path / "restored", live)
    assert "equal row counts but different rows for: delivery_receipts" in message
    assert "receipt-a" not in message
    assert "receipt-b" not in message


def test_newer_backup_watermark_refuses_restore(tmp_path: Path) -> None:
    """A strictly newer backup against an older live database is also divergent."""
    newer_root = tmp_path / "newer-install"
    newer_db = _capture(newer_root, tmp_path / "initial-snapshot")
    _add_receipt(newer_db, "receipt-newer")
    snapshot = tmp_path / "newer-snapshot"
    create_stopped_snapshot(
        newer_root,
        snapshot,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    rolled_back = tmp_path / "rolled-back" / "state" / "medre.sqlite"
    rolled_back.parent.mkdir(parents=True)
    _make_core_database(rolled_back, receipts=0)
    assert read_delivery_evidence_watermark(rolled_back) == 0
    assert (
        read_delivery_evidence_watermark(snapshot / "files" / "state" / "medre.sqlite")
        > 0
    )
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="divergent backup"):
        restore_stopped_snapshot(
            snapshot, destination, runtime_stopped=True, live_database=rolled_back
        )
    assert not destination.exists()


def test_different_schema_catalog_refuses_restore(tmp_path: Path) -> None:
    """A live database with a different catalog is refused and named."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    with closing(sqlite3.connect(live)) as db:
        db.execute("CREATE TABLE operator_notes (note_id TEXT PRIMARY KEY)")
        db.commit()
    assert read_delivery_evidence_watermark(live) == 0
    message = _restore_refusal_message(
        tmp_path / "snapshot", tmp_path / "restored", live
    )
    assert "tables present only in the live database: operator_notes" in message


def test_refusal_leaves_no_staging_residue(tmp_path: Path) -> None:
    """Every refusal cleans the staging tree and never touches the destination."""
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    _add_receipt(live, "receipt-newer")
    destination = tmp_path / "restored"
    with pytest.raises(ValueError):
        restore_stopped_snapshot(
            tmp_path / "snapshot",
            destination,
            runtime_stopped=True,
            live_database=live,
        )
    assert not destination.exists()
    assert not [
        entry
        for entry in tmp_path.iterdir()
        if entry.name.startswith(".medre-restore-")
    ]


def test_passing_restore_publishes_exactly_the_snapshot_and_spares_live(
    tmp_path: Path,
) -> None:
    """A passing restore publishes the manifest files only and never writes live.

    A read-only open of the WAL-mode live database may materialise empty
    ``-wal``/``-shm`` sidecars; the main database file and every pre-existing
    file must remain byte-identical, and nothing else may appear.
    """
    root = tmp_path / "live-install"
    live = _capture(root, tmp_path / "snapshot")
    live_bytes = live.read_bytes()
    before = sorted(path.name for path in live.parent.iterdir())
    before_bytes = {
        path.name: path.read_bytes()
        for path in live.parent.iterdir()
        if path.name != live.name
    }
    destination = tmp_path / "restored"
    restore_stopped_snapshot(
        tmp_path / "snapshot", destination, runtime_stopped=True, live_database=live
    )
    assert live.read_bytes() == live_bytes
    after = sorted(path.name for path in live.parent.iterdir())
    sidecar_names = {live.name + suffix for suffix in ("-wal", "-shm", "-journal")}
    assert set(after) - set(before) <= sidecar_names
    assert not (set(before) - set(after))
    for name, content in before_bytes.items():
        assert (live.parent / name).read_bytes() == content
    snapshot_files = {
        path.relative_to(tmp_path / "snapshot" / "files").as_posix(): path
        for path in (tmp_path / "snapshot" / "files").rglob("*")
        if path.is_file()
    }
    destination_files = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert set(destination_files) == set(snapshot_files)
    for name, source in snapshot_files.items():
        assert destination_files[name].read_bytes() == source.read_bytes()
    assert not [
        name
        for name in destination_files
        if name.endswith(("-wal", "-shm", "-journal"))
    ]
