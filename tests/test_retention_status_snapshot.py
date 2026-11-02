"""An operator activation cannot mix epochs in one aggregate status report."""

import sqlite3
import threading
from datetime import datetime, timezone

from medre.core.storage.sqlite._retention import sync_open_retention_epoch


async def test_status_keeps_epoch_and_preview_in_one_read_snapshot(temp_storage):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    first = await temp_storage.open_retention_epoch(activated_by="operator", now=now)
    rotated = []

    def rotate_before_receipt_count(sql):
        if "SELECT COUNT(*) FROM event_content_collection_log" not in sql or rotated:
            return
        # This callback runs after status read its epoch. A different writer
        # can activate while the WAL reader retains its original snapshot.
        writer = sqlite3.connect(temp_storage._db_path)
        try:
            rotated.append(
                sync_open_retention_epoch(
                    writer, threading.Lock(), activated_by="operator", now=now
                )
            )
        finally:
            writer.close()

    await temp_storage._run_in_thread(
        temp_storage._require_db().set_trace_callback, rotate_before_receipt_count
    )
    try:
        epoch, count, preview = await temp_storage.read_content_retention_status()
    finally:
        await temp_storage._run_in_thread(
            temp_storage._require_db().set_trace_callback, None
        )
    assert rotated and rotated[0].epoch == first.epoch + 1
    assert epoch.epoch == first.epoch == preview["epoch"]
    assert count == 0
    assert (await temp_storage.latest_retention_epoch()).epoch == rotated[0].epoch
