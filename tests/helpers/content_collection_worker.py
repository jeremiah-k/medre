"""Scripted worker storage and candidate sources for collection tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import msgspec

from medre.core.storage.backend import (
    StorageError,
)
from medre.core.storage.content_retention import (
    CandidatePage,
    ContentRetentionPolicy,
    RetentionEpoch,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import (
    ContentCollectionWorker,
)
from medre.runtime.events import EventBuffer
from tests.helpers.storage import make_storage_event

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
_MARKER = "SECRET-CONTENT-MARKER-123"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable worker clock advanced explicitly by tests."""

    def __init__(self, start: datetime = _T0) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: Any) -> None:
        self.value = self.value + timedelta(**delta)


class ScriptedCandidateSource:
    """Satisfies ``CollectionCandidateSource`` with a cursor-keyed page table.

    Serves the page registered for the requested ``after`` cursor; an
    unknown cursor yields an empty exhausted page. Every call is recorded
    so tests can assert the exact cursor, limit, and watermark progression.
    """

    def __init__(
        self,
        pages: dict[tuple[str, str] | None, CandidatePage],
        *,
        trace: list[str] | None = None,
    ) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []
        self.trace = trace if trace is not None else []

    async def page_collection_candidates(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        watermark: str | None,
    ) -> CandidatePage:
        self.calls.append({"after": after, "limit": limit, "watermark": watermark})
        self.trace.append("page")
        return self.pages.get(
            after, CandidatePage(event_ids=[], next_after=None, has_more=False)
        )


