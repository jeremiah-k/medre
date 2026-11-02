"""Shared qualification harness for the content-lifecycle test suites.

Test support only — nothing here runs in the product. The harness drives the
real ``ContentCollectionWorker`` over the real ``SQLiteStorage`` epoch,
selector, and collector methods with an injected clock, and provides the real
admission/outbox sequence adapted from the production-path vertical in
``tests/test_ordinary_text_collection.py`` (admit through the real pipeline,
stage both targets through the durable ingress worker, settle the first
target, and dispatch the pending one through the real initial-dispatch
worker). The recording wrappers add a call log only; every decision stays the
real storage's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import msgspec

from medre.core.events import EventMetadata, NativeRef
from medre.core.events.metadata import RoutingMetadata
from medre.core.storage.content_retention import (
    CandidatePage,
    ContentRetentionPolicy,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import ContentCollectionWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.initial_outbox import _pipeline
from tests.helpers.pipeline import make_event
from tests.helpers.storage import make_storage_event

#: Default pinned admission body marker; assertions prove it is gone after
#: collection and retained while blocked.
MARKER = "QUALIFICATION-BODY"

#: The controlled settlement instant for released append owners.
SETTLED_AT = "2026-10-31T01:00:00+00:00"

#: A pinned UTC instant for injected clocks.
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    """Injected worker clock advanced explicitly by tests."""

    def __init__(self, start: datetime = T0) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: Any) -> None:
        self.value = self.value + timedelta(**delta)


@dataclass
class CollectRecording:
    """The guarded collector's and selector's call log for assertions."""

    collects: list[str] = field(default_factory=list)
    pages: list[tuple[tuple[str, str] | None, int, str | None]] = field(
        default_factory=list
    )


def recording_storage(store: SQLiteStorage, record: CollectRecording) -> SQLiteStorage:
    """Wrap the real store's collector and selector with call recording.

    The wrapped methods delegate to the real guarded implementations (bound
    before the instance attributes are replaced); only the call log is added,
    so every decision stays the real storage's.
    """
    original_collect = store.collect_event_content
    original_page = store.page_collection_candidates

    async def collect(
        event_id: str, *, dry_run: bool = True, retention_epoch: int | None = None
    ) -> dict[str, Any]:
        record.collects.append(event_id)
        return await original_collect(
            event_id, dry_run=dry_run, retention_epoch=retention_epoch
        )

    async def page(
        *,
        after: tuple[str, str] | None,
        limit: int,
        watermark: str | None,
    ) -> CandidatePage:
        record.pages.append((after, limit, watermark))
        return await original_page(after=after, limit=limit, watermark=watermark)

    store.collect_event_content = collect  # type: ignore[method-assign]
    store.page_collection_candidates = page  # type: ignore[method-assign]
    return store


def make_worker(
    store: SQLiteStorage,
    clock: FakeClock,
    *,
    policy: ContentRetentionPolicy | None = None,
) -> ContentCollectionWorker:
    """An already-activated test worker; storage remains its real selector."""
    worker = ContentCollectionWorker(
        store,
        store,
        policy if policy is not None else ContentRetentionPolicy(mode="relay-only"),
        now=clock,
    )

    # This harness models a worker that already owns the seeded test epoch.
    # start() resets the flag and exercises the real process-start boundary.
    worker._startup_epoch_checked = True
    return worker


async def wait_for_worker_epoch(
    worker: ContentCollectionWorker, *, timeout: float = 5.0
) -> int:
    """Wait until a started worker's own activation epoch is established.

    Every started LIVE worker opens (or adopts) its epoch on the first
    sweep, and automatic collection applies only to events admitted after
    that epoch's boundary. Tests that need collectable seeds must admit
    them only after this returns; there is no fixed sleep — the poll rides
    the worker's published state.
    """
    assert await wait_until(lambda: worker.state.epoch is not None, timeout=timeout)
    epoch = worker.state.epoch
    assert epoch is not None
    return epoch


async def seed_event(
    store: SQLiteStorage,
    event_id: str,
    created_at: str,
    *,
    body: str | None = MARKER,
    settled: bool = True,
) -> None:
    """Append one event and pin its local admission time deterministically.

    Pinning ``created_at`` avoids racing the wall clock's microsecond
    resolution whenever a test depends on admission ordering. With
    ``settled=True`` the append owner is released so the guarded collector
    deems the event eligible.
    """
    payload = {"body": body} if body is not None else {}
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload=payload, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )
    if settled:
        await settle_append_owner(store, event_id)


async def settle_append_owner(store: SQLiteStorage, event_id: str) -> None:
    """Release the append owner (the controlled settlement fixture)."""
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        (SETTLED_AT, event_id),
    )


async def body_present(store: SQLiteStorage, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT body FROM event_body_content WHERE event_id=?", (event_id,)
    )
    return row is not None


async def open_operator_epoch(store: SQLiteStorage, when: datetime) -> int:
    """Open one operator activation epoch and return its number."""
    epoch = await store.open_retention_epoch(activated_by="operator", now=when)
    return epoch.epoch


def utc(year: int, month: int, day: int, **kw: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc, **kw)


def iso(moment: datetime) -> str:
    """Render a timezone-aware instant as a durable microsecond ISO string."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# The real admission/outbox vertical (adapted from the production-path
