"""Recover CLI command: analyze failed deliveries and generate recovery runbooks."""

from __future__ import annotations

import json as _json
import sys
from typing import Any

import medre.runtime.timeline as _timeline
from medre.core.observability.classification import (
    failure_category as _failure_category,
)
from medre.core.observability.classification import (
    infer_failure_kind as _infer_failure_kind,
)
from medre.core.observability.classification import (
    recommended_commands as _recommended_commands,
)
from medre.runtime.reporting import _derive_capability_evidence

from .exit_codes import EXIT_NOT_FOUND
from .storage_helpers import _open_readonly_storage
from .transport_constants import RADIO_TRANSPORTS


async def _build_event_recovery_runbook(
    storage: Any,
    event_id: str,
    *,
    storage_path: str,
) -> dict[str, Any] | None:
    """Build a recovery runbook dict for a single event.

    Returns ``None`` when the event does not exist in storage.
    This is the pure-logic core shared by ``medre recover`` and
    ``medre inspect event --recovery`` — no CLI I/O, no sys.exit.
    """
    tl_result = await _timeline.assemble_event_timeline(storage, event_id)
    if tl_result is None:
        return None

    event = tl_result["event"]
    receipts = tl_result["receipts"]
    native_refs = tl_result["native_refs"]

    # Identify failed targets and classify by failure_kind.
    classification: dict[str, list[dict[str, Any]]] = {
        "retryable": [],
        "permanent": [],
        "operational": [],
        "unknown": [],
    }
    failed_targets: list[dict[str, Any]] = []
    for r in receipts:
        if r.status not in ("failed", "dead_lettered"):
            continue
        error_msg = getattr(r, "error", None)
        inferred = _infer_failure_kind(error_msg, r.status)
        cat = _failure_category(inferred)
        entry: dict[str, Any] = {
            "target_adapter": r.target_adapter,
            "status": r.status,
            "attempt_number": r.attempt_number,
            "receipt_id": r.receipt_id,
            "failure_kind": inferred,
            "category": cat,
            "delivery_plan_id": getattr(r, "delivery_plan_id", None),
        }
        if getattr(r, "target_channel", None):
            entry["target_channel"] = r.target_channel
        if getattr(r, "route_id", None):
            entry["route_id"] = r.route_id
        if error_msg:
            entry["error"] = error_msg
        # Derive suppression reason for operator visibility.
        cap = _derive_capability_evidence(
            error_msg,
            getattr(r, "rendering_evidence", None),
            inferred,
            r.status,
        )
        if cap.get("suppression_reason"):
            entry["suppression_reason"] = cap["suppression_reason"]
        else:
            # Check receipt's own failure_kind for capability/policy suppression
            # when the inferred kind doesn't capture it.
            rk = getattr(r, "failure_kind", None)
            if (
                rk in ("capability_suppressed", "policy_suppressed", "loop_suppressed")
                and error_msg
            ):
                import re as _re

                cap_match = _re.match(
                    r"^(?:capability_suppressed|policy_suppressed|loop_suppressed):\s*(.+)$",
                    error_msg,
                )
                if cap_match:
                    entry["suppression_reason"] = cap_match.group(1).strip()
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
            replay_context.append(
                {
                    "replay_run_id": r_run_id,
                    "source": "replay",
                }
            )

    # Build timeline for runbook.
    timeline_entries = tl_result["timeline_entries"]

    # Aggregate recommended commands across present categories.
    present_categories = {cat for cat, items in classification.items() if items}
    all_commands: list[str] = []
    for cat in sorted(present_categories):
        all_commands.extend(
            _recommended_commands(cat, event_id, storage_path=storage_path)
        )

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
        "commands": {
            "primary": unique_commands,
            "specialized": [
                f"medre recover --event {event_id} --storage-path {storage_path}",
            ],
        },
        "timeline": timeline_entries,
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
        if nref.adapter.lower() in RADIO_TRANSPORTS or any(
            nref.adapter.lower().startswith(t) for t in RADIO_TRANSPORTS
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

    return runbook


async def _recover(
    event_id: str | None,
    failed_only: bool,
    since: str | None,
    dry_run: bool,
    json_output: bool,
    *,
    storage_path: str,
) -> None:
    """Analyze failed deliveries and generate a recovery runbook."""
    storage = await _open_readonly_storage(storage_path)
    try:
        # Determine scope: single event or broad scan.
        runbook: dict[str, Any]
        timeline: list[dict[str, Any]] = []
        failed_targets: list[dict[str, Any]] = []
        if event_id is not None:
            # Single-event recovery.
            result = await _build_event_recovery_runbook(
                storage, event_id, storage_path=storage_path
            )
            if result is None:
                print(
                    f"Error: event not found: {event_id}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_NOT_FOUND)

            runbook = result
            timeline = runbook["timeline"]
            failed_targets = runbook["failed_targets"]

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
            timeline = []
            failed_targets = []

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
                        target_line = ft["target_adapter"]
                        ch = ft.get("target_channel")
                        if ch:
                            target_line += f"/{ch}"
                        route = ft.get("route_id")
                        if route:
                            target_line += f" route={route}"
                        print(
                            f"    {target_line}: {ft['status']} "
                            f"({fk}, attempt {ft['attempt_number']})"
                        )
                        if ft.get("suppression_reason"):
                            print(f"      suppressed: {ft['suppression_reason']}")
                    # Show classification summary.
                    fc = runbook.get("failure_classification", {})
                    if fc:
                        print()
                        print("  Failure classification:")
                        for cat in ("retryable", "permanent", "operational", "unknown"):
                            items = fc.get(cat, [])
                            if items:
                                labels = []
                                for i in items:
                                    label = i["target_adapter"]
                                    ch = i.get("target_channel")
                                    if ch:
                                        label += f"/{ch}"
                                    labels.append(label)
                                print(f"    {cat}: {', '.join(labels)}")
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
