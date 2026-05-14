"""Trace CLI commands: chronological timeline assembly for events and replay runs."""
from __future__ import annotations

import json as _json
import sys

from medre.runtime.trace import (
    assemble_event_timeline,
    assemble_replay_timeline,
    timeline_to_json,
)

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
        event = await storage.get(event_id)
        if event is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

        receipts = await storage.list_receipts_for_event(event_id)
        native_refs = await storage.list_native_refs_for_event(event_id)
        relations = await storage.list_relations(event_id)

        timeline = assemble_event_timeline(event, receipts, native_refs, relations)

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
        receipts = await storage.list_receipts_by_replay_run(run_id)
        if not receipts:
            print(
                f"Error: no receipts found for replay run: {run_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

        # Build event cache for all referenced events.
        event_ids = list(dict.fromkeys(r.event_id for r in receipts))
        event_cache = {}
        for eid in event_ids:
            event = await storage.get(eid)
            if event is not None:
                event_cache[eid] = event

        result = assemble_replay_timeline(run_id, receipts, event_cache)

        if json_output:
            print(timeline_to_json(result))
        else:
            # Human-readable summary.
            print(f"Replay timeline: {run_id}")
            print(f"  Status:  {result['status']}")
            print(f"  Receipts: {result['receipt_count']}")
            print(f"  Events:  {len(result['event_ids'])}")
            print()
            for entry in result["timeline"]:
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
