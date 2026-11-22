"""The read-only residual audit attributes residuals to the policy row.

Residual candidate discovery correlates every battery fragment to the
outer policy row. The checkpoint projection must not own that
correlation: adding an ``event_id`` column to ``radio_attempt_progress``
must not stop the audit from reporting a radio residual, because SQLite
resolves a shared alias innermost-first and an inner column would
otherwise shadow the policy row.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _FINGERPRINT,
    _admit_with_ceiling,
    _read_ceiling,
)
from tests.helpers.storage_outbox import make_outbox_item


async def _seed_radio_residual(store: SQLiteStorage, event_id: str) -> None:
    """Enforce one elapsed ceiling, then land a late radio recovery plan."""
    created = await store.create_outbox_item(
        make_outbox_item(
            delivery_plan_id=f"plan-{event_id}",
            event_id=event_id,
            target_adapter="radio",
        )
    )
    now = "2026-01-01T00:00:00.000000+00:00"
    await store._write(
        "INSERT INTO radio_attempt_progress (outbox_id, attempt_number, "
        "plan_fingerprint, plan_json, provenance_json, parts_json, revision, "
        "created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?)",
        (
            created.outbox_id,
            _FINGERPRINT,
            '{"plan":"synthetic"}',
            '{"provenance":"synthetic"}',
            '{"parts":["late"]}',
            now,
            now,
        ),
    )


async def test_residual_audit_survives_a_checkpoint_event_id_column(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "residual-alias.sqlite")
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        event_id = "ceiling-alias-residual"
        await _admit_with_ceiling(store, event_id)
        ceiling = await _read_ceiling(store, event_id)
        await store.enforce_content_ceilings(now=ceiling + timedelta(seconds=1))
        await _seed_radio_residual(store, event_id)

        report = await store.audit_content_ceilings()
        assert [finding["event_id"] for finding in report["residual_enforced"]] == [
            event_id
        ]
        assert report["residual_enforced"][0]["residual_stores"] == {
            "radio_attempt_progress": 1
        }

        # The future schema: the checkpoint row carries its own event_id
        # column with a value matching no policy row.
        with closing(sqlite3.connect(db_path)) as raw:
            raw.execute("ALTER TABLE radio_attempt_progress ADD COLUMN event_id TEXT")
            raw.execute("UPDATE radio_attempt_progress SET event_id='unrelated'")
            raw.commit()

        report = await store.audit_content_ceilings()
        assert [finding["event_id"] for finding in report["residual_enforced"]] == [
            event_id
        ]
        assert report["residual_enforced"][0]["residual_stores"] == {
            "radio_attempt_progress": 1
        }
    finally:
        await store.close()
        # The throwaway database now carries a schema the opener would
        # refuse; remove it so nothing else can adopt it by mistake.
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
