"""Lifecycle convergence check functions.

Each function is a standalone pure check that takes pre-built index
structures and returns a list of :class:`OrphanFinding`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .helpers import (
    _NON_TERMINAL_OUTBOX,
    _NON_TERMINAL_RECEIPT,
    _TERMINAL_OUTBOX,
)
from .helpers import _TERMINAL_RECEIPT as _TERMINAL_RECEIPT_FOR_MISMATCH
from .helpers import (
    _ensure_aware,
    _get,
    _parse_iso_timestamp,
    _pick_latest_receipt_safe,
    _safe_record_id,
    _target_key,
    _TargetKey,
    _to_iso,
)
from .types import (
    KIND_ATTEMPT_COUNT_REGRESSION,
    KIND_NEXT_RETRY_IN_PAST,
    KIND_RECEIPT_OUTBOX_MISMATCH,
    KIND_RECEIPT_SEQUENCE_GAP,
    KIND_RETRY_WAIT_MISSING_NEXT_RETRY,
    KIND_RETRYABLE_WITHOUT_RETRY_METADATA,
    KIND_STALLED_DELIVERY_PLAN,
    KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT,
    KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX,
    OrphanFinding,
)

__all__ = [
    "_check_target_mismatches",
    "_check_retry_wait_outboxes",
    "_check_retryable_without_metadata",
    "_check_stalled_delivery_plans",
    "_check_attempt_count_regression",
    "_check_receipt_sequence_gap",
]


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

#: Retry policy metadata fields that should be present on a retryable receipt.
_RETRY_POLICY_FIELDS = (
    "retry_max_attempts",
    "retry_backoff_base",
    "retry_max_delay",
    "retry_jitter",
)

#: Normal non-terminal (outbox, receipt) status combinations — these are
#: not flagged as degraded mismatches.
_NORMAL_NON_TERMINAL_COMBOS = frozenset(
    {
        ("retry_wait", "failed"),
        ("retry_wait", "queued"),
        ("queued", "queued"),
        ("pending", "queued"),
        ("in_progress", "queued"),
        ("in_progress", "failed"),
    }
)


# ---------------------------------------------------------------------------
# Check A, B, C: Per-target status mismatches
# ---------------------------------------------------------------------------


def _check_target_mismatches(
    outbox_by_key: dict[_TargetKey, Any],
    receipts_by_key: dict[_TargetKey, list[Any]],
    all_keys: list[_TargetKey],
) -> list[OrphanFinding]:
    """Detect terminal/non-terminal and status mismatches between outbox and receipt."""
    findings: list[OrphanFinding] = []

    for key in all_keys:
        obx = outbox_by_key.get(key)
        recs = receipts_by_key.get(key, [])
        latest_rec = _pick_latest_receipt_safe(recs)

        has_outbox = obx is not None
        has_receipt = latest_rec is not None

        if not (has_outbox and has_receipt):
            continue

        outbox_status = str(_get(obx, "status", "") or "").lower()
        receipt_status = str(_get(latest_rec, "status", "") or "").lower()

        outbox_id = _safe_record_id(_get(obx, "outbox_id"))
        receipt_id = _safe_record_id(_get(latest_rec, "receipt_id"))

        receipt_is_terminal = receipt_status in _TERMINAL_RECEIPT_FOR_MISMATCH
        outbox_is_terminal = outbox_status in _TERMINAL_OUTBOX
        receipt_is_non_terminal = receipt_status in _NON_TERMINAL_RECEIPT
        outbox_is_non_terminal = outbox_status in _NON_TERMINAL_OUTBOX

        fired = False

        # A, B, and C are mutually exclusive per target: only the first
        # matching category emits a finding so that a single target is never
        # reported under more than one mismatch kind.

        # A. Terminal receipt, non-terminal outbox
        # Severity: degraded (not inconsistent).  Receipt writes and outbox
        # updates are separate SQLite transactions, so a snapshot can
        # legitimately observe terminal receipt with non-terminal outbox.
        if receipt_is_terminal and outbox_is_non_terminal:
            rid = _safe_record_id(outbox_id, receipt_id)
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX,
                        severity="degraded",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Terminal receipt ({receipt_status}) but outbox is "
                            f"non-terminal ({outbox_status}) for target {key!r}"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "receipt_id": receipt_id,
                            "outbox_status": outbox_status,
                            "receipt_status": receipt_status,
                        },
                    )
                )
                fired = True

        # B. Terminal outbox, non-terminal receipt
        if not fired and outbox_is_terminal and receipt_is_non_terminal:
            rid = _safe_record_id(outbox_id, receipt_id)
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT,
                        severity="inconsistent",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Terminal outbox ({outbox_status}) but receipt is "
                            f"non-terminal ({receipt_status}) for target {key!r}"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "receipt_id": receipt_id,
                            "outbox_status": outbox_status,
                            "receipt_status": receipt_status,
                        },
                    )
                )
                fired = True

        # C. Both present but statuses contradict normal flow (not A/B)
        if not fired:
            is_degraded_combo = False
            # Both terminal but different statuses
            if (
                outbox_is_terminal
                and receipt_is_terminal
                and outbox_status != receipt_status
            ):
                is_degraded_combo = True
            # Both non-terminal but the specific combo is contradictory
            if outbox_is_non_terminal and receipt_is_non_terminal:
                if (outbox_status, receipt_status) not in _NORMAL_NON_TERMINAL_COMBOS:
                    is_degraded_combo = True

            if is_degraded_combo:
                rid = _safe_record_id(outbox_id, receipt_id)
                if rid:
                    findings.append(
                        OrphanFinding(
                            kind=KIND_RECEIPT_OUTBOX_MISMATCH,
                            severity="degraded",
                            record_id=rid,
                            record_type="outbox",
                            details=(
                                f"Receipt/outbox status mismatch: outbox={outbox_status}, "
                                f"receipt={receipt_status} for target {key!r}"
                            ),
                            extra={
                                "outbox_id": outbox_id,
                                "receipt_id": receipt_id,
                                "outbox_status": outbox_status,
                                "receipt_status": receipt_status,
                            },
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# Check D, E: retry_wait outbox checks
# ---------------------------------------------------------------------------


def _check_retry_wait_outboxes(
    outbox_list: list[Any],
    now: datetime,
) -> list[OrphanFinding]:
    """Check retry_wait outboxes for missing/unparsable or past next_attempt_at."""
    findings: list[OrphanFinding] = []

    for obx in outbox_list:
        status = str(_get(obx, "status", "") or "").lower()
        if status != "retry_wait":
            continue

        outbox_id = _safe_record_id(_get(obx, "outbox_id"))
        next_attempt_raw = _get(obx, "next_attempt_at")

        # D. Missing/unparsable next_attempt_at
        if not next_attempt_raw:
            rid = outbox_id
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_RETRY_WAIT_MISSING_NEXT_RETRY,
                        severity="inconsistent",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Outbox {outbox_id!r} is retry_wait but "
                            f"next_attempt_at is missing or empty"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "status": status,
                            "next_attempt_at": None,
                        },
                    )
                )
            continue

        parsed = _parse_iso_timestamp(next_attempt_raw)
        if isinstance(parsed, tuple):
            # Parse failure
            _dt, parse_error = parsed
            rid = outbox_id
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_RETRY_WAIT_MISSING_NEXT_RETRY,
                        severity="inconsistent",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Outbox {outbox_id!r} is retry_wait but "
                            f"next_attempt_at is unparsable: {parse_error}"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "status": status,
                            "next_attempt_at": _to_iso(next_attempt_raw),
                            "parse_error": parse_error,
                        },
                    )
                )
            continue

        next_dt = _ensure_aware(parsed)
        if next_dt < now:
            rid = outbox_id
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_NEXT_RETRY_IN_PAST,
                        severity="degraded",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Outbox {outbox_id!r} is retry_wait but "
                            f"next_attempt_at ({_to_iso(next_dt)}) is in the past"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "status": status,
                            "next_attempt_at": _to_iso(next_dt),
                            "now": _to_iso(now),
                        },
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check F: Retryable receipt without retry metadata
# ---------------------------------------------------------------------------


def _check_retryable_without_metadata(
    receipt_list: list[Any],
    outbox_by_key: dict[_TargetKey, Any],
) -> list[OrphanFinding]:
    """Check failed receipts with transient failure_kind but missing retry metadata."""
    findings: list[OrphanFinding] = []

    for rec in receipt_list:
        rec_status = str(_get(rec, "status", "") or "").lower()
        if rec_status != "failed":
            continue

        receipt_id = _safe_record_id(_get(rec, "receipt_id"))

        # Determine if the receipt looks retryable
        failure_kind = str(_get(rec, "failure_kind", "") or "").lower()
        is_transient = failure_kind == "adapter_transient"

        # Check if there's a matching non-terminal outbox for this receipt's target
        key = _target_key(rec)
        obx = outbox_by_key.get(key)
        has_matching_non_terminal_outbox = (
            obx is not None
            and str(_get(obx, "status", "") or "").lower() in _NON_TERMINAL_OUTBOX
        )

        is_retryable = is_transient or has_matching_non_terminal_outbox
        if not is_retryable:
            continue

        # Check for missing retry metadata
        next_retry_at = _get(rec, "next_retry_at")
        has_next_retry = bool(next_retry_at)

        missing_fields: list[str] = []
        if not has_next_retry:
            missing_fields.append("next_retry_at")
        for field_name in _RETRY_POLICY_FIELDS:
            val = _get(rec, field_name)
            if val is None or val == "":
                missing_fields.append(field_name)

        if missing_fields:
            rid = receipt_id
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_RETRYABLE_WITHOUT_RETRY_METADATA,
                        severity="degraded",
                        record_id=rid,
                        record_type="receipt",
                        details=(
                            f"Failed receipt {receipt_id!r} appears retryable "
                            f"but is missing retry metadata: {', '.join(missing_fields)}"
                        ),
                        extra={
                            "receipt_id": receipt_id,
                            "failure_kind": failure_kind,
                            "missing_fields": missing_fields,
                        },
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check G: Stalled delivery plan
# ---------------------------------------------------------------------------


def _check_stalled_delivery_plans(
    outbox_list: list[Any],
    now: datetime,
    stall_threshold_seconds: int,
) -> list[OrphanFinding]:
    """Check non-terminal outboxes with stale updated_at timestamps."""
    findings: list[OrphanFinding] = []

    for obx in outbox_list:
        status = str(_get(obx, "status", "") or "").lower()
        if status not in _NON_TERMINAL_OUTBOX:
            continue

        updated_at_raw = _get(obx, "updated_at")
        if not updated_at_raw:
            continue

        parsed = _parse_iso_timestamp(updated_at_raw)
        if isinstance(parsed, tuple):
            # Can't parse — skip stall check (not a stall issue)
            continue

        updated_dt = _ensure_aware(parsed)
        delta_seconds = (now - updated_dt).total_seconds()
        if delta_seconds > stall_threshold_seconds:
            outbox_id = _safe_record_id(_get(obx, "outbox_id"))
            rid = outbox_id
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_STALLED_DELIVERY_PLAN,
                        severity="degraded",
                        record_id=rid,
                        record_type="outbox",
                        details=(
                            f"Non-terminal outbox {outbox_id!r} ({status}) "
                            f"has updated_at {_to_iso(updated_dt)} which is "
                            f"{int(delta_seconds)}s old (threshold: "
                            f"{stall_threshold_seconds}s)"
                        ),
                        extra={
                            "outbox_id": outbox_id,
                            "status": status,
                            "updated_at": _to_iso(updated_dt),
                            "seconds_stalled": int(delta_seconds),
                            "stall_threshold_seconds": stall_threshold_seconds,
                        },
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check H: Attempt count regression
# ---------------------------------------------------------------------------


def _check_attempt_count_regression(
    receipts_by_key: dict[_TargetKey, list[Any]],
) -> list[OrphanFinding]:
    """Check for attempt_number decreasing in later receipts."""
    findings: list[OrphanFinding] = []

    for _key, recs in receipts_by_key.items():
        if len(recs) < 2:
            continue

        # Sort by (sequence, created_at, receipt_id) ascending
        sorted_recs = sorted(
            recs,
            key=lambda r: (
                _get(r, "sequence") or 0,
                _to_iso(_get(r, "created_at")) or "",
                _get(r, "receipt_id") or "",
            ),
        )

        for i in range(1, len(sorted_recs)):
            prev_attempt = _get(sorted_recs[i - 1], "attempt_number") or 0
            curr_attempt = _get(sorted_recs[i], "attempt_number") or 0

            if curr_attempt < prev_attempt:
                curr_id = _safe_record_id(_get(sorted_recs[i], "receipt_id"))
                if curr_id:
                    findings.append(
                        OrphanFinding(
                            kind=KIND_ATTEMPT_COUNT_REGRESSION,
                            severity="inconsistent",
                            record_id=curr_id,
                            record_type="receipt",
                            details=(
                                f"Receipt {curr_id!r} has attempt_number "
                                f"{curr_attempt} but earlier receipt has "
                                f"{prev_attempt} — attempt count regression"
                            ),
                            extra={
                                "receipt_id": curr_id,
                                "attempt_number": curr_attempt,
                                "previous_attempt_number": prev_attempt,
                            },
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# Check I: Receipt sequence gap
# ---------------------------------------------------------------------------


def _check_receipt_sequence_gap(
    receipts_by_key: dict[_TargetKey, list[Any]],
) -> list[OrphanFinding]:
    """Check for gaps in receipt sequence numbers."""
    findings: list[OrphanFinding] = []

    for _key, recs in receipts_by_key.items():
        if len(recs) < 2:
            continue

        # Collect positive integer sequences
        seq_map: list[tuple[int, Any]] = []
        for rec in recs:
            seq = _get(rec, "sequence")
            if isinstance(seq, (int, float)) and int(seq) > 0:
                seq_map.append((int(seq), rec))

        if len(seq_map) < 2:
            continue

        seq_map.sort(key=lambda t: t[0])

        for i in range(1, len(seq_map)):
            prev_seq = seq_map[i - 1][0]
            curr_seq = seq_map[i][0]
            gap = curr_seq - prev_seq
            if gap > 1:
                rec = seq_map[i][1]
                rec_id = _safe_record_id(_get(rec, "receipt_id"))
                if rec_id:
                    findings.append(
                        OrphanFinding(
                            kind=KIND_RECEIPT_SEQUENCE_GAP,
                            severity="degraded",
                            record_id=rec_id,
                            record_type="receipt",
                            details=(
                                f"Receipt {rec_id!r} sequence {curr_seq} "
                                f"follows {prev_seq} — gap of {gap}"
                            ),
                            extra={
                                "receipt_id": rec_id,
                                "sequence": curr_seq,
                                "previous_sequence": prev_seq,
                                "gap": gap,
                            },
                        )
                    )

    return findings
