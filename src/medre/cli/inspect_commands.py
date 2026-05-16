"""Inspect CLI commands: read-only storage queries for events, receipts, native refs."""
from __future__ import annotations

import json
import sys

import msgspec

from .exit_codes import EXIT_NOT_FOUND, EXIT_CONFIG, EXIT_BUILD
from .json import _struct_to_json
from .storage_helpers import _open_readonly_storage


async def _inspect_event(
    config_path: str | None,
    event_id: str,
    *,
    storage_path: str | None = None,
) -> None:
    """Look up and print a canonical event by its ID."""
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    try:
        event = await storage.get(event_id)
        if event is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)
        print(_struct_to_json(event))
    finally:
        await storage.close()


async def _inspect_receipts(
    config_path: str | None,
    event_id: str | None,
    replay_run_id: str | None,
    *,
    storage_path: str | None = None,
) -> None:
    """List delivery receipts for an event or replay run."""
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    try:
        if event_id is not None:
            receipts = await storage.list_receipts_for_event(event_id)
        elif replay_run_id is not None:
            receipts = await storage.list_receipts_by_replay_run(replay_run_id)
        else:
            print("Error: specify --event or --replay-run", file=sys.stderr)
            sys.exit(EXIT_CONFIG)
        print(_struct_to_json(receipts))
    finally:
        await storage.close()


async def _inspect_native_ref(
    config_path: str | None,
    adapter: str,
    channel: str | None,
    message: str,
    *,
    storage_path: str | None = None,
) -> None:
    """Resolve a native message reference to a canonical event."""
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    try:
        event_id = await storage.resolve_native_ref(adapter, channel, message)
        if event_id is None:
            print(
                f"Error: native ref not found: adapter={adapter!r}, "
                f"channel={channel!r}, message={message!r}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)
        # Fetch the full event for richer output.
        event = await storage.get(event_id)
        result: dict[str, object] = {
            "adapter": adapter,
            "native_channel_id": channel,
            "native_message_id": message,
            "event_id": event_id,
        }
        if event is not None:
            result["event"] = json.loads(msgspec.json.encode(event))
        print(json.dumps(result, sort_keys=True, indent=2))
    finally:
        await storage.close()
