"""Timeline assembly for medre trace commands.

Builds deterministic, chronological timelines from storage evidence
(events, receipts, native refs, relations).  All output is JSON-safe
and bounded to prevent unbounded memory use.

This module is the sole authority for timeline construction logic used
by ``medre trace event`` and ``medre trace replay`` CLI commands.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import msgspec

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
)

# Maximum timeline entries returned by assembly functions.
_MAX_TIMELINE_ENTRIES: int = 1000


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


# ---------------------------------------------------------------------------
# Event timeline
# ---------------------------------------------------------------------------


def assemble_event_timeline(
    event: CanonicalEvent,
    receipts: list[DeliveryReceipt],
    native_refs: list[NativeMessageRef],
    relations: list[EventRelation],
) -> list[dict[str, Any]]:
    """Assemble a chronological timeline for a single event.

    Combines the event itself, its delivery receipts, native message
    refs, and relations into a single sorted timeline.  Entries are
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

    Returns
    -------
    list[dict[str, Any]]
        Chronologically sorted timeline entries.  Each entry is a dict
        with keys ``timestamp``, ``ordinal``, ``entry_type``, ``data``.
    """
    entries: list[dict[str, Any]] = []

    # Relations precede the event itself (they are structural metadata
    # that exists at event creation time).
    for i, rel in enumerate(relations):
        entries.append(
            _timeline_entry(
                timestamp=event.timestamp,
                ordinal=-(len(relations) - i),
                entry_type="relation",
                data={"relation_type": rel.relation_type},
            )
        )

    # The event itself.
    entries.append(
        _timeline_entry(
            timestamp=event.timestamp,
            ordinal=0,
            entry_type="event",
            data={
                "event_id": event.event_id,
                "event_kind": event.event_kind,
                "source_adapter": event.source_adapter,
                "source_channel_id": event.source_channel_id,
            },
        )
    )

    # Native message refs — materialisation evidence.
    for i, nref in enumerate(native_refs):
        entries.append(
            _timeline_entry(
                timestamp=nref.created_at,
                ordinal=i + 1,
                entry_type="native_ref",
                data={
                    "id": nref.id,
                    "event_id": nref.event_id,
                    "adapter": nref.adapter,
                    "native_channel_id": nref.native_channel_id,
                    "native_message_id": nref.native_message_id,
                    "native_thread_id": nref.native_thread_id,
                    "direction": nref.direction,
                },
            )
        )

    # Delivery receipts — outbound delivery evidence.
    for receipt in receipts:
        entries.append(
            _timeline_entry(
                timestamp=receipt.created_at,
                ordinal=receipt.sequence,
                entry_type="receipt",
                data={
                    "receipt_id": receipt.receipt_id,
                    "event_id": receipt.event_id,
                    "route_id": receipt.route_id,
                    "delivery_plan_id": receipt.delivery_plan_id,
                    "target_adapter": receipt.target_adapter,
                    "status": receipt.status,
                    "failure_kind": None,
                    "error": receipt.error,
                    "attempt_number": receipt.attempt_number,
                    "source": receipt.source,
                    "replay_run_id": receipt.replay_run_id,
                    "native_message_id": receipt.adapter_message_id,
                    "native_channel_id": None,
                },
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
) -> dict[str, Any]:
    """Assemble a replay timeline for a specific replay run.

    Combines all receipts produced by a replay run with their
    corresponding events into a structured timeline.

    Parameters
    ----------
    run_id:
        The replay run ID to assemble the timeline for.
    receipts:
        All delivery receipts with ``replay_run_id == run_id``.
    event_cache:
        Mapping of event_id → CanonicalEvent for the events referenced
        by the receipts.  Events that are not in the cache are
        gracefully omitted from the timeline with a ``partial`` status.

    Returns
    -------
    dict[str, Any]
        A dict with keys ``run_id``, ``status``, ``receipt_count``,
        ``event_ids``, ``timeline``.
    """
    if not receipts:
        return {
            "run_id": run_id,
            "status": "empty",
            "receipt_count": 0,
            "event_ids": [],
            "missing_event_ids": [],
            "duplicate_send_caveat": (
                "Replay does not deduplicate.  Adapters that already "
                "delivered an event may produce duplicate sends."
            ),
            "timeline": [],
        }

    # Collect unique event IDs referenced by receipts.
    event_ids = list(dict.fromkeys(r.event_id for r in receipts))

    # Determine partial status: some events may be missing from cache.
    missing = [eid for eid in event_ids if eid not in event_cache]
    status = "partial" if missing else "complete"

    timeline_entries: list[dict[str, Any]] = []

    for receipt in receipts:
        entry: dict[str, Any] = {
            "timestamp": _to_iso(receipt.created_at),
            "ordinal": receipt.sequence,
            "entry_type": "receipt",
            "data": {
                "receipt_id": receipt.receipt_id,
                "event_id": receipt.event_id,
                "route_id": receipt.route_id,
                "delivery_plan_id": receipt.delivery_plan_id,
                "target_adapter": receipt.target_adapter,
                "status": receipt.status,
                "failure_kind": None,
                "error": receipt.error,
                "attempt_number": receipt.attempt_number,
                "source": receipt.source,
                "replay_run_id": receipt.replay_run_id,
                "native_message_id": receipt.adapter_message_id,
                "native_channel_id": None,
            },
        }
        timeline_entries.append(entry)

        # If the referenced event is in cache, include a summary.
        event = event_cache.get(receipt.event_id)
        if event is not None:
            timeline_entries.append(
                {
                    "timestamp": _to_iso(event.timestamp),
                    "ordinal": receipt.sequence + 1,
                    "entry_type": "event_summary",
                    "data": {
                        "event_id": event.event_id,
                        "event_kind": event.event_kind,
                        "source_adapter": event.source_adapter,
                    },
                }
            )

    # Sort by (timestamp, ordinal).
    timeline_entries.sort(key=lambda e: (e["timestamp"], e["ordinal"]))

    # Bound to maximum entries.
    if len(timeline_entries) > _MAX_TIMELINE_ENTRIES:
        timeline_entries = timeline_entries[:_MAX_TIMELINE_ENTRIES]

    return {
        "run_id": run_id,
        "status": status,
        "receipt_count": len(receipts),
        "event_ids": event_ids,
        "missing_event_ids": missing,
        "duplicate_send_caveat": (
            "Replay does not deduplicate.  Adapters that already "
            "delivered an event may produce duplicate sends."
        ),
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
