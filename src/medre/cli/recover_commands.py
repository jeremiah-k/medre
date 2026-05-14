"""Recover CLI command: analyze failed deliveries and generate recovery runbooks."""
from __future__ import annotations

import json as _json
import sys
import uuid
from typing import Any

from medre.core.storage.replay import ReplayMode, ReplayRequest
from medre.runtime.trace import assemble_event_timeline

from .exit_codes import EXIT_NOT_FOUND, EXIT_CONFIG, EXIT_BUILD
from .storage_helpers import _open_readonly_storage
from .replay_commands import _RADIO_TRANSPORTS


# Failure-kind categories for recovery classification.
_RETRYABLE_KINDS = frozenset({"adapter_transient"})
_PERMANENT_KINDS = frozenset({
    "adapter_permanent", "adapter_missing", "renderer_failure", "planner_failure",
})
_OPERATIONAL_KINDS = frozenset({
    "capacity_rejection", "shutdown_rejection", "deadline_exceeded",
})


def _infer_failure_kind(error: str | None, status: str) -> str:
    """Infer a failure-kind string from receipt error and status fields.

    The ``DeliveryReceipt`` struct does not persist ``failure_kind`` directly;
    this helper reconstructs a best-effort classification from the error
    message patterns produced by the delivery pipeline.
    """
    err = (error or "").lower()
    # Operational: capacity / shutdown / deadline
    if "delivery_capacity_exceeded" in err or "capacity" in err:
        return "capacity_rejection"
    if "delivery_rejected_shutdown" in err or "shutdown" in err:
        return "shutdown_rejection"
    if "deadline_exceeded" in err or "deadline" in err:
        return "deadline_exceeded"
    # Permanent: renderer / adapter-missing
    if "renderer" in err or "no renderer" in err:
        return "renderer_failure"
    if "adapter_missing" in err or "not registered" in err:
        return "adapter_missing"
    if "planner" in err:
        return "planner_failure"
    # Retryable: transient signals
    if any(s in err for s in ("timeout", "connectionerror", "connection reset", "temporary")):
        return "adapter_transient"
    # dead_lettered implies retries exhausted — was transient
    if status == "dead_lettered":
        return "adapter_transient"
    # Default: permanent for unclassifiable failures
    if error:
        return "adapter_permanent"
    return "unknown"


def _failure_category(failure_kind: str) -> str:
    """Map a failure-kind string to a recovery category."""
    if failure_kind in _RETRYABLE_KINDS:
        return "retryable"
    if failure_kind in _PERMANENT_KINDS:
        return "permanent"
    if failure_kind in _OPERATIONAL_KINDS:
        return "operational"
    return "unknown"


def _recommended_commands(category: str, event_id: str) -> list[str]:
    """Return recommended next commands for a failure category."""
    if category == "retryable":
        return [
            f"medre trace event {event_id}",
            f"medre replay --mode DRY_RUN --event {event_id}",
            f"medre replay --mode BEST_EFFORT --event {event_id}",
        ]
    if category == "permanent":
        return [
            f"medre trace event {event_id}",
            f"medre inspect receipts --event {event_id}",
        ]
    if category == "operational":
        return [
            "medre diagnostics",
            "medre config check",
            f"medre trace event {event_id}",
        ]
    return [f"medre trace event {event_id}"]