class ScriptedStorage:
    """Satisfies ``CollectionWorkerStorage`` with scripted outcomes.

    The epoch/heartbeat bookkeeping imitates the durable max-semantics
    heartbeat; ``errors`` maps an event ID to an exception the collector
    raises for it; ``collect_gate`` optionally blocks collection calls
    until released (optionally cancellation-resistantly) to exercise the
    bounded stop.
    """

    def __init__(
        self,
        epoch: RetentionEpoch | None = None,
        *,
        results: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
        clock: FakeClock | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.epoch = epoch
        self.results = results if results is not None else {}
        self.errors = errors if errors is not None else {}
        self.clock = clock if clock is not None else FakeClock()
        self.collect_calls: list[tuple[str, bool]] = []
        self.heartbeats: list[tuple[int, datetime]] = []
        self.opened: list[tuple[str, datetime]] = []
        self.trace = trace if trace is not None else []
        self.fail_epoch_reads = 0
        self.collect_gate: asyncio.Event | None = None
        self.collect_cancel_resistant = False

    async def collect_event_content(
        self,
        event_id: str,
        *,
        dry_run: bool = True,
        retention_epoch: int | None = None,
    ) -> dict[str, Any]:
        if retention_epoch is not None and (
            self.epoch is None or retention_epoch != self.epoch.epoch
        ):
            from medre.core.storage.backend import ContentCollectionEpochChanged

            raise ContentCollectionEpochChanged("Fake activation changed")
        self.collect_calls.append((event_id, dry_run))
        self.trace.append(f"collect:{event_id}")
        exc = self.errors.get(event_id)
        if exc is not None:
            raise exc
        if self.collect_gate is not None and self.collect_cancel_resistant:
            # Model a storage call that swallows cancellation and keeps
            # blocking until explicitly released.
            while not self.collect_gate.is_set():
                try:
                    await self.collect_gate.wait()
                except asyncio.CancelledError:
                    continue
        elif self.collect_gate is not None:
            await self.collect_gate.wait()
        result = self.results.get(event_id)
        if result is not None:
            return result
        return {"status": "empty", "rows_removed": 0, "logical_bytes_removed": 0}

    async def latest_retention_epoch(self) -> RetentionEpoch | None:
        self.trace.append("epoch_read")
        if self.fail_epoch_reads > 0:
            self.fail_epoch_reads -= 1
            raise StorageError("Reading the retention epoch failed")
        return self.epoch

    async def open_retention_epoch(
        self, *, activated_by: str, now: datetime
    ) -> RetentionEpoch:
        self.opened.append((activated_by, now))
        self.trace.append("epoch_open")
        next_number = 1 if self.epoch is None else self.epoch.epoch + 1
        self.epoch = RetentionEpoch(
            epoch=next_number,
            mode="relay-only",
            activated_at=now.isoformat(),
            admission_watermark=None,
            last_observed_active_at=None,
            activated_by=activated_by,
        )
        return self.epoch

    async def observe_retention_heartbeat(self, *, epoch: int, now: datetime) -> None:
        self.heartbeats.append((epoch, now))
        self.trace.append(f"heartbeat:{epoch}")
        current = self.epoch
        if current is not None and current.epoch == epoch:
            observed = now.isoformat()
            if current.last_observed_active_at is None or (
                current.last_observed_active_at < observed
            ):
                self.epoch = replace(current, last_observed_active_at=observed)


class RecordingStorage:
    """Wraps the real ``SQLiteStorage``, recording collector calls.

    Satisfies ``CollectionWorkerStorage`` so real-storage tests can assert
    the exact ``collect_event_content`` invocation discipline (always
    ``dry_run=False``) while every decision stays the real guarded
    collector's.
    """

    def __init__(self, inner: SQLiteStorage) -> None:
        self._inner = inner
        self.collect_calls: list[tuple[str, bool]] = []

    async def collect_event_content(
        self,
        event_id: str,
        *,
        dry_run: bool = True,
        retention_epoch: int | None = None,
    ) -> dict[str, Any]:
        self.collect_calls.append((event_id, dry_run))
        return await self._inner.collect_event_content(
            event_id, dry_run=dry_run, retention_epoch=retention_epoch
        )

    async def latest_retention_epoch(self) -> RetentionEpoch | None:
        return await self._inner.latest_retention_epoch()

    async def open_retention_epoch(
        self, *, activated_by: str, now: datetime
    ) -> RetentionEpoch:
        return await self._inner.open_retention_epoch(
            activated_by=activated_by, now=now
        )

    async def observe_retention_heartbeat(self, *, epoch: int, now: datetime) -> None:
        await self._inner.observe_retention_heartbeat(epoch=epoch, now=now)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page(
    event_ids: list[str],
    *,
    next_after: tuple[str, str] | None = None,
    has_more: bool = False,
) -> CandidatePage:
    return CandidatePage(event_ids=event_ids, next_after=next_after, has_more=has_more)


def _fresh_epoch(clock: FakeClock, *, epoch: int = 1) -> RetentionEpoch:
    """An epoch activated at the clock's current instant (fresh by
    construction; heartbeat advances it forward-only from here)."""
    return RetentionEpoch(
        epoch=epoch,
        mode="relay-only",
        activated_at=clock.value.isoformat(),
        admission_watermark="2026-10-01T00:00:00+00:00",
        last_observed_active_at=None,
        activated_by="operator",
    )


def _worker(
    storage: ScriptedStorage | SQLiteStorage,
    source: ScriptedCandidateSource,
    *,
    policy: ContentRetentionPolicy | None = None,
    clock: FakeClock | None = None,
    buffer: EventBuffer | None = None,
    stop_timeout: float = 5.0,
) -> ContentCollectionWorker:
    worker = ContentCollectionWorker(
        storage,
        source,
        policy if policy is not None else ContentRetentionPolicy(mode="relay-only"),
        event_buffer=buffer,
        stop_timeout_seconds=stop_timeout,
        now=clock,
    )

    # This harness models a worker that already owns the seeded test epoch.
    # start() resets the flag and exercises the real process-start boundary.
    worker._startup_epoch_checked = True
    return worker


def _event_types(buffer: EventBuffer) -> list[str]:
    return [event.event_type.value for event in buffer]


def _detail_of(buffer: EventBuffer, event_type: str) -> dict[str, Any]:
    matches = [e for e in buffer if e.event_type.value == event_type]
    assert matches, f"no {event_type} event emitted"
    return matches[-1].detail


async def _seed_eligible(
    store: SQLiteStorage, event_id: str, created_at: str, body: str
) -> None:
    """Seed one settled ordinary text event at a pinned admission time."""
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": body}, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        ("2026-10-31T01:00:00+00:00", event_id),
    )


async def _body_present(store: SQLiteStorage, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT body FROM event_body_content WHERE event_id=?", (event_id,)
    )
    return row is not None


# ---------------------------------------------------------------------------
# Worker lifecycle: start/stop idempotence and bounded stop
# ---------------------------------------------------------------------------
