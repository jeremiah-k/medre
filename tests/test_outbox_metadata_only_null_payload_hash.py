"""Metadata-only outbox reads project ``payload_hash`` as NULL, cleanly.

The metadata-only projection replaces the payload-derived digest column with
``NULL AS payload_hash``; the typed mapper and both listing surfaces must
tolerate that without a decode failure or a rendered ``None`` hash, while the
ordinary read keeps the stored digest.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from medre.core.storage.sqlite.connection import sync_open
from medre.core.storage.sqlite.storage import SQLiteStorage

_NOW = "2026-11-20T00:00:00+00:00"
_HASH = "sha256:" + "a" * 64


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
            " target_adapter, payload_hash, created_at, updated_at)"
            " VALUES ('work-1', 'event', 'plan', 'radio', ?, ?, ?)",
            (_HASH, _NOW, _NOW),
        )
        db.commit()


async def test_metadata_only_outbox_reads_null_the_payload_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "medre.db"
    _populate(path)
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    try:
        listed = await storage.list_all_outbox_items(metadata_only=True)
        assert [item.outbox_id for item in listed] == ["work-1"]
        assert listed[0].payload_hash is None

        single = await storage.get_outbox_item("work-1", metadata_only=True)
        assert single is not None
        assert single.payload_hash is None

        ordinary = await storage.get_outbox_item("work-1")
        assert ordinary is not None
        assert ordinary.payload_hash == _HASH
    finally:
        await storage.close()