async def _recover(
    config_path: str | None,
    event_id: str | None,
    failed_only: bool,
    since: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Analyze failed deliveries and generate a recovery runbook."""
    storage = await _open_readonly_storage(config_path)
    try:
        # Determine scope: single event or broad scan.
        failed_targets: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        if event_id is not None:
            # Single-event recovery.
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

            # Identify failed targets and classify by failure_kind.
            classification: dict[str, list[dict[str, Any]]] = {
                "retryable": [],
                "permanent": [],
                "operational": [],
                "unknown": [],
            }
            for r in receipts:
                if r.status not in ("failed", "dead_lettered"):
                    continue
                error_msg = getattr(r, "error", None)
                inferred = _infer_failure_kind(error_msg, r.status)
                cat = _failure_category(inferred)
                entry = {
                    "target_adapter": r.target_adapter,
                    "status": r.status,
                    "attempt_number": r.attempt_number,
                    "receipt_id": r.receipt_id,
                    "failure_kind": inferred,
                    "category": cat,
                }
                if error_msg:
                    entry["error"] = error_msg
                # Include replay context if present.
                r_source = getattr(r, "source", "live")
                r_run_id = getattr(r, "replay_run_id", None)
                if r_source == "replay" and r_run_id:
                    entry["source"] = "replay"
                    entry["replay_run_id"] = r_run_id
                failed_targets.append(entry)
                classification[cat].append(entry)

            # Collect replay receipts for context.
            replay_context: list[dict[str, str]] = []
            seen_run_ids: set[str] = set()
            for r in receipts:
                r_source = getattr(r, "source", "live")
                r_run_id = getattr(r, "replay_run_id", None)
                if r_source == "replay" and r_run_id and r_run_id not in seen_run_ids:
                    seen_run_ids.add(r_run_id)
                    replay_context.append({
                        "replay_run_id": r_run_id,
                        "source": "replay",
                    })

            # Build timeline for runbook.
            timeline = assemble_event_timeline(
                event, receipts, native_refs, relations,
            )

            # Aggregate recommended commands across present categories.
            present_categories = {
                cat for cat, items in classification.items() if items
            }
            all_commands: list[str] = []
            for cat in sorted(present_categories):
                all_commands.extend(_recommended_commands(cat, event_id))

            # Deduplicate while preserving order.
            seen_cmds: set[str] = set()
            unique_commands: list[str] = []
            for cmd in all_commands:
                if cmd not in seen_cmds:
                    seen_cmds.add(cmd)
                    unique_commands.append(cmd)

            runbook: dict[str, Any] = {
                "scope": "event",
                "event_id": event_id,
                "event_kind": event.event_kind,
                "source_adapter": event.source_adapter,
                "total_receipts": len(receipts),
                "failed_targets": failed_targets,
                "failure_classification": {
                    cat: items for cat, items in classification.items() if items
                },
                "recommended_commands": unique_commands,
                "timeline": timeline,
                "warnings": [],
            }

            if replay_context:
                runbook["replay_context"] = replay_context

            # Add duplicate-send warning when BEST_EFFORT is recommended.
            if "retryable" in present_categories:
                runbook["warnings"].append(
                    "BEST_EFFORT replay recommended for retryable failures — "
                    "this may produce duplicate sends.  Use DRY_RUN first "
                    "to preview."
                )

            # Add duplicate-send risk warnings for radio transports.
            for nref in native_refs:
                if nref.adapter.lower() in _RADIO_TRANSPORTS or any(
                    nref.adapter.lower().startswith(t)
                    for t in _RADIO_TRANSPORTS
                ):
                    runbook["warnings"].append(
                        f"Adapter {nref.adapter} uses a radio transport — "
                        f"recovery may produce duplicate sends. "
                        f"Use --dry-run first to preview."
                    )
                    break

            if runbook["warnings"] and not any(
                "Radio transports" in w for w in runbook["warnings"]
            ):
                runbook["warnings"].append(
                    "Radio transports (Meshtastic, MeshCore, LXMF) use "
                    "fire-and-forget delivery.  Recovery is best-effort "
                    "and duplicates are possible."
                )

        else:
            # Broad scan: list events with failed receipts.
            runbook = {
                "scope": "scan",
                "failed_only": failed_only,
                "since": since,
                "warnings": [
                    "Specify --event <event_id> for a detailed recovery runbook.",
                    "Radio transports (Meshtastic, MeshCore, LXMF) use "
                    "fire-and-forget delivery.  Recovery is best-effort "
                    "and duplicates are possible.",
                ],
                "note": "Use --event <event_id> --dry-run to preview recovery.",
            }

        # If --dry-run, include a replay preview section.
        if dry_run and event_id is not None:
            runbook["dry_run"] = {
                "mode": "dry_run",
                "event_id": event_id,
                "status": "preview",
                "message": (
                    "DRY_RUN replay would re-process this event through "
                    "all pipeline stages except delivery.  Use "
                    "'medre replay --mode dry_run --event <event_id>' "
                    "to execute."
                ),
            }

        if json_output:
            print(_json.dumps(runbook, sort_keys=True, indent=2, default=str))
        else:
            # Human-readable runbook.
            if event_id is not None:
                print(f"Recovery runbook: {event_id}")
                print(f"  Kind:    {runbook['event_kind']}")
                print(f"  Source:  {runbook['source_adapter']}")
                print(f"  Receipts: {runbook['total_receipts']}")
                if failed_targets:
                    print(f"  Failed targets ({len(failed_targets)}):")
                    for ft in failed_targets:
                        fk = ft.get("failure_kind", "unknown")
                        print(
                            f"    {ft['target_adapter']}: {ft['status']} "
                            f"({fk}, attempt {ft['attempt_number']})"
                        )
                    # Show classification summary.
                    fc = runbook.get("failure_classification", {})
                    if fc:
                        print()
                        print("  Failure classification:")
                        for cat in ("retryable", "permanent", "operational", "unknown"):
                            items = fc.get(cat, [])
                            if items:
                                adapters = ", ".join(
                                    i["target_adapter"] for i in items
                                )
                                print(f"    {cat}: {adapters}")
                else:
                    print("  Failed targets: none")
                if runbook.get("recommended_commands"):
                    print()
                    print("  Recommended next commands:")
                    for cmd in runbook["recommended_commands"]:
                        print(f"    {cmd}")
                if runbook.get("replay_context"):
                    print()
                    print("  Prior replay runs:")
                    for rc in runbook["replay_context"]:
                        print(f"    run_id={rc['replay_run_id']}")
                print(f"  Timeline entries: {len(timeline)}")
            else:
                print("Recovery scan")
                print(f"  Failed-only: {failed_only}")
                print(f"  Since: {since or '(all)'}")

            if runbook.get("warnings"):
                print()
                for w in runbook["warnings"]:
                    print(f"  \u26a0 {w}")

            if dry_run and event_id is not None:
                print()
                print("  DRY RUN: No side effects. Preview only.")
    finally:
        await storage.close()