# fixtures in tests/test_ordinary_text_collection.py)
# ---------------------------------------------------------------------------


def text_event(
    event_id: str,
    *,
    metadata: EventMetadata | None = None,
    payload: dict | None = None,
) -> object:
    """A normal message with core routing metadata and a separated body."""
    event = make_event(
        event_id=event_id,
        source_adapter="source",
        payload={"body": MARKER},
        source_native_ref=NativeRef(
            adapter="source",
            native_channel_id="ch-0",
            native_message_id=f"native-{event_id}",
        ),
    )
    routing = (
        metadata.routing
        if metadata is not None and metadata.routing is not None
        else RoutingMetadata(matched_routes=("mapping",))
    )
    carried = EventMetadata() if metadata is None else msgspec.structs.replace(metadata)
    return msgspec.structs.replace(
        event,
        payload={"body": MARKER} if payload is None else payload,
        metadata=msgspec.structs.replace(carried, routing=routing),
    )


async def admit_and_stage(
    store: SQLiteStorage, event_id: str, **event_kwargs: object
) -> tuple[dict[str, Any], Any]:
    """Admit through the real pipeline and stage both targets for delivery.

    The durable ingress worker fans the event out through the production
    staging path, so both outbox rows and their frozen work snapshots are
    written by the real machinery (no manual outbox fixtures).
    """
    from medre.core.ingress import DurableIngressWorker

    runner, _, _ = _pipeline(store)
    event = text_event(event_id, **event_kwargs)
    await runner.admit_ingress(event, "live")
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    assert await ingress.run_once() == 1
    rows = await store.list_outbox_items_for_event(event_id)
    by_adapter = {row.target_adapter: row for row in rows}
    assert set(by_adapter) == {"radio", "matrix"}
    assert all(row.status == "pending" for row in by_adapter.values())
    return by_adapter, runner


async def settle_first_target(store: SQLiteStorage, rows: dict) -> None:
    """Settle the radio target through its real claim and terminal transition."""
    from datetime import datetime as _datetime

    claimed = await store.claim_due_outbox_items(
        _datetime.now(timezone.utc).isoformat(),
        "worker",
        excluded_adapters=("matrix",),
    )
    assert [item.outbox_id for item in claimed] == [rows["radio"].outbox_id]
    assert await store.mark_outbox_sent(rows["radio"].outbox_id)


async def dispatch_pending_through_real_worker(
    store: SQLiteStorage, runner: Any
) -> None:
    """Run one real initial-dispatch batch; no sleeps, no synthetic receipts."""
    from medre.runtime.retry import RetryWorker

    dispatcher = RetryWorker(store, runner, None, initial_dispatch=True, enabled=False)
    await dispatcher._process_due(datetime.now(timezone.utc))
