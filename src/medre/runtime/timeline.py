"""Shared timeline/lineage assembly layer for medre.

Centralises the fetch+assemble pattern duplicated across trace, evidence,
recover, and inspect commands.  Delegates timeline construction ordering
to :mod:`medre.runtime.trace`; this module adds storage-backed async
fetch, source classification (none/live/retry/replay/mixed), replay-run grouping,
and ordering guarantees.

All functions accept a :class:`~medre.core.storage.backend.StorageBackend`
instance and return plain dicts — no DTO hierarchies, no ORM, no hidden
SQL.
"""

from __future__ import annotations

from typing import Any

from medre.core.events import (
    CanonicalEvent,
    DeliveryObservation,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.storage.backend import StorageBackend
from medre.runtime.trace import assemble_event_timeline as _assemble_event_entries
from medre.runtime.trace import assemble_replay_timeline as _assemble_replay_entries

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
    "delivery_observations": {
        "order": "sequence ASC",
        "deterministic": "true",
        "note": "Append-only per-table sequence orders post-handoff evidence.",
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
    "outbox": {
        "order": "created_at ASC, outbox_id ASC",
        "deterministic": "true",
        "note": "Named replay admission is durable before its first receipt.",
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

    **Persistence boundary:** Reads from ``canonical_events``, ``delivery_receipts``,
    ``delivery_observations``, ``native_message_refs``, ``event_relations``,
    and ``delivery_outbox`` storage tables (all read-only). Never writes to
    storage. Dispatch-source classification is derived from receipt rows; named
    replay origin is derived from the union of receipts and durable outbox claims.

    The returned dict contains:

    - **event**: the :class:`CanonicalEvent` (or ``None``).
    - **receipts**: ``list[DeliveryReceipt]`` ordered by ``sequence ASC``.
    - **native_refs**: ``list[NativeMessageRef]`` ordered by
      ``created_at ASC, id ASC``.
    - **delivery_observations**: ``list[DeliveryObservation]`` ordered by
      ``sequence ASC``.
    - **relations**: ``list[EventRelation]`` ordered by ``id ASC``.
    - **replay_runs**: ``dict[str, list[DeliveryReceipt]]`` grouping
      ``replay_run_id`` → receipts belonging to that run. A key can exist with
      an empty list when the named run is durably admitted but has no receipt yet.
    - **replay_run_ids**: ordered durable named-run IDs seen on outbox claims or
      receipts.
    - **outbox_items**: durable target generations ordered by creation.
    - **source**: ``"none"`` | ``"live"`` | ``"replay"`` | ``"retry"`` |
      ``"mixed"`` — the dispatch mechanisms represented by immutable receipts.
      Durable outbox admission without a receipt reports ``"none"`` rather than
      fabricating a live dispatch.
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
    native_refs: list[NativeMessageRef] = await storage.list_native_refs_for_event(
        event_id
    )
    observations: list[DeliveryObservation] = (
        await storage.list_delivery_observations_for_event(event_id)
    )
    relations = await storage.list_relations(event_id)
    outbox_items = await storage.list_outbox_items_for_event(event_id)

    # -- Source classification --
    sources = {getattr(r, "source", "live") for r in receipts}
    has_live = "live" in sources
    has_non_live = bool(sources - {"live"})
    if not sources:
        source = "none"
    elif has_live and has_non_live:
        source = "mixed"
    elif has_non_live:
        # Use the first non-live source found.
        for r in receipts:
            s = getattr(r, "source", "live")
            if s != "live":
                source = s
                break
        else:  # pragma: no cover - guarded by ``has_non_live``
            source = "none"
    else:
        source = "live"

    # -- Replay-origin grouping --
    # Outbox admission is the earliest durable named-run fact. Preserve its
    # deterministic order, then enrich each run with immutable receipts.
    replay_runs: dict[str, list[DeliveryReceipt]] = {}
    for item in outbox_items:
        run_id = item.replay_run_id
        if run_id is not None:
            replay_runs.setdefault(run_id, [])
    for r in receipts:
        run_id = r.replay_run_id
        if run_id is not None:
            replay_runs.setdefault(run_id, []).append(r)
    replay_run_ids = list(replay_runs)

    # -- Delegate timeline construction --
    timeline_entries = _assemble_event_entries(
        event,
        receipts,
        native_refs,
        relations,
        observations,
        outbox_items,
    )

    return {
        "event": event,
        "receipts": receipts,
        "native_refs": native_refs,
        "delivery_observations": observations,
        "relations": relations,
        "outbox_items": outbox_items,
        "replay_runs": replay_runs,
        "replay_run_ids": replay_run_ids,
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
    """Fetch all durable evidence for *replay_run_id* and assemble its timeline.

    A named run is discoverable from its atomic outbox admission even when the
    process failed before producing a receipt.  Returns ``None`` only when
    neither durable outbox claims nor immutable receipts exist for the run.
    """
    receipts: list[DeliveryReceipt] = await storage.list_receipts_by_replay_run(
        replay_run_id
    )
    outbox_items = await storage.list_outbox_items_by_replay_run(replay_run_id)
    if not receipts and not outbox_items:
        return None

    event_ids = list(
        dict.fromkeys(
            [item.event_id for item in outbox_items]
            + [receipt.event_id for receipt in receipts]
        )
    )
    event_cache: dict[str, CanonicalEvent] = {}
    for event_id in event_ids:
        event = await storage.get(event_id)
        if event is not None:
            event_cache[event_id] = event

    timeline_entries = _assemble_replay_entries(
        replay_run_id,
        receipts,
        event_cache,
        outbox_items,
    )

    return {
        "replay_run_id": replay_run_id,
        "receipts": receipts,
        "outbox_items": outbox_items,
        "events": event_cache,
        "origin": "replay",
        "sources_seen": sorted({receipt.source for receipt in receipts}),
        "timeline_entries": timeline_entries,
    }


# ---------------------------------------------------------------------------
# assemble_storage_summary
# ---------------------------------------------------------------------------


async def assemble_storage_summary(storage: StorageBackend) -> dict[str, Any]:
    """Return aggregate counts and ordering documentation for *storage*.

    **Persistence boundary:** Read-only queries against storage tables.
    Never writes to storage.

    Uses existing public count methods where available; falls back to
    lightweight SQL for counts not exposed as dedicated methods.

    Returns a dict with:

    - **event_count**: total events.
    - **receipt_count**: total receipts.
    - **delivery_observation_count**: total post-handoff observations.
    - **receipt_count_by_source**: ``{"live": int, "replay": int, "retry": int}``.
    - **native_ref_count**: total native message refs.
    - **replay_run_count**: distinct durable non-empty ``replay_run_id`` values represented by receipts or outbox claims.
    - **ordering**: reference to :data:`ORDERING_GUARANTEES`.
    """
    event_count = await storage.count_events()
    receipt_count = await storage.count_receipts()
    observation_count = await storage.count_delivery_observations()

    # Receipt count by source (live vs replay vs retry).
    receipt_by_source: dict[str, int] = {
        "live": await storage.count_receipts_by_source("live"),
        "replay": await storage.count_receipts_by_source("replay"),
        "retry": await storage.count_receipts_by_source("retry"),
    }

    # Native ref count.
    native_ref_count = await storage.count_native_refs()

    # Distinct durable replay run count (receipt evidence or admitted outbox claim).
    replay_run_count = await storage.count_replay_runs()

    return {
        "event_count": event_count,
        "receipt_count": receipt_count,
        "delivery_observation_count": observation_count,
        "receipt_count_by_source": receipt_by_source,
        "native_ref_count": native_ref_count,
        "replay_run_count": replay_run_count,
        "ordering": ORDERING_GUARANTEES,
    }
