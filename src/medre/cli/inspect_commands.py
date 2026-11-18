"""Inspect CLI commands: read-only storage queries for events, receipts, native refs.

Supports augmented inspection via ``--timeline``, ``--evidence``, and ``--recovery``
flags on ``inspect event``, an ``inspect replay`` subcommand, and a
``work-status`` per-adapter delivery summary — all read-only, no runtime
start, no storage mutation.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any

import msgspec

import medre.runtime.timeline as _timeline
from medre.core.observability.work_status import (
    AdapterWorkStatus,
    WorkStatusOldest,
    WorkStatusReport,
    assemble_work_status_report,
)
from medre.core.storage.backend import StorageError
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.reporting import native_ref_to_report_dict
from medre.runtime.trace import timeline_to_json

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_FAILED, EXIT_NOT_FOUND
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


#: The closed pressure outcome vocabulary: three admission-gate outcomes
#: recorded by the runtime gate plus three producer-side outcomes reported
#: by adapters before any admission attempt.
_PRESSURE_OUTCOME_ORDER: tuple[str, ...] = (
    "rejected",
    "timed_out",
    "deferred",
    "scheduled",
    "overflow",
    "cancelled_before_admission",
)


def _pressure_outcome_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the listed rows' counts per outcome in vocabulary order.

    Derived, content-free rollup over the returned window only — rows stay
    the durable evidence; the totals make the admission-gate and
    producer-side halves of the vocabulary distinct at a glance.
    """
    totals = {outcome: 0 for outcome in _PRESSURE_OUTCOME_ORDER}
    for row in rows:
        outcome = str(row.get("outcome", ""))
        totals[outcome] = totals.get(outcome, 0) + int(row.get("count", 0))
    return {outcome: total for outcome, total in totals.items() if total}


