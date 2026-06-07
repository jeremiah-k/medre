"""Trace CLI commands: read-only chronological timeline assembly for events and replay runs.

All trace commands open storage in read-only mode and produce derived
projections from authoritative persisted rows.  They never mutate storage
or alter lifecycle state.
"""

from __future__ import annotations

import sys

import medre.runtime.timeline as _timeline
from medre.runtime.trace import timeline_to_json

from .exit_codes import EXIT_NOT_FOUND
from .storage_helpers import _open_readonly_storage


async def _trace_event(
    event_id: str,
    json_output: bool,
    *,
    storage_path: str,
) -> None:
    """Assemble and print a chronological timeline for a single event.

    Read-only derived view: opens storage in read-only mode and queries
    persisted events, receipts, native refs, and relations.  Does not
    mutate storage.
    """
    storage = await _open_readonly_storage(storage_path)
    _exit_code: int | None = None
    try:
        result = await _timeline.assemble_event_timeline(storage, event_id)
        if result is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND

        if _exit_code is None:
            assert result is not None  # guaranteed when _exit_code is None
            event = result["event"]
            timeline = result["timeline_entries"]

            if json_output:
                print(timeline_to_json(timeline))
            else:
                # Human-readable summary.
                print(
                    f"Event: {event_id} ({event.event_kind}) from {event.source_adapter}"
                )
                print(f"Timeline ({len(timeline)} entries):")
                print()
                for entry in timeline:
                    ts = entry["timestamp"]
                    etype = entry["entry_type"]
                    data = entry["data"]
                    if etype == "relation":
                        rtype = data.get("relation_type", "")
                        target_eid = data.get("target_event_id")
                        parts = [rtype]
                        if target_eid:
                            parts.append(f"-> {target_eid}")
                        key = data.get("key")
                        if key:
                            parts.append(f"key={key}")
                        print(f"  {ts}  [{etype}] {' '.join(parts)}")
                    elif etype == "event":
                        kind = data.get("event_kind", "")
                        src = data.get("source_adapter", "")
                        print(f"  {ts}  [{etype}] {kind} from {src}")
                    elif etype == "native_ref":
                        direction = data.get("direction", "")
                        adapter = data.get("adapter", "")
                        msg_id = data.get("native_message_id", "")
                        print(f"  {ts}  [{etype}] {direction} via {adapter}: {msg_id}")
                    elif etype == "receipt":
                        status = data.get("status", "")
                        target = data.get("target_adapter", "")
                        attempt = data.get("attempt_number", 1)
                        plan_id = data.get("delivery_plan_id", "")
                        line = f"  {ts}  [{etype}] {status} -> {target}"
                        if plan_id:
                            line += f" plan={plan_id}"
                        channel = data.get("target_channel") or data.get(
                            "native_channel_id"
                        )
                        if channel:
                            line += f" channel={channel}"
                        route = data.get("route_id")
                        if route:
                            line += f" route={route}"
                        line += f" (attempt {attempt})"
                        error = data.get("error")
                        if error:
                            truncated = (
                                error if len(error) <= 80 else error[:77] + "..."
                            )
                            line += f" error={truncated}"
                        print(line)
                    else:
                        print(f"  {ts}  [{etype}] {data}")

                # Summary: receipt counts by status, native ref count, relations count.
                receipt_entries = [e for e in timeline if e["entry_type"] == "receipt"]
                status_counts: dict[str, int] = {}
                for re in receipt_entries:
                    s = re["data"].get("status", "unknown")
                    status_counts[s] = status_counts.get(s, 0) + 1
                nref_count = sum(1 for e in timeline if e["entry_type"] == "native_ref")
                rel_count = sum(1 for e in timeline if e["entry_type"] == "relation")
                print()
                print("Summary:")
                status_parts = ", ".join(
                    f"{status}: {count}"
                    for status, count in sorted(status_counts.items())
                )
                print(f"  Receipts: {status_parts or 'none'}")
                print(f"  Native refs: {nref_count}")
                print(f"  Relations: {rel_count}")
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _trace_replay(
    run_id: str,
    json_output: bool,
    *,
    storage_path: str,
) -> None:
    """Assemble and print a chronological timeline for a replay run.

    Read-only derived view: opens storage in read-only mode and queries
    persisted replay receipts.  Does not mutate storage.
    """
    storage = await _open_readonly_storage(storage_path)
    _exit_code: int | None = None
    try:
        result = await _timeline.assemble_replay_timeline(storage, run_id)
        if result is None:
            print(
                f"Error: no receipts found for replay run: {run_id}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND

        if _exit_code is None:
            assert result is not None  # guaranteed when _exit_code is None
            replay_data = result["timeline_entries"]

            if json_output:
                print(timeline_to_json(replay_data))
            else:
                # Human-readable summary.
                print(f"Replay timeline: {run_id}")
                print(f"  Status:  {replay_data['status']}")
                print(f"  Receipts: {replay_data['receipt_count']}")
                print(f"  Events:  {len(replay_data['event_ids'])}")
                print()
                for entry in replay_data["timeline"]:
                    ts = entry["timestamp"]
                    etype = entry["entry_type"]
                    data = entry["data"]
                    if etype == "receipt":
                        status = data.get("status", "")
                        target = data.get("target_adapter", "")
                        eid = data.get("event_id", "")
                        line = f"  {ts}  [{etype}] {status} -> {target}"
                        channel = data.get("target_channel") or data.get(
                            "native_channel_id"
                        )
                        if channel:
                            line += f" channel={channel}"
                        route = data.get("route_id")
                        if route:
                            line += f" route={route}"
                        line += f" (event: {eid})"
                        print(line)
                    elif etype == "event_summary":
                        kind = data.get("event_kind", "")
                        src = data.get("source_adapter", "")
                        print(f"  {ts}  [{etype}] {kind} from {src}")
                    else:
                        print(f"  {ts}  [{etype}] {data}")
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)
