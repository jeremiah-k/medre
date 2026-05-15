"""Trace CLI commands: chronological timeline assembly for events and replay runs."""
from __future__ import annotations

import sys

from medre.runtime import timeline as _timeline
from medre.runtime.trace import timeline_to_json

from .exit_codes import EXIT_NOT_FOUND
from .storage_helpers import _open_readonly_storage


async def _trace_event(
    config_path: str | None,
    event_id: str,
    json_output: bool,
) -> None:
    """Assemble and print a chronological timeline for a single event."""
    storage = await _open_readonly_storage(config_path)
    try:
        result = await _timeline.assemble_event_timeline(storage, event_id)
        if result is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

        event = result["event"]
        timeline = result["timeline_entries"]

        if json_output:
            print(timeline_to_json(timeline))
        else:
            # Human-readable summary.
            print(f"Event: {event_id} ({event.event_kind}) from {event.source_adapter}")
            print(f"Timeline ({len(timeline)} entries):")
            print()
            for entry in timeline:
                ts = entry["timestamp"]
                etype = entry["entry_type"]
                data = entry["data"]
                if etype == "relation":
                    rtype = data.get("relation_type", "")
                    print(f"  {ts}  [{etype}] {rtype}")
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
                    line = f"  {ts}  [{etype}] {status} -> {target} (attempt {attempt})"
                    error = data.get("error")
                    if error:
                        truncated = error if len(error) <= 80 else error[:77] + "..."
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
                f"{status}: {count}" for status, count in sorted(status_counts.items())
            )
            print(f"  Receipts: {status_parts or 'none'}")
            print(f"  Native refs: {nref_count}")
            print(f"  Relations: {rel_count}")
    finally:
        await storage.close()


async def _trace_replay(
    config_path: str | None,
    run_id: str,
    json_output: bool,
) -> None:
    """Assemble and print a chronological timeline for a replay run."""
    storage = await _open_readonly_storage(config_path)
    try:
        result = await _timeline.assemble_replay_timeline(storage, run_id)
        if result is None:
            print(
                f"Error: no receipts found for replay run: {run_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

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
                    print(f"  {ts}  [{etype}] {status} -> {target} (event: {eid})")
                elif etype == "event_summary":
                    kind = data.get("event_kind", "")
                    src = data.get("source_adapter", "")
                    print(f"  {ts}  [{etype}] {kind} from {src}")
                else:
                    print(f"  {ts}  [{etype}] {data}")
    finally:
        await storage.close()
