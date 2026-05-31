"""Pure delivery lifecycle convergence diagnostics.

Detects inconsistencies between outbox item states and delivery receipt
states for the same delivery target, retry metadata anomalies, stalled
delivery plans, attempt count regressions, and receipt sequence gaps.

All functions are pure and read-only — no I/O, no state mutation.
Imports only stdlib + ``.helpers`` + ``.types``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .helpers import (
    _NON_TERMINAL_OUTBOX,
    _NON_TERMINAL_RECEIPT,
    _TERMINAL_OUTBOX,
    _build_outbox_by_key,
    _get,
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

__all__ = ["build_lifecycle_convergence_findings"]

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

#: Terminal receipt statuses used for the terminal-receipt / non-terminal-outbox
#: check.  ``dead_lettered`` is included because a dead-lettered receipt is
#: terminal from the receipt state-machine's perspective.
_TERMINAL_RECEIPT_FOR_MISMATCH = frozenset({"sent", "suppressed", "dead_lettered"})

#: Retry policy metadata fields that should be present on a retryable receipt.
_RETRY_POLICY_FIELDS = (
    "retry_max_attempts",
    "retry_backoff_base",
    "retry_max_delay",
    "retry_jitter",
)


# ---------------------------------------------------------------------------
# Timestamp parsing helper
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: Any) -> datetime | tuple[None, str]:
    """Parse a value to a timezone-aware ``datetime``.

    Returns ``datetime`` on success or ``(None, error_message)`` on failure.
    """
    if value is None:
        return (None, "timestamp is None")
    s = _to_iso(value)
    if not s:
        return (None, "timestamp is empty")
    try:
        dt = datetime.fromisoformat(s)
        return dt
    except (ValueError, TypeError) as exc:
        return (None, str(exc))


def _ensure_aware(dt: datetime) -> datetime:
    """Assume UTC when a datetime is naive (no tzinfo)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# record_id helper
# ---------------------------------------------------------------------------


def _safe_record_id(*candidates: Any) -> str:
    """Return the first non-empty string candidate, never ``"None"``."""
    for c in candidates:
        s = str(c) if c is not None else ""
        if s and s != "None":
            return s
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_lifecycle_convergence_findings(
    outbox_items: Iterable[Any] = (),
    receipts: Iterable[Any] = (),
    *,
    now_fn: Callable[[], datetime] | None = None,
    stall_threshold_seconds: int = 3600,
) -> list[OrphanFinding]:
    """Build lifecycle delivery convergence findings.

    Parameters
    ----------
    outbox_items:
        Duck-typed outbox item records (dataclasses or dicts).
    receipts:
        Duck-typed receipt records (dataclasses or dicts).
    now_fn:
        Callable returning the current ``datetime`` for time-based checks.
        Defaults to ``datetime.now(timezone.utc)``.
    stall_threshold_seconds:
        Seconds after which a non-terminal outbox with an unchanged
        ``updated_at`` is considered stalled.  Defaults to 3600 (1 hour).

    Returns
    -------
    list[OrphanFinding]
        Deterministically sorted by ``(kind, record_id)``.
    """
    findings: list[OrphanFinding] = []

    # -- Materialize iterables once (support one-shot generators) -----------
    outbox_list = list(outbox_items)
    receipt_list = list(receipts)

    if now_fn is None:

        def _default_now():
            return datetime.now(timezone.utc)

        now_fn = _default_now

    now = now_fn()

    # -- Index structures ---------------------------------------------------
    outbox_by_key = _build_outbox_by_key(outbox_list)

    receipts_by_key: dict[_TargetKey, list[Any]] = {}
    for rec in receipt_list:
        key = _target_key(rec)
        receipts_by_key.setdefault(key, []).append(rec)

    all_keys = sorted(
        set(outbox_by_key.keys()) | set(receipts_by_key.keys()),
        key=lambda k: (k[0], k[1], k[2] or ""),
    )

    # -- Per-target checks: A, B, C -----------------------------------------
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

        # A. Terminal receipt, non-terminal outbox
        if receipt_is_terminal and outbox_is_non_terminal:
            rid = _safe_record_id(outbox_id, receipt_id)
            if rid:
                findings.append(
                    OrphanFinding(
                        kind=KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX,
                        severity="inconsistent",
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
                # Normal combos that are fine
                _NORMAL_NON_TERMINAL_COMBOS = {
                    ("retry_wait", "failed"),
                    ("retry_wait", "queued"),
                    ("queued", "queued"),
                    ("pending", "queued"),
                    ("in_progress", "queued"),
                    ("in_progress", "failed"),
                }
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

    # -- D & E: retry_wait outbox checks ------------------------------------
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

    # -- F: Retryable receipt without retry metadata ------------------------
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

    # -- G: Stalled delivery plan -------------------------------------------
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

    # -- H: Attempt count regression ----------------------------------------
    for key, recs in receipts_by_key.items():
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

    # -- I: Receipt sequence gap --------------------------------------------
    for key, recs in receipts_by_key.items():
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

    # -- Deterministic sort -------------------------------------------------
    findings.sort(key=lambda f: (f.kind, f.record_id))

    return findings


# ---------------------------------------------------------------------------
# Internal: safe latest-receipt selection
# ---------------------------------------------------------------------------


def _pick_latest_receipt_safe(receipts: list[Any]) -> Any | None:
    """Select the latest receipt from a list, handling empty lists."""
    if not receipts:
        return None
    from .helpers import _pick_latest_receipt

    return _pick_latest_receipt(receipts)
