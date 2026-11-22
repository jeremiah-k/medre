"""Both ceiling commands leave a residual-carrying store logically unchanged.

The read-only proof must run against a store that actually carries
residual content: a command that updates sweep bookkeeping only when it
finds residuals would pass a byte-identical check on an already clean
database. The late-write fixture is compared after the audit and again
after the expected verify failure, before the repair sweep mutates it.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medre.cli.exit_codes import EXIT_FAILED
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
    from medre.cli.main import main

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = 0
        try:
            main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


async def _prepare_residual_database(db_path: str) -> str:
    """A swept lifecycle database that then regained residual content."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        event_id = "ceiling-cli-residual"
        await _admit_with_ceiling(storage, event_id)
        outbox_id = await _seed_every_copy(storage, event_id)
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
        await _insert_blob(storage, _BLOB_REF)
        await _associate_blob(storage, event_id, _BLOB_REF)
        ceiling = await _read_ceiling(storage, event_id)
        await storage.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
        await _seed_late_write(storage, event_id, outbox_id)
    finally:
        await storage.close()
    return outbox_id


def _logical_state(db_path: str) -> tuple:
    with closing(sqlite3.connect(db_path)) as db:
        return tuple(db.iterdump())


def test_audit_and_verify_leave_residual_store_logically_unchanged(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "residual-readonly.db")
    outbox_id = asyncio.run(_prepare_residual_database(db_path))

    assert _logical_state(db_path) == _logical_state(db_path)
    original_state = _logical_state(db_path)

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "audit", "--storage-path", db_path
    )
    assert code == 0, stderr
    report = json.loads(stdout)
    assert report["purged"] is False
    assert len(report["residual_enforced"]) == 1
    assert report["residual_enforced"][0]["event_id"] == "ceiling-cli-residual"
    # The audit observed the residuals without touching sweep bookkeeping.
    assert _logical_state(db_path) == original_state

    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == EXIT_FAILED, stderr
    verify = json.loads(stdout)
    assert verify["verdict"] == "failed"
    assert verify["receipts_failed"] == 1
    # The failed verdict also wrote nothing.
    assert _logical_state(db_path) == original_state

    async def _sweep() -> list:
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        try:
            return await storage.enforce_content_ceilings()
        finally:
            await storage.close()

    # The fixture really carried residuals: the repair sweep re-purges the
    # late write, so the unchanged comparisons above were meaningful.
    followup = asyncio.run(_sweep())
    assert len(followup) == 1
    assert followup[0].event_id == "ceiling-cli-residual"
    assert followup[0].residual_pass is True
    code, stdout, stderr = _run_cli(
        "storage", "ceiling", "verify", "--storage-path", db_path
    )
    assert code == 0, stderr
    assert json.loads(stdout)["verdict"] == "passed"
    assert outbox_id
