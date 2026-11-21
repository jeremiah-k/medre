# medre-test-tier: compatibility
"""Operator CLI tests for the hard content-privacy ceiling audit.

Core-only storage lifecycle and audit regressions live in
``test_content_ceiling_audit_storage.py``; these tests exercise the actual
CLI, its read-only behavior and its JSON/exit-code contract:

- ``audit_content_ceilings`` / ``medre storage ceiling audit`` walk the
  residual battery for elapsed ceilings and report per-store residuals
  without purging anything, flagging the documented late-write case.
- ``verify_content_ceiling_receipts`` / ``medre storage ceiling verify``
  re-run the battery over the enforcement receipts and report pass/fail
  per receipt with the receipt's counters.

Assertions: zero residuals per store after the sweep, receipt counters
matching the sweep summaries, content unreachable through every read path
(including the quoting event's excerpt of the expired event), shared blob
bytes surviving for their other referent until the last reference goes,
late-write residuals flagged by the audit and cleared by the next sweep,
stable idempotent audit re-runs, strictly read-only CLI behavior, and
scriptable exit codes. Privacy placeholders only: every seeded value is
synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medre.cli.exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_FAILED
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _BLOB_REF,
    _admit_with_ceiling,
    _associate_blob,
    _insert_blob,
    _read_ceiling,
    _seed_every_copy,
    _seed_late_write,
)


def _run_cli(*argv: str) -> tuple[int, str, str]:
    """Run one CLI invocation, capturing stdout/stderr and the exit code."""
    from medre.cli.main import main

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = 0
        try:
            main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _parse_cli_json(stdout: str) -> dict:
    return json.loads(stdout)


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


async def _prepare_database(db_path: str, *, with_late_write: bool = False) -> str:
    """Build a lifecycle database through the real storage backend.

    Admission refuses a deadline in the past, so the stored ceiling is
    moved behind the admission instant afterwards: the CLI scenarios run
    against the store's real clock, and the moved ceiling makes the event
    honestly elapsed by then, exactly as after the passage of time.
    """
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        event_id = "ceiling-cli-full"
        await _admit_with_ceiling(storage, event_id)
        outbox_id = await _seed_every_copy(storage, event_id)
        # Staging requires live content authority. Model elapsed time only
        # after all sample writes; otherwise the test cannot prove a sweep.
        await storage._write(
            "UPDATE event_content_access_policies SET hard_ceiling=? "
            "WHERE event_id=?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(
                    timespec="microseconds"
                ),
                event_id,
            ),
        )
        # One unshared attachment so the receipt carries blob counters.
        await _insert_blob(storage, _BLOB_REF)
        await _associate_blob(storage, event_id, _BLOB_REF)
        ceiling = await _read_ceiling(storage, event_id)
        await storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
        if with_late_write:
            await _seed_late_write(storage, event_id, outbox_id)
    finally:
        await storage.close()
    return db_path


async def _create_empty_database(db_path: str) -> None:
    """Create a valid, initialized, empty storage database."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    await storage.close()


def test_cli_audit_and_verify_on_clean_store(tmp_path: Path) -> None:
    """Both commands emit their content-free JSON and exit 0 on a clean store."""
    db_path = str(tmp_path / "clean.db")
    asyncio.run(_create_empty_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    report = _parse_cli_json(stdout)
    assert report["format"] == "medre-content-ceiling-audit"
    assert report["mode"] == "audit"
    assert report["purged"] is False
    assert report["physical_erasure_claimed"] is False
    assert report["elapsed_pending"] == []
    assert report["residual_enforced"] == []
    assert report["receipts"] == 0

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    report = _parse_cli_json(stdout)
    assert report["format"] == "medre-content-ceiling-verify"
    assert report["mode"] == "verify"
    assert report["verdict"] == "passed"
    assert report["receipts_examined"] == 0


def test_cli_audit_and_verify_prove_purged_lifecycle(tmp_path: Path) -> None:
    """On a swept database the CLI proof matches the storage-level contract."""
    db_path = str(tmp_path / "lifecycle.db")
    asyncio.run(_prepare_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert audit["elapsed_pending"] == []
    assert audit["residual_enforced"] == []
    assert audit["receipts"] == 1

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "passed"
    assert verify["receipts_passed"] == 1
    receipt = verify["receipts"][0]
    assert receipt["event_id"] == "ceiling-cli-full"
    assert receipt["rows_removed"] > 0
    assert receipt["logical_bytes_removed"] > 0
    assert receipt["blobs_removed"] == 1
    assert receipt["blob_bytes_removed"] == len(b"ATTACHDATA")


def test_cli_verify_exits_failed_on_residual_and_flags(tmp_path: Path) -> None:
    """A late-write residual makes verify exit 1; the audit flags the store."""
    db_path = str(tmp_path / "late.db")
    asyncio.run(_prepare_database(db_path, with_late_write=True))

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert len(audit["residual_enforced"]) == 1
    finding = audit["residual_enforced"][0]
    assert finding["event_id"] == "ceiling-cli-full"
    assert finding["residual_stores"] == {
        "delivery_observations": 1,
        "delivery_evidence_diagnostics": 1,
    }

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == EXIT_FAILED, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "failed"
    assert verify["receipts_failed"] == 1

    # Remediation is the sweep's job; the CLI never mutates anything.

    async def _sweep() -> list:
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        try:
            return await storage.enforce_content_ceilings()
        finally:
            await storage.close()

    followup = asyncio.run(_sweep())
    assert len(followup) == 1
    assert followup[0].residual_pass is True

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    verify = _parse_cli_json(stdout)
    assert verify["verdict"] == "passed"


def test_cli_refusals(tmp_path: Path) -> None:
    """Missing databases and malformed flags fail closed with exit codes."""
    missing = str(tmp_path / "absent.db")
    code, _, stderr = _run_cli("storage", "ceiling", "audit", "--storage-path", missing)
    assert code == EXIT_BUILD
    assert "existing SQLite database required" in stderr

    no_store = str(tmp_path / "empty.db")
    asyncio.run(_create_empty_database(no_store))

    code, _, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", no_store, "--limit", "0"
    )
    assert code == EXIT_CONFIG
    assert "--limit" in stderr

    code, _, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", no_store, "--limit", "101"
    )
    assert code == EXIT_CONFIG

    code, _, stderr = _run_cli(
        "storage",
        "ceiling",
        "verify",
        "--storage-path",
        no_store,
        "--event",
        "",
    )
    assert code == EXIT_CONFIG

    code, _, stderr = _run_cli(
        "storage",
        "ceiling",
        "audit",
        "--storage-path",
        no_store,
        "--as-of",
        "2026-11-21T12:00:00",
    )
    assert code == EXIT_CONFIG
    assert "offset-aware" in stderr


