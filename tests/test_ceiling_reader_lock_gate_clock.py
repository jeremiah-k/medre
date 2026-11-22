"""A storage reader must not sample the ceiling clock before the SQLite lock.

The clock may only be sampled at lock acquisition or later. A reader that
samples earlier — inside ``sync_read_all`` just before ``with lock:`` —
would see an instant that predates a ceiling crossing that happens while
the reader waits for the lock, and a queued read would then release
provider metadata that is already past its hard ceiling.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest

import medre.core.storage.sqlite.connection as connection_module
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _admit_with_ceiling, _read_ceiling


class _GateLock:
    """Proxy lock that reports when a worker reaches the acquisition point."""

    def __init__(self, inner: threading.Lock) -> None:
        self._inner = inner
        self.reached = threading.Event()
        self.proceed = threading.Event()

    def acquire(self, *args: object, **kwargs: object) -> object:
        self.reached.set()
        if not self.proceed.wait(timeout=30):
            raise RuntimeError("lock gate never released")
        return self._inner.acquire(*args, **kwargs)  # type: ignore[arg-type]

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "_GateLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


async def test_native_ref_reader_samples_clock_at_lock_acquisition(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = "ceiling-lock-gate-read"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    # A raw late write models provider metadata an authorized finalizer
    # committed before the ceiling; ordinary reads must mask it.
    await temp_storage._write(
        "INSERT INTO native_message_refs "
        "(id, event_id, adapter, native_message_id, direction, metadata, created_at) "
        "VALUES (?, ?, 'radio', 'gate-native-1', 'outbound', ?, ?)",
        (
            "gate-nref",
            event_id,
            '{"provider":"gate-private"}',
            ceiling.isoformat(timespec="microseconds"),
        ),
    )
    clock = {"now": ceiling - timedelta(seconds=1)}
    monkeypatch.setattr(
        connection_module,
        "ceiling_read_time",
        lambda: clock["now"].isoformat(timespec="microseconds"),
    )

    real_lock = temp_storage._lock
    gate = _GateLock(real_lock)
    temp_storage._lock = gate  # type: ignore[assignment]
    try:
        task = asyncio.create_task(temp_storage.list_native_refs_for_event(event_id))
        # The worker is now parked at the acquisition point: any sample taken
        # before the lock would already have happened and seen the old time.
        assert await asyncio.to_thread(gate.reached.wait, 10)
        assert not task.done()
        clock["now"] = ceiling + timedelta(seconds=1)
        gate.proceed.set()
        refs = await asyncio.wait_for(task, timeout=10)
    finally:
        temp_storage._lock = real_lock

    assert len(refs) == 1
    assert refs[0].event_id == event_id
    # The read crossed the ceiling while waiting for the lock, so the
    # provider metadata stayed masked.
    assert refs[0].metadata == {}
