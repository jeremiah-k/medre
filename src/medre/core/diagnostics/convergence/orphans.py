"""Orphan / invalid-lineage detection for persisted outbox+receipt snapshots.

Detects orphaned outbox items, broken receipt lineage, cross-plan and
cross-event parent references, missing delivery plan IDs on retry
receipts, and dead-lettered/retryable mismatches.

Pure and read-only: no storage I/O, no state mutation, no side effects.
"""

from __future__ import annotations

from typing import Any, Iterable

from .helpers import (
    _NON_TERMINAL_OUTBOX,
    _NON_TERMINAL_RECEIPT,
    _get,
    _latest_receipt_for_target,
    _target_key,
    _TargetKey,
    _worst_severity,
)
from .types import (
    KIND_CROSS_EVENT_PARENT,
    KIND_CROSS_PLAN_PARENT,
    KIND_DEAD_LETTERED_RETRYABLE_MISMATCH,
    KIND_MISSING_DELIVERY_PLAN_ID,
    KIND_ORPHANED_OUTBOX,
    KIND_ORPHANED_PARENT_RECEIPT,
    ConvergenceSeverity,
    OrphanFinding,
    OrphanReport,
)

__all__ = [
    "build_orphan_report",
]


def build_orphan_report(
    receipts: Iterable[Any] = (),
    outbox_items: Iterable[Any] = (),
    known_event_ids: set[str] | frozenset[str] | None = None,
) -> OrphanReport:
    """Build an orphan / invalid-lineage report from receipt and outbox snapshots.

    Pure function — no I/O, no state mutation, no storage access.
    Accepts dataclass/struct objects or dict-like records for each
    parameter.

    Parameters
    ----------
    receipts:
        Delivery receipt records (``DeliveryReceipt`` objects or dicts).
    outbox_items:
        Outbox item records (``DeliveryOutboxItem`` objects or dicts).
    known_event_ids:
        Set of event IDs known to exist in the event store.  When
        supplied, non-terminal outbox items whose ``event_id`` is not
        in this set are flagged as orphaned.  ``None`` (default) means
        the caller has no event catalogue and orphaned-outbox checks
        are skipped.

    Returns
    -------
    OrphanReport
        Frozen, JSON-safe orphan report with deterministic ordering.

    Detection rules
    ---------------
    ``orphaned_outbox`` (inconsistent):
        Non-terminal outbox item whose ``event_id`` is absent from
        *known_event_ids*.  Only checked when *known_event_ids* is
        provided.

    ``orphaned_parent_receipt`` (inconsistent):
        Receipt with a non-empty ``parent_receipt_id`` that does not
        exist in the supplied receipt set.

    ``cross_plan_parent`` (inconsistent):
        Receipt whose parent exists but has a different
        ``delivery_plan_id``.

    ``cross_event_parent`` (inconsistent):
        Receipt whose parent exists but has a different ``event_id``.

    ``missing_delivery_plan_id`` (degraded):
        Receipt with ``source="retry"`` whose ``delivery_plan_id`` is
        empty or ``None``.  Degraded because the retry may still succeed
        once the plan ID is resolved.

    ``dead_lettered_retryable_mismatch`` (degraded):
        Outbox item with ``dead_lettered`` status whose latest receipt
        for the same target key is non-terminal (``failed`` or
        ``queued``), suggesting the item may still be retryable despite
        the terminal outbox status.  Degraded because the discrepancy
        is recoverable through re-delivery.
    """
    receipt_list = list(receipts)
    outbox_list = list(outbox_items)

    findings: list[OrphanFinding] = []

    # --- Index receipts by receipt_id for parent lookups ------------------
    receipt_by_id: dict[str, Any] = {}
    for rec in receipt_list:
        rid = _get(rec, "receipt_id") or ""
        if rid:
            receipt_by_id[rid] = rec

    # --- Index outbox items by target key ---------------------------------
    outbox_by_key: dict[_TargetKey, Any] = {}
    for obx in outbox_list:
        key = _target_key(obx)
        existing = outbox_by_key.get(key)
        if existing is None:
            outbox_by_key[key] = obx
        else:
            existing_attempt = _get(existing, "attempt_number") or 0
            new_attempt = _get(obx, "attempt_number") or 0
            if new_attempt > existing_attempt:
                outbox_by_key[key] = obx
            elif new_attempt == existing_attempt:
                existing_id = _get(existing, "outbox_id") or ""
                new_id = _get(obx, "outbox_id") or ""
                if new_id > existing_id:
                    outbox_by_key[key] = obx

    # --- Index receipts by target key -------------------------------------
    receipts_by_key: dict[_TargetKey, list[Any]] = {}
    for rec in receipt_list:
        key = _target_key(rec)
        receipts_by_key.setdefault(key, []).append(rec)

    # --- 1. Orphaned outbox (event_id not in known_event_ids) -------------
    if known_event_ids is not None:
        event_id_set = (
            known_event_ids
            if isinstance(known_event_ids, (set, frozenset))
            else set(known_event_ids)
        )
        for obx in outbox_list:
            status = _get(obx, "status")
            if status in _NON_TERMINAL_OUTBOX:
                eid = _get(obx, "event_id") or ""
                if eid and eid not in event_id_set:
                    oid = _get(obx, "outbox_id") or ""
                    findings.append(
                        OrphanFinding(
                            kind=KIND_ORPHANED_OUTBOX,
                            severity=ConvergenceSeverity.INCONSISTENT.value,
                            record_id=oid,
                            record_type="outbox",
                            details=(
                                f"Non-terminal outbox item {oid} references "
                                f"event_id {eid!r} not found in known_event_ids"
                            ),
                            extra={
                                "event_id": eid,
                                "outbox_id": oid,
                                "status": status,
                            },
                        )
                    )

    # --- 2. Receipt parent lineage checks ---------------------------------
    for rec in receipt_list:
        parent_id = _get(rec, "parent_receipt_id")
        if not parent_id:
            continue

        receipt_id = _get(rec, "receipt_id") or ""

        parent = receipt_by_id.get(parent_id)

        # 2a. Orphaned parent receipt
        if parent is None:
            findings.append(
                OrphanFinding(
                    kind=KIND_ORPHANED_PARENT_RECEIPT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} references parent_receipt_id "
                        f"{parent_id!r} which does not exist in the receipt set"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                    },
                )
            )
            continue

        # 2b. Cross-plan parent
        parent_plan = _get(parent, "delivery_plan_id") or ""
        child_plan = _get(rec, "delivery_plan_id") or ""
        if parent_plan != child_plan:
            findings.append(
                OrphanFinding(
                    kind=KIND_CROSS_PLAN_PARENT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} (plan={child_plan!r}) has "
                        f"parent_receipt_id {parent_id!r} belonging to "
                        f"different plan {parent_plan!r}"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                        "delivery_plan_id": child_plan,
                        "parent_delivery_plan_id": parent_plan,
                    },
                )
            )

        # 2c. Cross-event parent
        parent_event = _get(parent, "event_id") or ""
        child_event = _get(rec, "event_id") or ""
        if parent_event != child_event:
            findings.append(
                OrphanFinding(
                    kind=KIND_CROSS_EVENT_PARENT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} (event={child_event!r}) has "
                        f"parent_receipt_id {parent_id!r} belonging to "
                        f"different event {parent_event!r}"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                        "event_id": child_event,
                        "parent_event_id": parent_event,
                    },
                )
            )

    # --- 3. Missing delivery_plan_id on retry receipts --------------------
    for rec in receipt_list:
        source = _get(rec, "source") or ""
        if source == "retry":
            plan_id = _get(rec, "delivery_plan_id") or ""
            if not plan_id:
                receipt_id = _get(rec, "receipt_id") or ""
                findings.append(
                    OrphanFinding(
                        kind=KIND_MISSING_DELIVERY_PLAN_ID,
                        severity=ConvergenceSeverity.DEGRADED.value,
                        record_id=receipt_id,
                        record_type="receipt",
                        details=(
                            f"Retry receipt {receipt_id} has missing or "
                            f"empty delivery_plan_id"
                        ),
                        extra={
                            "receipt_id": receipt_id,
                            "source": source,
                        },
                    )
                )

    # --- 4. Dead-lettered outbox with retryable receipt -------------------
    for key, obx in outbox_by_key.items():
        outbox_status = _get(obx, "status")
        if outbox_status != "dead_lettered":
            continue

        latest_rec = _latest_receipt_for_target(receipts_by_key, key)
        if latest_rec is None:
            continue

        receipt_status = _get(latest_rec, "status") or ""
        if receipt_status in _NON_TERMINAL_RECEIPT:
            oid = _get(obx, "outbox_id") or ""
            rid = _get(latest_rec, "receipt_id") or ""
            findings.append(
                OrphanFinding(
                    kind=KIND_DEAD_LETTERED_RETRYABLE_MISMATCH,
                    severity=ConvergenceSeverity.DEGRADED.value,
                    record_id=oid,
                    record_type="outbox",
                    details=(
                        f"Dead-lettered outbox item {oid} has latest "
                        f"receipt {rid} with non-terminal status "
                        f"{receipt_status!r}; item may be retryable"
                    ),
                    extra={
                        "outbox_id": oid,
                        "receipt_id": rid,
                        "outbox_status": outbox_status,
                        "receipt_status": receipt_status,
                    },
                )
            )

    # --- Deterministic sort and aggregate ---------------------------------
    findings.sort(key=lambda f: (f.kind, f.record_id))

    severity_counts: dict[str, int] = {
        ConvergenceSeverity.SAFE.value: 0,
        ConvergenceSeverity.DEGRADED.value: 0,
        ConvergenceSeverity.INCONSISTENT.value: 0,
    }
    finding_severities: list[ConvergenceSeverity] = []
    for finding in findings:
        sev = ConvergenceSeverity(finding.severity)
        severity_counts[sev.value] += 1
        finding_severities.append(sev)

    total = len(findings)
    worst = _worst_severity(finding_severities)

    summary = (
        f"{total} finding(s): "
        f"{severity_counts[ConvergenceSeverity.INCONSISTENT.value]} inconsistent, "
        f"{severity_counts[ConvergenceSeverity.DEGRADED.value]} degraded"
        if total > 0
        else "No orphan or invalid-lineage findings"
    )

    return OrphanReport(
        findings=tuple(findings),
        total_findings=total,
        severity_counts=severity_counts,
        worst_severity=worst,
        summary=summary,
    )