def test_cli_single_event_modes(tmp_path: Path) -> None:
    """--event reports exactly one event and verify fails closed without it."""
    db_path = str(tmp_path / "single.db")
    asyncio.run(_prepare_database(db_path))

    code, stdout, stderr = _run_cli(
        "storage",
        "ceiling",
        "verify",
        "--storage-path",
        db_path,
        "--event",
        "ceiling-never-admitted",
    )
    assert code == EXIT_FAILED, stderr
    verify = _parse_cli_json(stdout)
    assert verify["event_state"] == "no_receipt"
    assert verify["receipts_examined"] == 0

    code, stdout, stderr = _run_cli(
        "storage",
        "ceiling",
        "audit",
        "--storage-path",
        db_path,
        "--event",
        "ceiling-cli-full",
    )
    assert code == 0, stderr
    audit = _parse_cli_json(stdout)
    assert audit["state"] == "enforced_clean"
    assert audit["residual_stores"] == {}
    assert audit["receipt"]["rows_removed"] > 0

    code, stdout, stderr = _run_cli(
        "storage",
        "ceiling",
        "audit",
        "--storage-path",
        db_path,
        "--as-of",
        "2026-11-21T12:00:00Z",
    )
    assert code == 0, stderr
    assert _parse_cli_json(stdout)["observed"] == "2026-11-21T12:00:00.000000+00:00"


def test_cli_commands_are_strictly_read_only(tmp_path: Path) -> None:
    """Audit and verify write no application data: the database image is
    byte-identical after both read-only runs. SQLite may create empty WAL
    bookkeeping sidecars for its readers — the same documented caveat as
    the catalog status command — so sidecars are not part of the claim."""
    db_path = str(tmp_path / "readonly.db")
    asyncio.run(_prepare_database(db_path))

    with open(db_path, "rb") as source:
        before = hashlib.sha256(source.read()).hexdigest()

    import sqlite3
    from contextlib import closing

    def logical_state():
        with closing(sqlite3.connect(db_path)) as db:
            return tuple(db.iterdump())

    original_state = logical_state()
    for mode in ("audit", "verify"):
        code, _, stderr = _run_cli(
            "storage", "ceiling", mode, "--storage-path", db_path
        )
        assert code == 0, stderr
        assert logical_state() == original_state

    with open(db_path, "rb") as source:
        assert hashlib.sha256(source.read()).hexdigest() == before


def test_ceiling_commands_registered_in_parser() -> None:
    """The storage ceiling surface is registered with both modes and flags."""
    from medre.cli.main import _build_parser

    parser = _build_parser()
    parsed = parser.parse_args(
        ["storage", "ceiling", "audit", "--storage-path", "/dev/null"]
    )
    assert parsed.storage_command == "ceiling"
    assert parsed.ceiling_command == "audit"
    assert parsed.limit == 25
    assert parsed.event is None
    parsed = parser.parse_args(
        [
            "storage",
            "ceiling",
            "verify",
            "--storage-path",
            "/dev/null",
            "--limit",
            "50",
            "--event",
            "evt",
        ]
    )
    assert parsed.ceiling_command == "verify"
    assert parsed.limit == 50
    assert parsed.event == "evt"
