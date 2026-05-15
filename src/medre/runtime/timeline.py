"""Shared timeline/lineage assembly layer for medre.

Centralises the fetch+assemble pattern duplicated across trace, evidence,
recover, and inspect commands.  Delegates timeline construction ordering
to :mod:`medre.runtime.trace`; this module adds storage-backed async
fetch, source classification (live/retry/replay/mixed), replay-run grouping,
and ordering guarantees.

All functions accept a :class:`~medre.core.storage.backend.StorageBackend`
instance and return plain dicts — no DTO hierarchies, no ORM, no hidden
SQL.
"""

from __future__ import annotations

from typing import Any

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.storage.backend import StorageBackend
from medre.runtime.trace import (
    assemble_event_timeline as _assemble_event_entries,
    assemble_replay_timeline as _assemble_replay_entries,
)

__all__ = [
    "assemble_event_timeline",
    "assemble_replay_timeline",
    "assemble_storage_summary",
]


# ---------------------------------------------------------------------------
# Ordering guarantees (documented once, referenced everywhere)
# ---------------------------------------------------------------------------

ORDERING_GUARANTEES: dict[str, dict[str, str]] = {
    "receipts": {
        "order": "sequence ASC",
        "deterministic": "true",
        "note": "Autoincrement PK guarantees global insertion order.",
    },
    "native_refs": {
        "order": "created_at ASC, id ASC",
        "deterministic": "true",
        "note": "Timestamp with id tiebreaker covers clock skew.",
    },
    "relations": {
        "order": "id ASC",
        "deterministic": "true",
        "note": "Autoincrement PK on event_relations table.",
    },
    "retry_due": {
        "order": "next_retry_at ASC, sequence ASC",
        "deterministic": "true",
        "note": "Retry due query orders by scheduled retry time with sequence tiebreaker.",
    },
    "timeline_entries": {
        "order": "timestamp ASC, ordinal ASC",
        "deterministic": "true",
        "note": "Synthesised by medre.runtime.trace from above orderings.",
    },
}
"""Documented ordering guarantees for each timeline component."""


# ---------------------------------------------------------------------------
# assemble_event_timeline
# ---------------------------------------------------------------------------


async def assemble_event_timeline(
    storage: StorageBackend,
    event_id: str,
) -> dict[str, Any] | None:
    """Fetch all data for *event_id* and assemble an enriched timeline.

    Returns ``None`` when the event does not exist in storage.

    The returned dict contains:

    - **event**: the :class:`CanonicalEvent` (or ``None``).
    - **receipts**: ``list[DeliveryReceipt]`` ordered by ``sequence ASC``.
    - **native_refs**: ``list[NativeMessageRef]`` ordered by
      ``created_at ASC, id ASC``.
    - **relations**: ``list[EventRelation]`` ordered by ``id ASC``.
    - **replay_runs**: ``dict[str, list[DeliveryReceipt]]`` grouping
      ``replay_run_id`` → receipts belonging to that run.
    - **source**: ``"live"`` | ``"replay"`` | ``"retry"`` | ``"mixed"`` — whether
      this event has live receipts, replay receipts, retry receipts, or
      a combination.
    - **timeline_entries**: flat list built by
      :func:`medre.runtime.trace.assemble_event_timeline`.
    - **ordering_guarantees**: reference to :data:`ORDERING_GUARANTEES`.
    """
    event: CanonicalEvent | None = await storage.get(event_id)
    if event is None:
        return None

    receipts: list[DeliveryReceipt] = await storage.list_receipts_for_event(
        event_id,
    )
    native_refs: list[NativeMessageRef] = (
        await storage.list_native_refs_for_event(event_id)
    )
    relations = await storage.list_relations(event_id)

    # -- Source classification --
    sources = {getattr(r, "source", "live") for r in receipts}
    has_live = "live" in sources
    has_non_live = bool(sources - {"live"})
    if has_live and has_non_live:
        source = "mixed"
    elif has_non_live:
        # Use the first non-live source found.
        for r in receipts:
            s = getattr(r, "source", "live")
            if s != "live":
                source = s
                break
        else:
            source = "live"
    else:
        source = "live"

    # -- Replay-run grouping --
    replay_runs: dict[str, list[DeliveryReceipt]] = {}
    for r in receipts:
        run_id = r.replay_run_id
        if run_id is not None and r.source == "replay":
            replay_runs.setdefault(run_id, []).append(r)

    # -- Delegate timeline construction --
    timeline_entries = _assemble_event_entries(
        event, receipts, native_refs, relations,
    )

    return {
        "event": event,
        "receipts": receipts,
        "native_refs": native_refs,
        "relations": relations,
        "replay_runs": replay_runs,
        "source": source,
        "timeline_entries": timeline_entries,
        "ordering_guarantees": ORDERING_GUARANTEES,
    }


