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
from medre.core.storage.backend import StorageError
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.reporting import native_ref_to_report_dict
from medre.runtime.trace import timeline_to_json

from .exit_codes import EXIT_CONFIG, EXIT_NOT_FOUND
from .json import to_json
from .storage_helpers import _open_readonly_storage


async def _inspect_event(
    event_id: str,
    *,
    storage_path: str,
    timeline: bool = False,
    evidence: bool = False,
    recovery: bool = False,
) -> None:
    """Look up and print a canonical event by its ID.

    When *timeline*, *evidence*, or *recovery* flags are set, the output
    is augmented with the corresponding data sections.  When no flags are
    set, the original event-only JSON is printed (default behaviour).
    """
    _exit_code: int | None = None

    # Fast path: no augmentation flags — preserve exact existing behaviour.
    if not (timeline or evidence or recovery):
        storage = await _open_readonly_storage(storage_path)
        try:
            event = await storage.get_metadata(event_id)
            if event is None:
                print(
                    f"Error: event not found: {event_id}",
                    file=sys.stderr,
                )
                _exit_code = EXIT_NOT_FOUND
            else:
                print(to_json(event))
        finally:
            await storage.close()
        if _exit_code is not None:
            sys.exit(_exit_code)
        return

    # Augmented path: build a compound result.
    storage = await _open_readonly_storage(storage_path)
    try:
        event = await storage.get_metadata(event_id)
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
                    None,
                    event_id=event_id,
                    storage_path=storage_path,
                )
                result["evidence"] = bundle

            if recovery:
                from .recover_commands import _build_event_recovery_runbook

                runbook = await _build_event_recovery_runbook(
                    storage, event_id, storage_path=storage_path
                )
                # Event exists, so runbook is not None.
                result["recovery"] = runbook

            print(to_json(result))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_receipts(
    event_id: str | None,
    replay_run_id: str | None,
    *,
    storage_path: str,
) -> None:
    """List delivery receipts for an event or replay run.

    Read-only: queries persisted ``delivery_receipts`` rows without mutation.
    """
    storage = await _open_readonly_storage(storage_path)
    _exit_code: int | None = None
    try:
        if event_id is not None:
            receipts = await storage.list_receipts_for_event(
                event_id, metadata_only=True
            )
        elif replay_run_id is not None:
            receipts = await storage.list_receipts_by_replay_run(
                replay_run_id, metadata_only=True
            )
        else:
            print("Error: specify --event or --replay-run", file=sys.stderr)
            _exit_code = EXIT_CONFIG
            receipts = None
        if _exit_code is None:
            print(to_json(receipts))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_native_ref(
    adapter: str,
    channel: str | None,
    message: str,
    *,
    storage_path: str,
) -> None:
    """Resolve a native message reference to a canonical event.

    Read-only: reads the real persisted ``native_message_refs`` row from
    storage via ``get_native_ref()``.  The correlation facts (adapter,
    channel, message → event_id) come directly from the authoritative
    persisted row — not reconstructed or assumed.  Uses
    ``native_ref_to_report_dict`` only for display formatting.
    """
    storage = await _open_readonly_storage(storage_path)
    _exit_code: int | None = None
    try:
        ref = await storage.get_native_ref(
            adapter, channel, message, metadata_only=True
        )
        if ref is None:
            print(
                f"Error: native ref not found: adapter={adapter!r}, "
                f"channel={channel!r}, message={message!r}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND

        if _exit_code is None:
            result: dict[str, object] = native_ref_to_report_dict(
                nref=ref,
                resolved_to_event_id=ref.event_id,
            )
            # Expose stored fields that prove provenance.
            result["id"] = ref.id
            result["created_at"] = ref.created_at.isoformat()
            result["metadata"] = ref.metadata
            # Preserve the original channel value (None when channelless)
            # rather than the helper's normalized "" default.
            if channel is None:
                result["native_channel_id"] = None
                result["channel"] = None
            # Fetch the full event for richer output.
            event = await storage.get_metadata(ref.event_id)
            if event is not None:
                result["event"] = json.loads(msgspec.json.encode(event))
            print(to_json(result))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_pressure(
    *,
    storage_path: str,
    source: str | None,
    limit: int | None = None,
) -> None:
    """List durable pre-admission pressure aggregates (counters only).

    Read-only listing of ``inbound_pressure_observations`` rows — the only
    durable trace of arrivals the inbound admission gate rejected, timed
    out, or deferred before canonical admission.  Rows carry window,
    source, outcome, and counters; never payloads.
    """
    storage = await _open_readonly_storage(storage_path)
    try:
        rows = await storage.list_inbound_pressure_observations(
            source=source, limit=limit
        )
        print(
            to_json(
                {
                    "observations": rows,
                    "count": len(rows),
                    "source_filter": source,
                }
            )
        )
    finally:
        await storage.close()


async def _inspect_replay(
    run_id: str,
    *,
    storage_path: str,
) -> None:
    """Inspect a replay run from durable outbox and receipt evidence.

    Outputs deterministic JSON matching ``medre trace replay --json`` and
    remains available during the crash window before the run's first receipt.
    """
    storage = await _open_readonly_storage(storage_path)
    _exit_code: int | None = None
    try:
        result = await _timeline.assemble_replay_timeline(storage, run_id)
        if result is None:
            print(
                f"Error: no durable evidence found for replay run: {run_id}",
                file=sys.stderr,
            )
            _exit_code = EXIT_NOT_FOUND
        else:
            print(timeline_to_json(result["timeline_entries"]))
    finally:
        await storage.close()
    if _exit_code is not None:
        sys.exit(_exit_code)


async def _inspect_radio_attempt(
    outbox_id: str,
    attempt: int,
    *,
    storage_path: str,
    assess_continuation: bool = False,
) -> None:
    """Print exact retained radio facts without recovery, SDKs or submission authority.

    The parent row is current operational authority; progress belongs to the
    explicitly selected historical generation. Reading a submitting checkpoint
    does not turn it into unknown or authorize a late native observation.
    """
    from medre.core.storage.backend import StorageError

    from .exit_codes import EXIT_BUILD

    storage = await _open_readonly_storage(storage_path)
    try:
        # Use the same retained checkpoint for presentation and assessment.
        # Two separate reads could otherwise describe different native states
        # while another process is committing a packet handoff.
        checkpoint = await storage.inspect_radio_attempt(outbox_id, attempt)
        snapshot = checkpoint.to_data() if checkpoint is not None else None
        if snapshot is None:
            print("Error: radio attempt not found", file=sys.stderr)
            raise SystemExit(EXIT_NOT_FOUND)
        parent = await storage.get_outbox_item(outbox_id, metadata_only=True)
        if parent is None:
            raise StorageError("Retained radio attempt has no outbox parent")
        report: dict[str, Any] = {
            "schema_version": 1,
            "outbox": {
                "outbox_id": parent.outbox_id,
                "event_id": parent.event_id,
                "delivery_plan_id": parent.delivery_plan_id,
                "target_adapter": parent.target_adapter,
                "target_channel": parent.target_channel,
                "current_status": parent.status,
                "current_attempt_number": parent.active_attempt
                or parent.attempt_number,
            },
            "progress": snapshot,
        }
        if assess_continuation:
            from medre.core.recovery.radio_assessment import assess_radio_continuation

            assert (
                checkpoint is not None
            )  # Same checkpoint produced the displayed snapshot.
            report["continuation_assessment"] = assess_radio_continuation(checkpoint)
        print(to_json(report))
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from None
    finally:
        await storage.close()


_CONTENT_REFUSAL = "Content inventory unavailable: storage is incomplete or unreadable; no files changed."


async def _inspect_content(*, storage_path: str, event_id: str | None = None) -> None:
    """Print aggregate core content accounting through a validated read-only store."""
    storage = await _open_readonly_storage(
        storage_path, refusal=(_CONTENT_REFUSAL, EXIT_CONFIG)
    )
    try:
        if event_id is None:
            report = await storage.inspect_content_inventory()
        else:
            report = await storage.inspect_event_content_dependencies(event_id)
        print(to_json(report))
    except (StorageError, ValueError):
        print(_CONTENT_REFUSAL, file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from None
    finally:
        await storage.close()
