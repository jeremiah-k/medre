"""Timeline assembly for medre trace commands.

Builds deterministic, chronological timelines from storage evidence
(events, receipts, native refs, relations).  All output is JSON-safe
and bounded to prevent unbounded memory use.

**Persistence boundary:** This module is the sole authority for timeline
construction logic used by ``medre trace event`` and ``medre trace replay``
CLI commands.  It is pure — it accepts already-loaded data objects and
never reads from or writes to storage directly.  All state labels in
timeline output are derived display values, not authoritative lifecycle
states.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import msgspec

from medre.core.engine.pipeline.delivery_state import TERMINAL_OUTBOX_STATUSES
from medre.core.events import (
    CanonicalEvent,
    DeliveryObservation,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
)
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.reporting import (
    delivery_observation_to_report_dict,
    delivery_receipt_to_report_dict,
    native_ref_to_report_dict,
)

# Maximum timeline entries returned by assembly functions.
_MAX_TIMELINE_ENTRIES: int = 1000
_REPLAY_DUPLICATE_CAVEAT = (
    "A non-empty replay run ID atomically claims one target generation in "
    "durable storage. Replay does not deduplicate prior live delivery or "
    "different/empty run IDs, and ambiguous transport attempts may still be "
    "redispatched by retry or recovery."
)


# ---------------------------------------------------------------------------
# JSON-safe conversion helpers
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime) -> str:
    """Convert a datetime to an ISO 8601 string (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _sanitize_for_json(obj: object) -> Any:
    """Recursively convert a value to a JSON-safe representation.

    Handles msgspec Structs (via encode/decode round-trip), datetimes
    (to ISO strings), and normal Python containers.
    """
    if isinstance(obj, datetime):
        return _to_iso(obj)
    if isinstance(obj, msgspec.Struct):
        raw = msgspec.json.encode(obj)
        return json.loads(raw)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    return obj


def _timeline_entry(
    timestamp: datetime,
    ordinal: int,
    entry_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Build a single timeline entry dict."""
    return {
        "timestamp": _to_iso(timestamp),
        "ordinal": ordinal,
        "entry_type": entry_type,
        "data": _sanitize_for_json(data),
    }


def _outbox_generation_entry(
    item: DeliveryOutboxItem,
    index: int,
) -> dict[str, Any]:
    """Return one deterministic durable outbox-admission timeline entry."""
    timestamp = item.created_at or item.updated_at or "1970-01-01T00:00:00+00:00"
    return {
        "timestamp": str(timestamp),
        # Outbox rows have no global sequence. A negative namespace keeps
        # admission before same-timestamp receipt sequences while preserving
        # deterministic order among sibling generations.
        "ordinal": -1_000_000 + index,
        "entry_type": "outbox_generation",
        "data": {
            "outbox_id": item.outbox_id,
            "event_id": item.event_id,
            "delivery_plan_id": item.delivery_plan_id,
            "target_adapter": item.target_adapter,
            "target_channel": item.target_channel,
            "attempt_number": item.attempt_number,
            "active_attempt": item.active_attempt,
            "status": item.status,
            "receipt_id": item.receipt_id,
            "replay_run_id": item.replay_run_id,
            "created_at": str(item.created_at) if item.created_at is not None else None,
            "updated_at": str(item.updated_at) if item.updated_at is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# Event timeline
# ---------------------------------------------------------------------------


def assemble_event_timeline(
    event: CanonicalEvent,
    receipts: list[DeliveryReceipt],
    native_refs: list[NativeMessageRef],
    relations: list[EventRelation],
    observations: list[DeliveryObservation] | None = None,
    outbox_items: list[DeliveryOutboxItem] | None = None,
) -> list[dict[str, Any]]:
    """Assemble a chronological timeline for a single event.

    Combines the event itself, its delivery receipts, post-handoff
    observations, native message refs, and relations.  Entries are
    ordered by ``(timestamp, ordinal)`` and capped at
    ``_MAX_TIMELINE_ENTRIES``.

    Parameters
    ----------
    event:
        The canonical event at the centre of the timeline.
    receipts:
        Delivery receipts for this event.
    native_refs:
        Native message refs materialised for this event.
    relations:
        Event relations attached to this event.
    observations:
        Append-only post-handoff transport observations for exact delivery
        attempts.  These are evidence only and do not alter receipt state.
    outbox_items:
        Durable target generations for this event. These make named replay
        admission visible before its first receipt without treating replay
        origin as a dispatch source.

    Returns
    -------
    list[dict[str, Any]]
        Chronologically sorted timeline entries.  Each entry is a dict
        with keys ``timestamp``, ``ordinal``, ``entry_type``, ``data``.
    """
    entries: list[dict[str, Any]] = []

    # Relations precede the event itself (they are structural metadata
    # that exists at event creation time).  Concise relation entries
    # surface the target identity, key discriminator, fallback text,
    # and a short native-ref summary — without dumping huge metadata.
    for i, rel in enumerate(relations):
        rel_data: dict[str, Any] = {
            "relation_type": rel.relation_type,
            "target_event_id": rel.target_event_id,
        }
        if rel.key is not None:
            rel_data["key"] = rel.key
        if rel.fallback_text is not None:
            rel_data["fallback_text"] = rel.fallback_text
        if rel.target_native_ref is not None:
            nref = rel.target_native_ref
            rel_data["target_native_ref"] = {
                "adapter": nref.adapter,
                "native_channel_id": nref.native_channel_id,
                "native_message_id": nref.native_message_id,
            }
        entries.append(
            _timeline_entry(
                timestamp=event.timestamp,
                ordinal=-(len(relations) - i),
                entry_type="relation",
                data=rel_data,
            )
        )

    # The event itself — includes conversation identity fields so operators
    # can answer "what conversation/thread does this belong to?" without
    # loading the full event object.
    event_data: dict[str, Any] = {
        "event_id": event.event_id,
        "event_kind": event.event_kind,
        "source_adapter": event.source_adapter,
        "source_channel_id": event.source_channel_id,
    }
    # Optional identity fields — only included when present so that JSON
    # output stays compact by omitting null conversation-identity keys.
    if event.root_event_id is not None:
        event_data["root_event_id"] = event.root_event_id
    if event.conversation_id is not None:
        event_data["conversation_id"] = event.conversation_id
    if event.parent_event_id is not None:
        event_data["parent_event_id"] = event.parent_event_id
    _trace_id = getattr(event, "trace_id", None)
    if _trace_id is not None:
        event_data["trace_id"] = _trace_id

    entries.append(
        _timeline_entry(
            timestamp=event.timestamp,
            ordinal=0,
            entry_type="event",
            data=event_data,
        )
    )

    # Durable outbox generations — mutable operational evidence.  These are
    # snapshot-at-read rows, not immutable lifecycle transitions; created_at is
    # therefore used only to place admission in the timeline.
    for index, item in enumerate(outbox_items or []):
        entries.append(_outbox_generation_entry(item, index))

    # Native message refs — materialisation evidence.
    for i, nref in enumerate(native_refs):
        entries.append(
            _timeline_entry(
                timestamp=nref.created_at,
                ordinal=i + 1,
                entry_type="native_ref",
                data={
                    **native_ref_to_report_dict(nref),
                    "id": nref.id,
                    "event_id": nref.event_id,
                    "native_thread_id": nref.native_thread_id,
                },
            )
        )

    # Delivery receipts — outbound delivery evidence.
    for receipt in receipts:
        receipt_data = delivery_receipt_to_report_dict(receipt)
        entries.append(
            _timeline_entry(
                timestamp=receipt.created_at,
                ordinal=receipt.sequence,
                entry_type="receipt",
                data={
                    **receipt_data,
                    "replay_run_id": receipt.replay_run_id,
                },
            )
        )

    # Post-handoff delivery observations — later transport evidence.
    # Use a high ordinal namespace so an observation and receipt with the exact
    # same timestamp still order deterministically without pretending their
    # independent SQLite sequences share one global counter.
    for observation in observations or []:
        entries.append(
            _timeline_entry(
                timestamp=observation.observed_at,
                ordinal=1_000_000_000 + observation.sequence,
                entry_type="delivery_observation",
                data=delivery_observation_to_report_dict(observation),
            )
        )

    # Sort by (timestamp, ordinal) for deterministic chronological order.
    entries.sort(key=lambda e: (e["timestamp"], e["ordinal"]))

    # Bound to maximum entries.
    if len(entries) > _MAX_TIMELINE_ENTRIES:
        entries = entries[:_MAX_TIMELINE_ENTRIES]

    return entries


# ---------------------------------------------------------------------------
# Replay timeline
# ---------------------------------------------------------------------------


def assemble_replay_timeline(
    run_id: str,
    receipts: list[DeliveryReceipt],
    event_cache: dict[str, CanonicalEvent],
    outbox_items: list[DeliveryOutboxItem] | None = None,
) -> dict[str, Any]:
    """Assemble durable evidence for one named replay run.

    A replay run becomes durable when an outbox generation is admitted, before
    the first receipt necessarily exists.  The timeline therefore combines both
    outbox-generation evidence and receipts.  ``source`` remains per-attempt
    dispatch mechanism; the run itself has replay origin regardless of whether a
    later receipt was produced by the retry worker.
    """
    outbox_items = outbox_items or []
    if not receipts and not outbox_items:
        return {
            "run_id": run_id,
            "origin": "replay",
            "status": "empty",
            "receipt_count": 0,
            "outbox_count": 0,
            "sources_seen": [],
            "event_ids": [],
            "missing_event_ids": [],
            "duplicate_send_caveat": _REPLAY_DUPLICATE_CAVEAT,
            "timeline": [],
        }

    event_ids = list(
        dict.fromkeys(
            [item.event_id for item in outbox_items]
            + [receipt.event_id for receipt in receipts]
        )
    )
    missing = [eid for eid in event_ids if eid not in event_cache]
    active_outbox = [
        item for item in outbox_items if item.status not in TERMINAL_OUTBOX_STATUSES
    ]
    if missing:
        status = "partial"
    elif outbox_items and not receipts:
        status = "admitted"
    elif active_outbox:
        status = "active"
    else:
        status = "complete"

    timeline_entries: list[dict[str, Any]] = []

    # Outbox admission is the earliest durable named-run fact.  Persisted rows
    # always have created_at; the fallback keeps synthetic unit fixtures safe.
    for index, item in enumerate(outbox_items):
        timeline_entries.append(_outbox_generation_entry(item, index))

    for receipt in receipts:
        receipt_data = delivery_receipt_to_report_dict(receipt)
        timeline_entries.append(
            {
                "timestamp": _to_iso(receipt.created_at),
                "ordinal": receipt.sequence,
                "entry_type": "receipt",
                "data": {
                    **receipt_data,
                    "replay_run_id": receipt.replay_run_id,
                },
            }
        )

    # Include one canonical-event summary per event, regardless of whether the
    # run has reached receipt creation yet.
    for index, event_id in enumerate(event_ids):
        event = event_cache.get(event_id)
        if event is None:
            continue
        timeline_entries.append(
            {
                "timestamp": _to_iso(event.timestamp),
                "ordinal": -500_000 + index,
                "entry_type": "event_summary",
                "data": {
                    "event_id": event.event_id,
                    "event_kind": event.event_kind,
                    "source_adapter": event.source_adapter,
                },
            }
        )

    timeline_entries.sort(key=lambda entry: (entry["timestamp"], entry["ordinal"]))
    if len(timeline_entries) > _MAX_TIMELINE_ENTRIES:
        timeline_entries = timeline_entries[:_MAX_TIMELINE_ENTRIES]

    return {
        "run_id": run_id,
        "origin": "replay",
        "status": status,
        "receipt_count": len(receipts),
        "outbox_count": len(outbox_items),
        "sources_seen": sorted({receipt.source for receipt in receipts}),
        "event_ids": event_ids,
        "missing_event_ids": missing,
        "duplicate_send_caveat": _REPLAY_DUPLICATE_CAVEAT,
        "timeline": timeline_entries,
    }


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def timeline_to_json(timeline: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Serialise a timeline (list of entries or replay dict) to deterministic JSON.

    Output is sorted by key and indented for readability.
    """
    return json.dumps(timeline, sort_keys=True, indent=2, default=str)