# ---------------------------------------------------------------------------
# assemble_replay_timeline
# ---------------------------------------------------------------------------


async def assemble_replay_timeline(
    storage: StorageBackend,
    replay_run_id: str,
) -> dict[str, Any] | None:
    """Fetch all data for *replay_run_id* and assemble a replay timeline.

    Returns ``None`` when no receipts exist for the given run ID.

    The returned dict contains:

    - **replay_run_id**: the run ID passed by the caller.
    - **receipts**: ``list[DeliveryReceipt]`` ordered by ``sequence ASC``.
    - **events**: ``dict[str, CanonicalEvent]`` — referenced events
      (deduplicated, keyed by event_id).
    - **source**: always ``"replay"``.
    - **timeline_entries**: flat list built by
      :func:`medre.runtime.trace.assemble_replay_timeline`.
    """
    receipts: list[DeliveryReceipt] = (
        await storage.list_receipts_by_replay_run(replay_run_id)
    )
    if not receipts:
        return None

    # Build event cache for all referenced events.
    event_ids = list(dict.fromkeys(r.event_id for r in receipts))
    event_cache: dict[str, CanonicalEvent] = {}
    for eid in event_ids:
        ev = await storage.get(eid)
        if ev is not None:
            event_cache[eid] = ev

    timeline_entries = _assemble_replay_entries(
        replay_run_id, receipts, event_cache,
    )

    return {
        "replay_run_id": replay_run_id,
        "receipts": receipts,
        "events": event_cache,
        "source": "replay",
        "timeline_entries": timeline_entries,
    }


# ---------------------------------------------------------------------------
# assemble_storage_summary
# ---------------------------------------------------------------------------


async def assemble_storage_summary(storage: StorageBackend) -> dict[str, Any]:
    """Return aggregate counts and ordering documentation for *storage*.

    Uses existing public count methods where available; falls back to
    lightweight SQL for counts not exposed as dedicated methods.

    Returns a dict with:

    - **event_count**: total events.
    - **receipt_count**: total receipts.
    - **receipt_count_by_source**: ``{"live": int, "replay": int, "retry": int}``.
    - **native_ref_count**: total native message refs.
    - **replay_run_count**: distinct non-null ``replay_run_id`` values.
    - **ordering**: reference to :data:`ORDERING_GUARANTEES`.
    """
    event_count = await storage.count_events()
    receipt_count = await storage.count_receipts()

    # Receipt count by source (live vs replay vs retry).
    receipt_by_source: dict[str, int] = {
        "live": await storage.count_receipts_by_source("live"),
        "replay": await storage.count_receipts_by_source("replay"),
        "retry": await storage.count_receipts_by_source("retry"),
    }

    # Native ref count.
    native_ref_count = await storage.count_native_refs()

    # Distinct replay run count.
    replay_run_count = await storage.count_replay_runs()

    return {
        "event_count": event_count,
        "receipt_count": receipt_count,
        "receipt_count_by_source": receipt_by_source,
        "native_ref_count": native_ref_count,
        "replay_run_count": replay_run_count,
        "ordering": ORDERING_GUARANTEES,
    }
