"""Recover CLI command: analyze unresolved deliveries and generate recovery runbooks.

Read-only diagnostic / operator recovery surface.  Opens storage in
read-only mode, inspects events/receipts/timeline, classifies failures,
and prints structured runbooks.  Does not mutate storage, create
receipts, or transition outbox status.

Current-outcome rule (shared with the storage query surface and the
``delivery_status`` authority): a delivery is *currently unresolved* when
its lifecycle-authoritative receipt has status ``failed`` or
``dead_lettered``.  For outbox-backed delivery the outbox ``receipt_id`` is
the current-state pointer; append order remains historical evidence only.
Outbox-less lineages continue to use durable append order.  Superseded or
stale-worker failures stay visible as *historical* evidence for audit
drilldown.

The only write-like behaviour is printing to stdout/stderr.  All storage
access uses ``_open_readonly_storage``.

Recovery planning/preview authority lives with ``medre replay`` (``--mode
dry_run``); this command describes evidence only and accepts no replay
options.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import asdict
from typing import Any

import medre.runtime.timeline as _timeline
from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    effective_generation,
)
from medre.core.observability.classification import (
    failure_category as _failure_category,
)
from medre.core.observability.classification import (
    infer_failure_kind as _infer_failure_kind,
)
from medre.core.observability.classification import (
    recommended_commands as _recommended_commands,
)
from medre.core.observability.sanitization import sanitize_error
from medre.core.storage.backend import (
    DEFAULT_RECOVERY_PAGE_LIMIT,
    UnresolvedDelivery,
    attempt_source_label,
    decode_page_cursor,
)
from medre.runtime.reporting import _derive_capability_evidence

from .exit_codes import EXIT_CONFIG, EXIT_NOT_FOUND
from .json import to_json
from .storage_helpers import _open_readonly_storage
from .transport_constants import RADIO_TRANSPORTS

#: Machine- and human-shared definition of what the scan/runbook reports.
_OUTCOME_DEFINITION = (
    "A delivery is unresolved when its lifecycle-authoritative receipt for "
    "the logical delivery (event, delivery plan, target adapter, target "
    "channel) has status failed or dead_lettered. Outbox-backed delivery "
    "uses the outbox receipt_id pointer; outbox-less delivery uses durable "
    "append order. Later stale-worker receipts remain historical evidence, "
    "and dry-run replays (which record no receipts) change nothing."
)

_REPLAY_CONFIG_NOTE = (
    "medre replay has no --storage-path argument; it needs configuration "
    "context: append --config <config.yaml> (auto-discovered from XDG "
    "defaults when omitted)."
)


def _disposition(item: UnresolvedDelivery) -> str:
    """Derive an evidence-backed retry/terminal disposition for a row.

    Only facts from the current receipt and (as enrichment, never as
    acceptance evidence) the outbox row are used.
    """
    if item.status == "dead_lettered":
        return "dead_lettered"
    if item.outbox_status == "cancelled":
        return "cancelled"
    if item.outbox_status == "abandoned":
        return "abandoned"
    if item.next_retry_at or (
        item.outbox_status == "retry_wait" and item.outbox_next_attempt_at
    ):
        return "retry_scheduled"
    return "needs_operator_action"


async def _build_event_recovery_runbook(
    storage: Any,
    event_id: str,
    *,
    storage_path: str,
) -> dict[str, Any] | None:
    """Build a recovery runbook dict for a single event.

    Returns ``None`` when the event does not exist in storage.
    This is the pure-logic core shared by ``medre recover --event`` and
    ``medre inspect event --recovery`` — no CLI I/O, no sys.exit.

    ``failed_targets`` lists **current** unresolved failures only; earlier
    failures of the same lineage that a later receipt superseded are kept
    in ``historical_failures`` so immutable history stays drillable.
    """
    tl_result = await _timeline.assemble_event_timeline(storage, event_id)
    if tl_result is None:
        return None

    event = tl_result["event"]
    receipts = tl_result["receipts"]
    native_refs = tl_result["native_refs"]

    # Build the shared event-scoped lifecycle-authority index. Plan IDs may
    # recur across events, so current delivery identity always includes the
    # canonical event even in this event-scoped runbook.
    # Reuse the timeline's outbox snapshot so recovery projections cannot mix
    # two storage states if delivery progresses during runbook construction.
    outbox_items: list[Any] = list(tl_result.get("outbox_items") or [])
    authority = DeliveryAuthorityResolver(receipts, outbox_items)

    # Identify currently-failed lineages and classify by failure_kind.
    classification: dict[str, list[dict[str, Any]]] = {
        "retryable": [],
        "permanent": [],
        "operational": [],
        "unknown": [],
    }
    failed_targets: list[dict[str, Any]] = []
    historical_failures: list[dict[str, Any]] = []

    for snapshot in authority.ordered_snapshots():
        lineage_receipts = list(snapshot.receipts)
        if not lineage_receipts:
            continue

        # Recovery describes the current mutable generation when one exists.
        # Global immutable receipt authority can legitimately belong to an
        # older generation while a fresh replay/retry generation is pending.
        # Only outbox-less delivery falls back to global receipt authority.
        current = (
            snapshot.current_receipt
            if snapshot.current_outbox is not None
            else snapshot.authoritative_receipt
        )
        is_current_failure = current is not None and current.status in (
            "failed",
            "dead_lettered",
        )

        # Historical failures are immutable receipts that are no longer the
        # lifecycle-authoritative receipt.  This includes a stale worker's
        # receipt appended *after* the current outcome when its guarded outbox
        # transition was rejected.
        current_sequence = current.sequence if current is not None else None
        for r in lineage_receipts:
            if r.status not in ("failed", "dead_lettered"):
                continue
            if current is not None and r.receipt_id == current.receipt_id:
                continue
            if (
                is_current_failure
                and current_sequence is not None
                and r.sequence <= current_sequence
            ):
                # Earlier failed attempts are part of the active failure's
                # lineage, not superseded history.  A later failure can only
                # be historical here (for example, a stale owner whose
                # guarded outbox transition was rejected).
                continue
            hist: dict[str, Any] = {
                "target_adapter": r.target_adapter,
                "status": r.status,
                "attempt_number": r.attempt_number,
                "receipt_id": r.receipt_id,
                "delivery_plan_id": getattr(r, "delivery_plan_id", None),
                "attempt_source": attempt_source_label(
                    getattr(r, "source", "live"),
                    getattr(r, "replay_run_id", None),
                ),
                "superseded_by": (
                    {
                        "receipt_id": current.receipt_id,
                        "status": current.status,
                    }
                    if current is not None
                    else (
                        {
                            "outbox_id": snapshot.current_outbox.outbox_id,
                            "status": snapshot.current_outbox.status,
                            "attempt_number": effective_generation(
                                snapshot.current_outbox
                            ),
                        }
                        if snapshot.current_outbox is not None
                        else None
                    )
                ),
            }
            if getattr(r, "target_channel", None):
                hist["target_channel"] = r.target_channel
            error_msg_hist = getattr(r, "error", None)
            if error_msg_hist:
                hist["error"] = sanitize_error(error_msg_hist)
            historical_failures.append(hist)

        if not is_current_failure or current is None:
            continue

        r = current
        error_msg = getattr(r, "error", None)
        inferred = getattr(r, "failure_kind", None) or _infer_failure_kind(
            error_msg, r.status
        )
        cat = _failure_category(inferred)
        entry: dict[str, Any] = {
            "target_adapter": r.target_adapter,
            "status": r.status,
            "attempt_number": r.attempt_number,
            "receipt_id": r.receipt_id,
            "failure_kind": inferred,
            "category": cat,
            "delivery_plan_id": getattr(r, "delivery_plan_id", None),
            "attempt_source": attempt_source_label(
                getattr(r, "source", "live"),
                getattr(r, "replay_run_id", None),
            ),
        }
        if getattr(r, "target_channel", None):
            entry["target_channel"] = r.target_channel
        if getattr(r, "route_id", None):
            entry["route_id"] = r.route_id
        if error_msg:
            entry["error"] = sanitize_error(error_msg)
        if getattr(r, "next_retry_at", None) is not None:
            entry["next_retry_at"] = r.next_retry_at
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
        if r_run_id:
            entry["source"] = r_source
            entry["replay_run_id"] = r_run_id
        failed_targets.append(entry)
        classification[cat].append(entry)

    # Collect named replay origins from both durable surfaces.  Outbox claims
    # are admission authority and may exist before the first receipt (for
    # example after a crash immediately following replay admission).
    # ``source`` remains the per-attempt dispatch mechanism, so a named replay
    # can legitimately contain both replay and retry receipts.
    replay_context_by_run: dict[str, dict[str, Any]] = {}
    for item in outbox_items:
        run_id = getattr(item, "replay_run_id", None)
        if not run_id:
            continue
        context = replay_context_by_run.setdefault(
            run_id,
            {
                "replay_run_id": run_id,
                "dispatch_sources": set(),
                "outbox_count": 0,
                "outbox_statuses": set(),
            },
        )
        context["outbox_count"] += 1
        context["outbox_statuses"].add(str(getattr(item, "status", "pending")))

    for receipt in receipts:
        run_id = getattr(receipt, "replay_run_id", None)
        if not run_id:
            continue
        context = replay_context_by_run.setdefault(
            run_id,
            {
                "replay_run_id": run_id,
                "dispatch_sources": set(),
                "outbox_count": 0,
                "outbox_statuses": set(),
            },
        )
        context["dispatch_sources"].add(
            str(getattr(receipt, "source", "live") or "live")
        )

    replay_context: list[dict[str, Any]] = [
        {
            "replay_run_id": context["replay_run_id"],
            "dispatch_sources": sorted(context["dispatch_sources"]),
            "outbox_count": context["outbox_count"],
            "outbox_statuses": sorted(context["outbox_statuses"]),
        }
        for context in replay_context_by_run.values()
    ]

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
        "definition": _OUTCOME_DEFINITION,
        "total_receipts": len(receipts),
        "failed_targets": failed_targets,
        "historical_failures": historical_failures,
        "failure_classification": {
            cat: items for cat, items in classification.items() if items
        },
        "recommended_commands": unique_commands,
        "commands": {
            "primary": unique_commands,
            "specialized": [
                f"medre recover --event {event_id} --storage-path {shlex.quote(storage_path)}",
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
            "this may produce duplicate sends.  Preview with "
            "'medre replay --mode dry_run --event "
            f"{event_id} --config <config.yaml>' first."
        )

    # Add duplicate-send risk warnings for radio transports.
    for nref in native_refs:
        if nref.adapter.lower() in RADIO_TRANSPORTS or any(
            nref.adapter.lower().startswith(t) for t in RADIO_TRANSPORTS
        ):
            runbook["warnings"].append(
                f"Adapter {nref.adapter} uses a radio transport — "
                f"recovery may produce duplicate sends. "
                f"Preview with 'medre replay --mode dry_run' first."
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


def _target_label(entry: dict[str, Any]) -> str:
    """Human label for a failed-target entry: adapter[/channel] [route=..]."""
    label = entry["target_adapter"]
    if entry.get("target_channel"):
        label += f"/{entry['target_channel']}"
    if entry.get("route_id"):
        label += f" route={entry['route_id']}"
    return label


def _print_event_runbook(runbook: dict[str, Any]) -> None:
    """Print the single-event runbook in human-readable form."""
    event_id = runbook["event_id"]
    print(f"Recovery runbook: {event_id}")
    print(f"  Kind:    {runbook['event_kind']}")
    print(f"  Source:  {runbook['source_adapter']}")
    print(f"  Receipts: {runbook['total_receipts']}")
    failed_targets = runbook["failed_targets"]
    if failed_targets:
        print(f"  Failed targets ({len(failed_targets)}):")
        for ft in failed_targets:
            fk = ft.get("failure_kind", "unknown")
            line = _target_label(ft)
            if ft.get("attempt_source") and ft["attempt_source"] != "live":
                line += f" attempt_source={ft['attempt_source']}"
            print(
                f"    {line}: {ft['status']} " f"({fk}, attempt {ft['attempt_number']})"
            )
            if ft.get("next_retry_at"):
                print(f"      retry scheduled at: {ft['next_retry_at']}")
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
                    labels = [_target_label(i) for i in items]
                    print(f"    {cat}: {', '.join(labels)}")
    else:
        print("  Failed targets: none (no unresolved current failures)")
    historical = runbook.get("historical_failures", [])
    if historical:
        print(
            f"  Historical failures superseded by later delivery state "
            f"({len(historical)}):"
        )
        for hf in historical:
            label = hf["target_adapter"]
            if hf.get("target_channel"):
                label += f"/{hf['target_channel']}"
            sup = hf.get("superseded_by")
            if not sup:
                superseding = "unknown current state"
            elif sup.get("receipt_id"):
                superseding = f"receipt {sup['receipt_id']} ({sup['status']})"
            elif sup.get("outbox_id"):
                superseding = (
                    f"outbox generation {sup['outbox_id']} "
                    f"attempt {sup['attempt_number']} ({sup['status']})"
                )
            else:
                superseding = "unknown current state"
            print(
                f"    {label}: attempt {hf['attempt_number']} "
                f"{hf['status']} — superseded by {superseding}"
            )
    if runbook.get("recommended_commands"):
        print()
        print("  Recommended next commands:")
        for cmd in runbook["recommended_commands"]:
            print(f"    {cmd}")
        print(f"    ({_REPLAY_CONFIG_NOTE})")
    if runbook.get("replay_context"):
        print()
        print("  Prior replay runs:")
        for rc in runbook["replay_context"]:
            print(f"    run_id={rc['replay_run_id']}")
    print(f"  Timeline entries: {len(runbook['timeline'])}")


def _scan_record(item: UnresolvedDelivery) -> dict[str, Any]:
    """JSON-safe operator record for one unresolved delivery."""
    record = asdict(item)
    if item.error:
        record["error"] = sanitize_error(item.error)
    record["disposition"] = _disposition(item)
    return record


def _print_scan(
    page: Any,
    *,
    since: str | None,
    limit: int | None,
    storage_path: str,
) -> None:
    """Print the broad scan in human-readable form."""
    items = page.items
    print("Recovery scan — unresolved current delivery failures")
    print(f"  Storage: {storage_path} (read-only)")
    if since is not None:
        print("  Scope: canonical event timestamp >= " f"{since} (inclusive, UTC)")
    else:
        print("  Scope: all canonical event timestamps")
    print("  Ordering: receipt sequence ascending (oldest first)")
    print("  Pages are a live view, not a snapshot.")
    print()

    if not items:
        print("No unresolved current delivery failures within scope.")
        print(f"  ({_OUTCOME_DEFINITION})")
        return

    for item in items:
        channel = f"/{item.target_channel}" if item.target_channel else ""
        print(
            f"  {item.event_id}  {item.event_kind}  "
            f"from {item.source_adapter}  event@{item.event_timestamp}"
        )
        print(
            f"    delivery: {item.target_adapter}{channel} "
            f"route={item.route_id or '-'} plan={item.delivery_plan_id} "
            f"attempt_source={item.attempt_source}"
        )
        current = item.status.upper()
        if item.status == "failed":
            current += (
                f" attempt {item.attempt_number} " f"({item.failure_kind or 'unknown'})"
            )
        else:
            current += f" (terminal, attempt {item.attempt_number})"
        print(f"    current:  {current} — {_disposition(item)}")
        if item.next_retry_at:
            print(f"      next retry: {item.next_retry_at}")
        elif item.outbox_next_attempt_at:
            print(f"      next attempt: {item.outbox_next_attempt_at}")
        if item.error:
            print(f"      error: {sanitize_error(item.error)}")
        print(
            "      inspect:   medre inspect event "
            f"{item.event_id} --recovery --storage-path "
            f"{shlex.quote(storage_path)}"
        )

    shown = f"{len(items)}"
    if page.has_more:
        shown += " (more available)"
    print()
    print(f"  Page: {shown} of at most {limit}")
    if page.has_more and page.next_cursor:
        continuation = (
            f"medre recover --storage-path {shlex.quote(storage_path)} "
            f"--cursor {page.next_cursor}"
        )
        if since is not None:
            continuation += f" --since {since}"
        if limit != DEFAULT_RECOVERY_PAGE_LIMIT:
            continuation += f" --limit {limit}"
        print("  Continue with:")
        print(f"    {continuation}")
    print()
    print("  To re-deliver, preview then execute a replay (needs config):")
    print("    medre replay --mode dry_run --event <event_id> --config <config.yaml>")
    print(
        "    medre replay --mode best_effort --event <event_id> --config <config.yaml>"
    )
    print(f"  ({_REPLAY_CONFIG_NOTE})")


async def _recover(
    event_id: str | None,
    since: str | None,
    limit: int | None,
    cursor: str | None,
    json_output: bool,
    *,
    storage_path: str,
) -> None:
    """Analyze unresolved deliveries and generate a recovery runbook."""
    if event_id is not None:
        scan_only: list[str] = []
        if since is not None:
            scan_only.append("--since")
        if cursor is not None:
            scan_only.append("--cursor")
        if limit is not None:
            scan_only.append("--limit")
        if scan_only:
            joined = ", ".join(scan_only)
            print(
                f"Error: --event cannot be combined with scan-only option(s): {joined}",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)

    page_limit = limit if limit is not None else DEFAULT_RECOVERY_PAGE_LIMIT
    storage = await _open_readonly_storage(storage_path)
    try:
        if event_id is not None:
            # Single-event recovery.
            runbook = await _build_event_recovery_runbook(
                storage, event_id, storage_path=storage_path
            )
            if runbook is None:
                print(
                    f"Error: event not found: {event_id}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_NOT_FOUND)

            if json_output:
                print(to_json(runbook))
            else:
                _print_event_runbook(runbook)
                if runbook.get("warnings"):
                    print()
                    for w in runbook["warnings"]:
                        print(f"  \u26a0 {w}")
            return

        # Broad scan: currently-unresolved deliveries across all events,
        # bounded and keyset-paginated at the storage layer.
        if cursor:
            try:
                decode_page_cursor(cursor)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(EXIT_CONFIG)

        page = await storage.query_unresolved_deliveries(
            cursor=cursor,
            since_event_time=since,
            limit=page_limit,
        )

        scan_report: dict[str, Any] = {
            "scope": "scan",
            "definition": _OUTCOME_DEFINITION,
            "filters": {
                "since": since,
                "since_field": "canonical_events.timestamp",
                "since_inclusive": True,
                "timezone": "UTC (input must carry an explicit offset)",
            },
            "page": {
                "limit": page.limit,
                "count": len(page.items),
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
                "order": page.order,
                "live_view": page.live_view,
            },
            "unresolved": [_scan_record(item) for item in page.items],
            "warnings": [
                "Radio transports (Meshtastic, MeshCore, LXMF) use "
                "fire-and-forget delivery.  Recovery is best-effort "
                "and duplicates are possible.",
            ],
        }
        if json_output:
            print(to_json(scan_report))
        else:
            _print_scan(
                page,
                since=since,
                limit=page_limit,
                storage_path=storage_path,
            )
            if scan_report["warnings"]:
                print()
                for w in scan_report["warnings"]:
                    print(f"  \u26a0 {w}")
    finally:
        await storage.close()
