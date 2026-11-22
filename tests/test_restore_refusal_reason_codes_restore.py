"""Restore refusals report a fixed, content-free reason code.

Every refusal that is MEDRE's own validation on the ``restore`` path carries
a ``reason`` identifier from the closed vocabulary in
``medre.maintenance.stopped_snapshot``. The code is a fixed literal: it never
carries a path, file name, identifier, timestamp, size, count, or OS text.
Exceptions without a code from that vocabulary, such as an OS error, keep
printing the bare error type only, so private-looking paths stay out of the
operator output.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.sqlite.connection import sync_open
from medre.maintenance import stopped_snapshot
from medre.maintenance.stopped_cli import main as stopped_cli_main
from medre.maintenance.stopped_snapshot import (
    STOPPED_REFUSAL_REASONS,
    SnapshotEntry,
    StoppedStateDestinationRefused,
    StoppedStateRefused,
    create_stopped_snapshot,
    restore_stopped_snapshot,
)

_NOW = "2026-11-20T00:00:00+00:00"
_PRIVATE_PATH = "/srv/private/native-profile/identity.key"
_OPERATOR_PAGE = (
    Path(__file__).resolve().parents[1] / "docs" / "ops" / "stopped-state-snapshots.md"
)
_REFUSAL_CLASSES = frozenset({"StoppedStateRefused", "StoppedStateDestinationRefused"})


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


def _capture(root: Path, snapshot: Path, *, receipts: int = 0) -> Path:
    """Capture a stopped snapshot of one installation root's core database."""
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "medre.sqlite"
    _make_core_database(database, receipts=receipts)
    create_stopped_snapshot(
        root,
        snapshot,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )
    return database


def _add_newer_receipt(live: Path) -> None:
    """Record one delivery in the live database after the snapshot was taken."""
    with closing(sync_open(str(live))) as db:
        db.execute(
            "INSERT INTO delivery_receipts (receipt_id, event_id,"
            " delivery_plan_id, target_adapter, status, receipt_kind, created_at)"
            " VALUES ('receipt-newer', 'event', 'plan', 'radio', 'sent',"
            " 'attempt', ?)",
            (_NOW,),
        )
        db.commit()


def _restore(
    snapshot: Path,
    destination: Path,
    *options: str,
) -> int:
    return stopped_cli_main(
        [
            "restore",
            "--snapshot",
            str(snapshot),
            "--destination",
            str(destination),
            "--stopped",
            *options,
        ]
    )


def _refused_payload(
    capsys: pytest.CaptureFixture[str], exit_code: int
) -> dict[str, object]:
    """Return the single refused JSON line printed on stdout."""
    assert exit_code == 1
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


def _operator_page() -> str:
    return _OPERATOR_PAGE.read_text(encoding="utf-8")


def test_restore_cli_reports_runtime_not_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    destination = tmp_path / "restored"
    exit_code = stopped_cli_main(
        [
            "restore",
            "--snapshot",
            str(snapshot_dir),
            "--destination",
            str(destination),
        ]
    )
    payload = _refused_payload(capsys, exit_code)
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateRefused",
        "reason": "runtime_not_stopped",
    }
    assert not destination.exists()


def test_restore_cli_reports_destination_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    destination = tmp_path / "restored"
    destination.mkdir()
    payload = _refused_payload(capsys, _restore(snapshot_dir, destination))
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateDestinationRefused",
        "reason": "destination_exists",
    }


def test_restore_cli_reports_checksum_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    stored = snapshot_dir / "files" / "state" / "medre.sqlite"
    stored.write_bytes(b"tampered-after-capture" * 8)
    destination = tmp_path / "restored"
    payload = _refused_payload(capsys, _restore(snapshot_dir, destination))
    assert payload["reason"] == "checksum_mismatch"
    assert not destination.exists()


def test_restore_cli_reports_unsupported_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    manifest = snapshot_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"format": "unrelated-format", "version": 1, "files": []},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "restored"
    payload = _refused_payload(capsys, _restore(snapshot_dir, destination))
    assert payload["reason"] == "unsupported_manifest"
    assert not destination.exists()


def test_restore_cli_reports_missing_or_unexpected_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    (snapshot_dir / "files" / "extra.bin").write_bytes(b"unlisted")
    destination = tmp_path / "restored"
    payload = _refused_payload(capsys, _restore(snapshot_dir, destination))
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateRefused",
        "reason": "missing_or_unexpected_files",
    }
    assert not destination.exists()


def test_restore_cli_reports_newer_live_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    live = _capture(tmp_path / "live-install", snapshot_dir)
    _add_newer_receipt(live)
    destination = tmp_path / "restored"
    payload = _refused_payload(
        capsys, _restore(snapshot_dir, destination, "--against-live", str(live))
    )
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateRefused",
        "reason": "newer_live_evidence",
    }
    assert not destination.exists()


def test_restore_cli_reports_divergent_live_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "live-install"
    snapshot_dir = tmp_path / "snapshot"
    live = _capture(root, snapshot_dir, receipts=1)
    with closing(sqlite3.connect(live)) as db:
        db.execute("UPDATE delivery_receipts SET receipt_id='receipt-renamed'")
        db.commit()
    destination = tmp_path / "restored"
    payload = _refused_payload(
        capsys, _restore(snapshot_dir, destination, "--against-live", str(live))
    )
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateRefused",
        "reason": "live_state_diverged",
    }
    assert not destination.exists()


