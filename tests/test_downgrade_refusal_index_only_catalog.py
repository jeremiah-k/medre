"""Index-only newer catalogs refuse a downgrade as explicitly as tables.

An undeclared index created on an existing declared table, with no extra
table, must alone trigger the explicit downgrade refusal naming only that
index, and a refused open must leave the database bytes untouched.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.backend import StorageDowngradeRefusedError
from medre.core.storage.sqlite.connection import sync_open

_NOW = "2026-11-20T00:00:00+00:00"


def _populate_current(path: Path) -> None:
    with closing(sync_open(str(path))) as db:
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('event', 'message.text', 1, ?, 'radio', 'radio', ?)",
            (_NOW, _NOW),
        )
        db.commit()


def test_extra_index_without_extra_table_refuses_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index-only.sqlite"
    _populate_current(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE INDEX idx_future_outbox_replay ON delivery_outbox (replay_run_id)"
        )
        db.commit()
    before = path.read_bytes()
    with pytest.raises(StorageDowngradeRefusedError) as excinfo:
        with closing(sync_open(str(path))):
            pass
    assert excinfo.value.unexpected == ["index idx_future_outbox_replay"]
    assert path.read_bytes() == before
