"""Cancellation retains ownership of connections created by SQLite workers."""

import asyncio
import sqlite3
import threading

import pytest

from medre.core.storage.sqlite import storage as storage_module
from medre.core.storage.sqlite.storage import SQLiteStorage


@pytest.mark.parametrize("readonly", [False, True])
async def test_cancelled_open_closes_worker_result(tmp_path, monkeypatch, readonly):
    """A connection arriving after cancellation is closed before the caller exits."""
    path = str(tmp_path / "state.sqlite")
    seed = SQLiteStorage(path)
    await seed.initialize()
    await seed.close()
    instances = []

    class TrackedStorage(SQLiteStorage):
        """Retain the otherwise unpublished read-only instance for leak checks."""

        def __init__(self, db_path):
            super().__init__(db_path)
            instances.append(self)

    entered, release = threading.Event(), threading.Event()
    handles = []
    name = "sync_open_readonly" if readonly else "sync_open"
    original = getattr(storage_module, name)

    def paused_open(db_path):
        """Hold a real open handle in the worker before returning ownership."""
        db = original(db_path)
        handles.append(db)
        entered.set()
        if not release.wait(5):
            db.close()
            raise RuntimeError("test opener barrier timed out")
        return db

    monkeypatch.setattr(storage_module, name, paused_open)
    if readonly:
        opening = asyncio.create_task(TrackedStorage.open_readonly(path))
    else:
        storage = TrackedStorage(path)
        opening = asyncio.create_task(storage.initialize())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        opening.cancel()
        opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert opening.cancelling() == 2
        # Exercise the runtime's idempotent cleanup too: it cannot recover a
        # handle discarded by a cancelled executor await.
        await instances[0].close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            handles[0].execute("SELECT 1")
        assert instances[0]._db is None
        assert instances[0]._executor is None
    finally:
        release.set()
        if not opening.done():
            opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        for instance in instances:
            await instance.close()
        for db in handles:
            db.close()
