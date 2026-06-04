"""Inspect CLI commands: read-only storage queries for events, receipts, native refs.

Supports augmented inspection via ``--timeline``, ``--evidence``, and ``--recovery``
flags on ``inspect event``, and an ``inspect replay`` subcommand — all read-only,
all deterministic JSON, no runtime start, no storage mutation.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import msgspec

import medre.runtime.timeline as _timeline
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.trace import timeline_to_json

from .exit_codes import EXIT_CONFIG, EXIT_NOT_FOUND
from .json import _struct_to_json
from .storage_helpers import _open_readonly_storage


async def _inspect_event(
    config_path: str | None,
    event_id: str,
    *,
    storage_path: str | None = None,
    timeline: bool = False,
    evidence: bool = False,
    recovery: bool = False,
) -> None:
    """Look up and print a canonical event by its ID.

    When *timeline*, *evidence*, or *recovery* flags are set, the output
    is augmented with the corresponding data sections.  When no flags are
    set, the original event-only JSON is printed (default behaviour).
    """
    # Fast path: no augmentation flags — preserve exact existing behaviour.
    if not (timeline or evidence or recovery):
        storage = await _open_readonly_storage(config_path, storage_path=storage_path)
        _exit_code: int | None = None
        try:
            event = await storage.get(event_id)
            if event is None:
                print(
                    f"Error: event not found: {event_id}",
                    file=sys.stderr,
                )
                _exit_code = EXIT_NOT_FOUND
            else:
                print(_struct_to_json(event))
        finally:
            await storage.close()
        if _exit_code is not None:
            sys.exit(_exit_code)
        return

    # Augmented path: build a compound result.
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    _exit_code: int | None = None
    try:
        event = await storage.get(event_id)
        if event is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND

        if _exit_code is None:
            result: dict[str, Any] = {
                "event": json.loads(msgspec.json.encode(event)),
            }

            if timeline:
                tl_result = await _timeline.assemble_event_timeline(storage, event_id)
                # Event exists (we already checked), so tl_result is not None.
                result["timeline"] = tl_result["timeline_entries"] if tl_result else []

            if evidence:
                bundle = await collect_evidence_bundle(
                    config_path,
                    event_id=event_id,
                    storage_path=storage_path,
                )
                result["evidence"] = bundle

            if recovery:
                from .recover_commands import _build_event_recovery_runbook

                runbook = await _build_event_recovery_runbook(storage, event_id)
                # Event exists, so runbook is not None.
                result["recovery"] = runbook

            print(json.dumps(result, sort_keys=True, indent=2, default=str))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_receipts(
    config_path: str | None,
    event_id: str | None,
    replay_run_id: str | None,
    *,
    storage_path: str | None = None,
) -> None:
    """List delivery receipts for an event or replay run."""
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    _exit_code: int | None = None
    try:
        if event_id is not None:
            receipts = await storage.list_receipts_for_event(event_id)
        elif replay_run_id is not None:
            receipts = await storage.list_receipts_by_replay_run(replay_run_id)
        else:
            print("Error: specify --event or --replay-run", file=sys.stderr)
            _exit_code = EXIT_CONFIG
            receipts = None
        if _exit_code is None:
            print(_struct_to_json(receipts))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


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
    _exit_code: int | None = None
    try:
        event_id = await storage.resolve_native_ref(adapter, channel, message)
        if event_id is None:
            print(
                f"Error: native ref not found: adapter={adapter!r}, "
                f"channel={channel!r}, message={message!r}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND

        if _exit_code is None:
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
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_replay(
    config_path: str | None,
    run_id: str,
    *,
    storage_path: str | None = None,
) -> None:
    """Inspect a replay run: read-only timeline via storage.

    Outputs deterministic JSON with the replay timeline, matching the
    shape produced by ``medre trace replay --json``.
    """
    storage = await _open_readonly_storage(config_path, storage_path=storage_path)
    _exit_code: int | None = None
    try:
        result = await _timeline.assemble_replay_timeline(storage, run_id)
        if result is None:
            print(
                f"Error: no receipts found for replay run: {run_id}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND
        else:
            print(timeline_to_json(result["timeline_entries"]))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)
