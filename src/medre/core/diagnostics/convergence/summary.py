"""Convergence summary builder — per-target classification and aggregation.

Groups outbox items and receipts by delivery target key, classifies each
target's convergence state, and aggregates results into a
:class:`~convergence.types.ConvergenceSummary`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .helpers import (
    _NON_TERMINAL_OUTBOX,
    _NON_TERMINAL_RECEIPT,
    _TERMINAL_OUTBOX,
    _TERMINAL_RECEIPT,
    _build_outbox_by_key,
    _get,
    _pick_latest_receipt,
    _target_key,
    _TargetKey,
    _worst_severity,
)
from .types import (
    ConvergenceSeverity,
    ConvergenceSummary,
    DeliveryTargetConvergence,
)

__all__ = [
    "build_convergence_summary",
]


# ---------------------------------------------------------------------------
# Classification engine
# ---------------------------------------------------------------------------


def _classify_target(
    *,
    outbox_status: str | None,
    latest_receipt_status: str | None,
    has_outbox: bool,
    has_receipt: bool,
    plan_id_present: bool,
) -> tuple[ConvergenceSeverity, list[str]]:
    """Classify a single delivery target's convergence state.

    Returns ``(severity, warnings)`` where *warnings* is a (possibly
    empty) list of human-readable diagnostic messages.
    """
    warnings: list[str] = []
    outbox_terminal = outbox_status in _TERMINAL_OUTBOX if outbox_status else False
    outbox_non_terminal = (
        outbox_status in _NON_TERMINAL_OUTBOX if outbox_status else False
    )
    receipt_terminal = (
        latest_receipt_status in _TERMINAL_RECEIPT if latest_receipt_status else False
    )
    receipt_non_terminal = (
        latest_receipt_status in _NON_TERMINAL_RECEIPT
        if latest_receipt_status
        else False
    )

    # --- No delivery_plan_id: degraded with warning ---
    if not plan_id_present:
        if has_outbox or has_receipt:
            warnings.append("delivery_plan_id is empty; target grouped by fallback key")
            # Still classify — but degrade if we can't fully reconcile
            if has_outbox and has_receipt:
                if (
                    outbox_terminal
                    and receipt_terminal
                    and outbox_status == latest_receipt_status
                ):
                    return ConvergenceSeverity.DEGRADED, warnings
                return ConvergenceSeverity.DEGRADED, warnings
            if has_outbox:
                if outbox_non_terminal:
                    return ConvergenceSeverity.DEGRADED, warnings
                return ConvergenceSeverity.DEGRADED, warnings
            # Receipt-only
            if receipt_terminal:
                warnings.append(
                    "Receipt-only terminal evidence without delivery_plan_id"
                )
                return ConvergenceSeverity.DEGRADED, warnings
            return ConvergenceSeverity.DEGRADED, warnings
        return ConvergenceSeverity.DEGRADED, warnings

    # --- Both outbox and receipt present ---
    if has_outbox and has_receipt:
        # Matching terminal: safe
        if outbox_terminal and receipt_terminal:
            if outbox_status == latest_receipt_status:
                return ConvergenceSeverity.SAFE, warnings
            # Both terminal but different (e.g. sent + dead_lettered from old receipt)
            # The outbox is the operational authority — if outbox says terminal,
            # and receipt is also terminal, this is safe even if statuses differ
            # because both agree work is done.
            warnings.append(
                f"Both terminal but statuses differ: outbox={outbox_status}, "
                f"receipt={latest_receipt_status}"
            )
            return ConvergenceSeverity.SAFE, warnings

        # Terminal outbox + non-terminal receipt: inconsistent
        if outbox_terminal and receipt_non_terminal:
            warnings.append(
                f"Terminal outbox ({outbox_status}) with non-terminal receipt "
                f"({latest_receipt_status})"
            )
            return ConvergenceSeverity.INCONSISTENT, warnings

        # Non-terminal outbox + terminal receipt sent/suppressed: inconsistent
        if outbox_non_terminal and receipt_terminal:
            if latest_receipt_status in ("sent", "suppressed"):
                warnings.append(
                    f"Non-terminal outbox ({outbox_status}) but receipt claims "
                    f"terminal ({latest_receipt_status})"
                )
                return ConvergenceSeverity.INCONSISTENT, warnings
            # receipt dead_lettered with non-terminal outbox retry_wait: degraded
            # (outbox retry may produce a different outcome)
            return ConvergenceSeverity.DEGRADED, warnings

        # Non-terminal outbox + failed receipt: degraded (retry expected)
        if outbox_non_terminal and latest_receipt_status == "failed":
            return ConvergenceSeverity.DEGRADED, warnings

        # Non-terminal outbox + queued receipt: degraded (in flight)
        if outbox_non_terminal and latest_receipt_status == "queued":
            return ConvergenceSeverity.DEGRADED, warnings

        # Both non-terminal but not a known combination
        if outbox_non_terminal and receipt_non_terminal:
            return ConvergenceSeverity.DEGRADED, warnings

        # Unrecognised outbox status with both outbox and receipt present
        if not outbox_terminal and not outbox_non_terminal:
            warnings.append(f"Unrecognised outbox status: {outbox_status!r}")
            return ConvergenceSeverity.DEGRADED, warnings

    # --- Outbox only, no receipt ---
    if has_outbox and not has_receipt:
        if outbox_non_terminal:
            # in_progress / queued without receipt: mid-flight, degraded
            if outbox_status in ("in_progress", "queued"):
                warnings.append(
                    f"Outbox {outbox_status} with no receipt; mid-flight or "
                    f"receipt not yet written"
                )
            return ConvergenceSeverity.DEGRADED, warnings
        if not outbox_terminal:
            # Unrecognised outbox status without receipt → degraded
            warnings.append(f"Unrecognised outbox status: {outbox_status!r}")
            return ConvergenceSeverity.DEGRADED, warnings
        # Terminal outbox without receipt: safe (receipt may not exist yet or was
        # never created for cancelled/abandoned)
        return ConvergenceSeverity.SAFE, warnings

    # --- Receipt only, no outbox ---
    if has_receipt and not has_outbox:
        if receipt_terminal:
            warnings.append(
                "Receipt-only terminal evidence; outbox item absent "
                "(completed and cleaned up, or never created)"
            )
            return ConvergenceSeverity.SAFE, warnings
        # Non-terminal receipt without outbox: degraded
        warnings.append(
            f"Receipt-only non-terminal ({latest_receipt_status}); "
            f"no outbox item found"
        )
        return ConvergenceSeverity.DEGRADED, warnings

    # --- Neither outbox nor receipt (should not happen at this point) ---
    return ConvergenceSeverity.SAFE, warnings


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_convergence_summary(
    receipts: Iterable[Any] = (),
    outbox_items: Iterable[Any] = (),
) -> ConvergenceSummary:
    """Build a convergence diagnostics summary from receipt and outbox snapshots.

    Pure function — no I/O, no state mutation, no storage access.
    Accepts dataclass/struct objects or dict-like records for each
    parameter.

    Parameters
    ----------
    receipts:
        Delivery receipt records (``DeliveryReceipt`` objects or dicts).
    outbox_items:
        Outbox item records (``DeliveryOutboxItem`` objects or dicts).

    Returns
    -------
    ConvergenceSummary
        Frozen, JSON-safe convergence summary.

    Notes
    -----
    * Targets are grouped by ``(delivery_plan_id, target_adapter,
      target_channel)`` with fallbacks for missing plan/channel.
    * The latest receipt is selected deterministically by
      ``(attempt_number DESC, sequence DESC, created_at DESC,
      receipt_id DESC)`` without relying on object identity.
    * ``orphan_count`` is ``None`` until linked to an orphan report;
      ``evidence_bundle_ref`` is ``None`` until attached to an
      evidence bundle.
    """
    receipt_list = list(receipts)
    outbox_list = list(outbox_items)

    # --- Build target-keyed maps ------------------------------------------
    # Outbox items: at most one per target key (latest by attempt_number).
    outbox_by_key = _build_outbox_by_key(outbox_list)

    # Receipts: group by target key.
    receipts_by_key: dict[_TargetKey, list[Any]] = {}
    for rec in receipt_list:
        key = _target_key(rec)
        receipts_by_key.setdefault(key, []).append(rec)

    # --- Collect all target keys (union) ----------------------------------
    def _sort_key(key: _TargetKey) -> tuple:
        plan_id, adapter, channel = key
        return (plan_id, adapter, channel or "")

    all_keys = sorted(
        set(outbox_by_key.keys()) | set(receipts_by_key.keys()),
        key=_sort_key,
    )

    # --- Classify each target ---------------------------------------------
    targets: list[DeliveryTargetConvergence] = []
    severities: list[ConvergenceSeverity] = []
    global_warnings: list[str] = []

    for key in all_keys:
        plan_id, adapter, channel = key
        obx = outbox_by_key.get(key)
        recs = receipts_by_key.get(key, [])

        outbox_status = _get(obx, "status") if obx else None
        outbox_id = _get(obx, "outbox_id") if obx else None
        has_outbox = obx is not None
        has_receipt = len(recs) > 0
        plan_id_present = bool(plan_id)

        latest_rec = _pick_latest_receipt(recs)
        latest_receipt_status = _get(latest_rec, "status") if latest_rec else None
        latest_receipt_id = _get(latest_rec, "receipt_id") if latest_rec else None
        latest_attempt_number = (
            _get(latest_rec, "attempt_number") if latest_rec else None
        )

        severity, target_warnings = _classify_target(
            outbox_status=outbox_status,
            latest_receipt_status=latest_receipt_status,
            has_outbox=has_outbox,
            has_receipt=has_receipt,
            plan_id_present=plan_id_present,
        )
        severities.append(severity)

        targets.append(
            DeliveryTargetConvergence(
                delivery_plan_id=plan_id,
                target_adapter=adapter,
                target_channel=channel,
                outbox_status=outbox_status,
                latest_receipt_status=latest_receipt_status,
                latest_receipt_id=latest_receipt_id,
                latest_attempt_number=latest_attempt_number,
                severity=severity.value,
                warnings=tuple(target_warnings),
                outbox_id=outbox_id,
            )
        )
        global_warnings.extend(target_warnings)

    # --- Aggregate severity counts ----------------------------------------
    severity_counts: dict[str, int] = {
        ConvergenceSeverity.SAFE.value: 0,
        ConvergenceSeverity.DEGRADED.value: 0,
        ConvergenceSeverity.INCONSISTENT.value: 0,
    }
    for sev in severities:
        severity_counts[sev.value] += 1

    return ConvergenceSummary(
        severity_counts=severity_counts,
        targets=tuple(targets),
        total_targets=len(targets),
        worst_severity=_worst_severity(severities),
        warnings=tuple(global_warnings),
        orphan_count=None,
        evidence_bundle_ref=None,
    )
