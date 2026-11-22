"""Equal-watermark restore refuses observation-only divergence.

The delivery-evidence watermark is the receipt identity high-water, so a
delivery observation appended after capture leaves both watermarks equal.
The restore guard additionally requires equal logical state, so an
observation-only change on the live database refuses the restore, and an
unchanged live database restores the same snapshot successfully.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.core.storage.sqlite.connection import sync_open
from medre.maintenance.stopped_snapshot import (
    SnapshotEntry,
    create_stopped_snapshot,
    restore_stopped_snapshot,
)

_NOW = "2026-11-20T00:00:00+00:00"


def _populate(path: Path) -> None:
    with closing(sync_open(str(path))) as db:
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version,"
            " timestamp, source_adapter, source_transport_id, created_at)"
            " VALUES ('event', 'message.text', 1, ?, 'radio', 'radio', ?)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, delivery_plan_id,"
            " target_adapter, attempt_number, created_at, updated_at)"
            " VALUES ('work-1', 'event', 'plan', 'radio', 1, ?, ?)",
            (_NOW, _NOW),
        )
        db.commit()


def _capture(install_root: Path, output: Path) -> None:
    create_stopped_snapshot(
        install_root,
        output,
        [SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")],
        runtime_stopped=True,
    )


def test_observation_only_live_change_refuses_equal_watermark_restore(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    live = install_root / "state" / "medre.sqlite"
    live.parent.mkdir(parents=True)
    _populate(live)
    backup = tmp_path / "backup"
    _capture(install_root, backup)

    unchanged_destination = tmp_path / "restored-unchanged"
    restore_stopped_snapshot(
        backup,
        unchanged_destination,
        runtime_stopped=True,
        live_database=live,
    )
    assert (unchanged_destination / "state" / "medre.sqlite").is_file()

    with closing(sqlite3.connect(live)) as db:
        db.execute(
            "INSERT INTO delivery_observations (observation_id, event_id,"
            " delivery_plan_id, target_adapter, outbox_id, attempt_number,"
            " adapter_message_id, state, confirmation_level, metadata,"
            " observed_at) VALUES ('obs-later', 'event', 'plan', 'radio',"
            " 'work-1', 1, 'nm-later', 'delivered', 'unknown', '{}', ?)",
            (_NOW,),
        )
        db.commit()

    divergent_destination = tmp_path / "restored-divergent"
    with pytest.raises(ValueError, match="logical state differs"):
        restore_stopped_snapshot(
            backup,
            divergent_destination,
            runtime_stopped=True,
            live_database=live,
        )
    assert not divergent_destination.exists()