def test_restore_cli_reports_unreadable_live_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture(tmp_path / "live-install", tmp_path / "snapshot")
    impostor = tmp_path / "impostor.sqlite"
    impostor.write_bytes(b"SQLite format 3\x00" + b"corrupt" * 16)
    destination = tmp_path / "restored"
    payload = _refused_payload(
        capsys,
        _restore(tmp_path / "snapshot", destination, "--against-live", str(impostor)),
    )
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "StoppedStateRefused",
        "reason": "live_evidence_unreadable",
    }
    assert not destination.exists()


def _documented_refused_example() -> dict[str, object]:
    """The refused output example shown on the operator page."""
    match = re.search(
        r'```json\n(\{\s*"operation": "restore".*?)\n```',
        _operator_page(),
        re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


def test_documented_refused_example_is_the_cli_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator page's refused example carries the CLI's keys and values."""
    snapshot_dir = tmp_path / "snapshot"
    live = _capture(tmp_path / "live-install", snapshot_dir)
    _add_newer_receipt(live)
    exit_code = _restore(
        snapshot_dir, tmp_path / "restored", "--against-live", str(live)
    )
    assert exit_code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    documented = _documented_refused_example()
    assert payload == documented
    assert list(payload) == list(documented)


def test_documented_reason_table_lists_the_closed_vocabulary() -> None:
    """Every code in the page's reason table is a listed code, and only those."""
    documented = {
        code
        for line in _operator_page().splitlines()
        if line.startswith("|")
        for code in re.findall(r"`([a-z][a-z0-9_]+)`", line)
    }
    assert documented == STOPPED_REFUSAL_REASONS


def test_every_reason_code_is_raised_and_every_raised_code_is_listed() -> None:
    """The vocabulary and the library's raise sites name the same codes."""
    raised: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(stopped_snapshot))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _REFUSAL_CLASSES
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason":
                assert isinstance(keyword.value, ast.Constant)
                assert isinstance(keyword.value.value, str)
                raised.add(keyword.value.value)
    assert raised == STOPPED_REFUSAL_REASONS


def _restore_with_failing_copy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> tuple[dict[str, object], str]:
    """Restore a snapshot whose file copy raises ``error``.

    Returns the refused payload and everything the command printed.
    """
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)

    def failing_copy(source: Path, target: Path) -> None:
        raise error

    monkeypatch.setattr(stopped_snapshot, "_copy_regular", failing_copy)
    destination = tmp_path / "restored"
    exit_code = _restore(snapshot_dir, destination)
    captured = capsys.readouterr()
    assert not destination.exists()
    assert exit_code == 1
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.out + captured.err


def test_restore_cli_os_error_keeps_bare_error_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OS error prints no reason and no private path."""
    payload, printed = _restore_with_failing_copy(
        tmp_path, capsys, monkeypatch, OSError(f"cannot read {_PRIVATE_PATH}")
    )
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "OSError",
    }
    assert _PRIVATE_PATH not in printed


def test_restore_cli_foreign_reason_attribute_prints_no_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason outside the closed vocabulary is never printed."""

    class _ForeignRefusal(OSError):
        reason = _PRIVATE_PATH

    payload, printed = _restore_with_failing_copy(
        tmp_path, capsys, monkeypatch, _ForeignRefusal("unreadable input")
    )
    assert payload == {
        "operation": "restore",
        "status": "refused",
        "error_type": "_ForeignRefusal",
    }
    assert _PRIVATE_PATH not in printed


def test_destination_refusal_keeps_file_exists_error_type(tmp_path: Path) -> None:
    """Library callers still observe FileExistsError for an existing target."""
    snapshot_dir = tmp_path / "snapshot"
    _capture(tmp_path / "install", snapshot_dir)
    destination = tmp_path / "restored"
    destination.mkdir()
    with pytest.raises(FileExistsError) as exc_info:
        restore_stopped_snapshot(snapshot_dir, destination, runtime_stopped=True)
    refusal = exc_info.value
    assert isinstance(refusal, StoppedStateDestinationRefused)
    assert refusal.reason == "destination_exists"
    assert str(refusal) == "restore requires an absent destination directory"


def test_reason_vocabulary_is_closed_and_snake_case() -> None:
    """Every reason is a fixed snake_case literal; unknown codes are refused."""
    assert isinstance(STOPPED_REFUSAL_REASONS, frozenset)
    assert STOPPED_REFUSAL_REASONS
    for reason in STOPPED_REFUSAL_REASONS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", reason)
    with pytest.raises(ValueError, match="closed vocabulary"):
        StoppedStateRefused("unrelated", reason="not_in_vocabulary")
    with pytest.raises(ValueError, match="closed vocabulary"):
        StoppedStateDestinationRefused("unrelated", reason="not_in_vocabulary")


def test_reason_carrier_subclasses_preserve_library_contract() -> None:
    """Reason-carrying refusals stay ValueError/FileExistsError instances."""
    refusal = StoppedStateRefused(
        "stop the runtime and native workers before restore",
        reason="runtime_not_stopped",
    )
    assert isinstance(refusal, ValueError)
    assert refusal.reason == "runtime_not_stopped"
    assert str(refusal) == "stop the runtime and native workers before restore"
    destination_refusal = StoppedStateDestinationRefused(
        "restore requires an absent destination directory",
        reason="destination_exists",
    )
    assert isinstance(destination_refusal, FileExistsError)
    assert not isinstance(destination_refusal, ValueError)