async def _inspect_pressure(
    *,
    storage_path: str,
    source: str | None,
    limit: int | None = None,
) -> None:
    """List durable pre-admission pressure aggregates (counters only).

    Read-only listing of pressure observations and loss-accounting gaps.
    Observation rows carry window, source, outcome and counters; gap rows
    show where bounded refusal retention lost facts during an outage.
    Gaps are global, not source-specific, even when source filters the
    observation rows. Neither family contains message payloads.
    """
    storage = await _open_readonly_storage(storage_path)
    try:
        rows = await storage.list_inbound_pressure_observations(
            source=source, limit=limit
        )
        outage_gaps = await storage.list_inbound_pressure_outage_gaps(limit=limit)
        print(
            to_json(
                {
                    "observations": rows,
                    "count": len(rows),
                    "source_filter": source,
                    "outcome_totals": _pressure_outcome_totals(rows),
                    "outage_gaps": outage_gaps,
                    "outage_gap_count": len(outage_gaps),
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


def _work_status_next_inspection(event_id: str, *, storage_path: str) -> list[str]:
    """Read-only event-level inspection arguments, independent of shell."""
    return [
        "medre",
        "inspect",
        "event",
        "--recovery",
        f"--storage-path={storage_path}",
        "--",
        event_id,
    ]


def _describe_age(oldest: WorkStatusOldest) -> str:
    """Human age sentence carrying the anchor and its precise origin."""
    anchor = oldest.anchor or "unknown time"
    if oldest.origin == "retry_due":
        if oldest.category == "retry_scheduled":
            due = (
                "unknown interval"
                if oldest.age_seconds is None
                else f"due in at most {-oldest.age_seconds}s"
            )
            return f"retry scheduled at {anchor} ({due})"
        overdue = (
            " (due time unknown)"
            if oldest.age_seconds is None
            else f" ({oldest.age_seconds}s overdue)"
        )
        return f"retry due at {anchor}{overdue}"
    if oldest.origin == "claim":
        held = "" if oldest.age_seconds is None else f" — held {oldest.age_seconds}s"
        return f"claimed at {anchor}{held}"
    if oldest.origin == "queue_admission":
        waiting = (
            "" if oldest.age_seconds is None else f" — queued {oldest.age_seconds}s"
        )
        return f"admitted to adapter queue at {anchor}{waiting}"
    waiting = "" if oldest.age_seconds is None else f" — waiting {oldest.age_seconds}s"
    return f"admitted at {anchor}{waiting}"


def _print_adapter_work_status(
    adapter: AdapterWorkStatus, *, storage_path: str
) -> None:
    """Print one adapter's summary, uncertainty, age, and next inspection."""
    categories = adapter.categories
    header = (
        f"    {adapter.target_adapter}: outstanding {adapter.outstanding} of "
        f"{adapter.current_generations} current "
        f"(unstarted {categories.get('unstarted_eligible', 0)}, "
        f"scheduled {categories.get('retry_scheduled', 0)}, "
        f"reserved/active {categories.get('reserved_active', 0)}, "
        f"native queue {categories.get('native_queue', 0)}; "
        f"uncertain outcome {categories.get('uncertain_outcome', 0)}, "
        f"terminal {categories.get('terminal_outcome', 0)}"
    )
    if categories.get("unrecognized"):
        header += f", unrecognized {categories['unrecognized']}"
    print(header + ")")
    if categories.get("uncertain_outcome"):
        print("      abandoned outcomes remain uncertain; see the operator runbook")
    oldest = adapter.oldest_outstanding
    if oldest is None:
        print("      no outstanding work")
        return
    print(f"      oldest outstanding: {_describe_age(oldest)}")
    print(
        f"      delivery: event={oldest.event_id!r} plan={oldest.delivery_plan_id!r} "
        f"channel={oldest.target_channel!r} outbox={oldest.outbox_id!r}"
    )
    uncertainty = adapter.uncertainty
    flags: list[str] = []
    if uncertainty.reserved_without_generation_receipt:
        flags.append(
            f"{uncertainty.reserved_without_generation_receipt} reserved "
            "generation(s) carry no attempt evidence: whether a native send "
            "occurred is unknown"
        )
    if uncertainty.lease_missing_or_expired:
        flags.append(
            f"{uncertainty.lease_missing_or_expired} reserved generation(s) "
            "have a missing, unusable or expired lease: recorded-owner liveness is "
            "unknown"
        )
    if uncertainty.stale_queue_reclaimable:
        flags.append(
            f"{uncertainty.stale_queue_reclaimable} queued generation(s) are "
            "past the reclaim grace: adapter-local acceptance may be stale"
        )
    for flag in flags:
        print(f"      uncertainty: {flag}")
    args = _work_status_next_inspection(oldest.event_id, storage_path=storage_path)
    if os.name == "nt":
        print(
            "      next inspection (event-level; argument list, not shell code): "
            + json.dumps(args)
        )
    else:
        print("      next inspection (event-level, POSIX shell): " + shlex.join(args))


def _print_work_status(report: WorkStatusReport, *, storage_path: str) -> None:
    """Print the concise human work-status summary."""
    totals = report.totals
    scope = (
        f"adapter {report.adapter_filter!r} only"
        if report.adapter_filter is not None
        else "all adapters"
    )
    print("Work status — current delivery generations per destination (read-only)")
    print(f"  Storage: {storage_path} (read-only)")
    print(f"  Sampled at: {report.sampled_at}")
    print(f"  Scope: {scope}; metadata only")
    print(
        f"  Current generations: {totals.get('current_generations', 0)} — "
        f"outstanding {totals.get('outstanding', 0)} "
        f"(unstarted {totals.get('unstarted_eligible', 0)}, "
        f"scheduled {totals.get('retry_scheduled', 0)}, "
        f"reserved/active {totals.get('reserved_active', 0)}, "
        f"native queue {totals.get('native_queue', 0)}), "
        f"uncertain outcome {totals.get('uncertain_outcome', 0)}, "
        f"terminal {totals.get('terminal_outcome', 0)}"
    )
    if totals.get("unrecognized"):
        print(f"  Unrecognized states: {totals['unrecognized']} (fail-closed bucket)")
    for key in ("connectivity", "owner_liveness", "recipient_delivery"):
        print(f"  {key.replace('_', ' ').capitalize()}: {report.limits[key]}")
    if not report.adapters:
        print("  Adapters: none within scope")
    else:
        print(f"  Adapters ({len(report.adapters)}):")
        for adapter in report.adapters:
            _print_adapter_work_status(adapter, storage_path=storage_path)
    uncertainty = report.uncertainty
    print(
        "  Uncertainty totals: "
        f"{uncertainty.reserved_without_generation_receipt} reserved without "
        f"attempt evidence, {uncertainty.lease_missing_or_expired} missing or "
        f"expired leases, {uncertainty.stale_queue_reclaimable} stale queue "
        "admissions"
    )
    raw = ", ".join(
        f"{status} {count}"
        for status, count in sorted(report.raw_outbox_states.items())
    )
    print(f"  Raw outbox states (all retained rows): {raw or 'none'}")
    history = report.retained_history
    receipts = history.get("receipts_by_status", {})
    receipt_summary = ", ".join(
        f"{status} {count}" for status, count in sorted(receipts.items())
    )
    print(
        f"  Retained history: {history.get('outbox_rows_total', 0)} outbox rows, "
        f"{history.get('superseded_generations', 0)} superseded generations, "
        f"{history.get('receipts_total', 0)} receipts"
        + (f" ({receipt_summary})" if receipts else "")
    )
    print(
        "  This view reads local storage only; it starts no adapter and sends nothing."
    )


async def _inspect_work_status(
    *,
    storage_path: str,
    adapter: str | None = None,
    json_output: bool = False,
) -> None:
    """Print the per-adapter work-status projection for one storage snapshot.

    Read-only: opens storage read-only, aggregates the current delivery
    generations over one consistent read snapshot, and prints metadata
    only. Starts no adapter, opens no native profile, and submits,
    replays, retires, or migrates nothing. Unreadable storage and
    inconsistent accounting are reported honestly instead of being
    synthesized into a healthy-looking summary.
    """
    storage = await _open_readonly_storage(storage_path)
    try:
        summary = await storage.query_work_status_summary(adapter=adapter)
        report = assemble_work_status_report(
            sampled_at=summary["sampled_at"],
            adapter_filter=adapter,
            adapter_rows=summary["adapter_rows"],
            oldest_rows=summary["oldest_rows"],
            raw_status_rows=summary["raw_status_rows"],
            current_generations_total=summary["current_generations_total"],
            receipts_by_status=summary["receipts_by_status"],
        )
    except StorageError as exc:
        print(f"Work status unavailable: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from None
    except ValueError as exc:
        print(f"Work status refused: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from None
    finally:
        await storage.close()

    if json_output:
        print(to_json(report.to_dict()))
    else:
        _print_work_status(report, storage_path=storage_path)
