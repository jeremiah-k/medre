"""Recovery-specific convergence findings.

Extends convergence diagnostics with visibility into recovery
ownership accountability: what was recovered, whether it progressed,
and whether repeated recoveries indicate systemic issues.

All functions are pure and read-only — no I/O, no state mutation.
"""

from __future__ import annotations

from typing import Any, Iterable

from .helpers import (
    _TERMINAL_OUTBOX,
    _TERMINAL_RECEIPT,
    _get,
    _latest_receipt_for_target,
    _target_key,
)
from .types import (
    KIND_RECLAIMED_THEN_ORPHANED,
    KIND_RECLAIMED_THEN_TERMINAL,
    KIND_RECOVERED_NOT_PROGRESSED,
    KIND_REPEATEDLY_RECLAIMED,
    OrphanFinding,
)

__all__ = ["build_recovery_convergence_findings"]

# ---------------------------------------------------------------------------
# Status sets (imported from helpers — canonical definitions)
# ---------------------------------------------------------------------------

# Outbox statuses that have a direct receipt-vocabulary equivalent.
# Only these mappings allow a valid cross-state-machine comparison.
# Outbox statuses like "pending", "retry_wait", "in_progress" have NO
# receipt equivalent — comparing them to a receipt status would be a
# vocabulary mismatch, producing false positives.
_OUTBOX_TO_RECEIPT_EQUIV: dict[str, str] = {
    "queued": "queued",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_recovery_convergence_findings(
    outbox_items: Iterable[Any] = (),
    receipts: Iterable[Any] = (),
    recovery_ledger: Any | None = None,
    known_event_ids: set[str] | frozenset[str] | None = None,
) -> list[OrphanFinding]:
    """Build recovery-specific convergence findings.

    Parameters
    ----------
    outbox_items:
        Duck-typed outbox item records.
    receipts:
        Duck-typed receipt records.
    recovery_ledger:
        :class:`~medre.core.recovery.models.StartupRecoveryLedger`
        or a dict with an ``actions`` key containing recovery actions.
        ``None`` skips recovery-accountability findings.
    known_event_ids:
        Known event IDs for the orphaned-after-reclaim detection.
        ``None`` skips the check.

    Returns
    -------
    list[OrphanFinding]
        Deterministically sorted by ``(kind, record_id)``.
    """
    findings: list[OrphanFinding] = []

    # Materialize generators once — outbox_items may be a one-shot generator.
    outbox_list = list(outbox_items)

    # Normalized recovery actions — populated when recovery_ledger is present.
    actions_list: list[Any] = []

    # Index outbox items by target key and by outbox_id.
    outbox_by_target: dict[tuple[str, str, str | None], list[Any]] = {}
    outbox_by_id: dict[str, Any] = {}
    for item in outbox_list:
        key = _target_key(item)
        outbox_by_target.setdefault(key, []).append(item)
        oid = _get(item, "outbox_id", "")
        if oid:
            outbox_by_id[str(oid)] = item

    # Index receipts by target key.
    receipts_by_target: dict[tuple[str, str, str | None], list[Any]] = {}
    for rec in receipts:
        key = _target_key(rec)
        receipts_by_target.setdefault(key, []).append(rec)

    # -- Recovered but not progressed ---------------------------------------
    # An outbox item was reclaimed (its recovery action says so) but
    # its latest receipt hasn't changed status since the previous
    # shutdown.  This means recovery claimed ownership but the item
    # hasn't actually progressed.
    if recovery_ledger is not None:
        # Extract actions from ledger (duck-typed), normalize once.
        # Defensive: ``actions`` may be ``None`` on dict-based ledgers where
        # the key exists but holds a null value.
        _raw_actions = _get(recovery_ledger, "actions", ()) or ()
        if isinstance(_raw_actions, (tuple, list)):
            actions_list = list(_raw_actions)
        else:
            actions_list = list(_raw_actions)

        # Track recovery_run_ids to detect repeatedly reclaimed.
        action_run_ids: dict[str, list[str]] = {}  # outbox_id → list[recovery_run_id]

        for action in actions_list:
            outbox_id = str(_get(action, "outbox_id", ""))
            if not outbox_id:
                continue
            raw_run_id = _get(action, "recovery_run_id")
            run_id = str(raw_run_id) if raw_run_id is not None else ""
            ownership_action = str(_get(action, "ownership_action", ""))
            prior_status = str(_get(action, "prior_status", ""))

            if run_id and ownership_action in (
                "recoverable",
                "claimed_for_recovery",
                "reclaimed",
            ):
                action_run_ids.setdefault(outbox_id, []).append(run_id)

            # recovered_not_progressed: item was reclaimed/recoverable
            # but its latest receipt is still the same (non-terminal, non-progressed).
            # Only compare when the outbox prior_status has a valid receipt
            # equivalent — comparing outbox "pending" to receipt "pending"
            # would be a vocabulary-mismatch false positive.
            if ownership_action in ("recoverable", "reclaimed", "claimed_for_recovery"):
                receipt_equiv = _OUTBOX_TO_RECEIPT_EQUIV.get(prior_status)
                if receipt_equiv is not None:
                    item = outbox_by_id.get(outbox_id)
                    if item is not None:
                        target_key = _target_key(item)
                        latest = _latest_receipt_for_target(
                            receipts_by_target, target_key
                        )
                        if latest is not None:
                            latest_status = str(_get(latest, "status", ""))
                            if (
                                latest_status == receipt_equiv
                                and latest_status not in _TERMINAL_RECEIPT
                            ):
                                rec_id = (
                                    outbox_id
                                    or str(_get(latest, "receipt_id", ""))
                                    or None
                                )
                                if rec_id is None:
                                    continue
                                findings.append(
                                    OrphanFinding(
                                        kind=KIND_RECOVERED_NOT_PROGRESSED,
                                        severity="degraded",
                                        record_id=rec_id,
                                        record_type="outbox",
                                        details=(
                                            f"Outbox item {outbox_id!r} ({prior_status}) "
                                            f"was recovered but latest receipt is still "
                                            f"{latest_status!r} — no progress since shutdown"
                                        ),
                                        extra={
                                            "outbox_id": outbox_id,
                                            "prior_status": prior_status,
                                            "latest_receipt_status": latest_status,
                                            "recovery_run_id": run_id,
                                        },
                                    )
                                )

        # -- Repeatedly reclaimed ------------------------------------------
        # Same outbox item appears in multiple DISTINCT recovery runs.
        for oid, run_ids in action_run_ids.items():
            distinct_ids = sorted(set(run_ids))
            if len(distinct_ids) >= 2:
                item = outbox_by_id.get(oid)
                status = str(_get(item, "status", "")) if item else "unknown"
                findings.append(
                    OrphanFinding(
                        kind=KIND_REPEATEDLY_RECLAIMED,
                        severity="degraded",
                        record_id=oid,
                        record_type="outbox",
                        details=(
                            f"Outbox item {oid!r} was reclaimed across "
                            f"{len(distinct_ids)} distinct recovery runs "
                            f"{distinct_ids} — repeated recovery without progress"
                        ),
                        extra={
                            "outbox_id": oid,
                            "status": status,
                            "recovery_run_ids": distinct_ids,
                            "recovery_count": len(distinct_ids),
                        },
                    )
                )

        # -- Reclaimed then terminal ------------------------------------------
        # Outbox item is terminal but latest receipt is non-terminal, AND the
        # item was actually present in the recovery ledger with a
        # recovered/reclaimed action.  Without this gate, any terminal outbox
        # item with a non-terminal receipt would fire regardless of recovery.
        # Build set of outbox_ids that had recovery actions.
        recovered_outbox_ids_terminal: set[str] = set()
        for action in actions_list:
            oa = str(_get(action, "ownership_action", ""))
            if oa in ("recoverable", "reclaimed", "claimed_for_recovery"):
                oid_t = str(_get(action, "outbox_id", ""))
                if oid_t:
                    recovered_outbox_ids_terminal.add(oid_t)

        for item in outbox_list:
            status = str(_get(item, "status", "")).lower()
            if status in _TERMINAL_OUTBOX:
                oid = str(_get(item, "outbox_id", ""))
                if oid not in recovered_outbox_ids_terminal:
                    continue
                target_key = _target_key(item)
                latest = _latest_receipt_for_target(receipts_by_target, target_key)
                if latest is not None:
                    latest_status = str(_get(latest, "status", "")).lower()
                    if latest_status not in _TERMINAL_RECEIPT:
                        rec_id = oid or str(_get(latest, "receipt_id", "")) or None
                        if rec_id is None:
                            continue
                        findings.append(
                            OrphanFinding(
                                kind=KIND_RECLAIMED_THEN_TERMINAL,
                                severity="inconsistent",
                                record_id=rec_id,
                                record_type="outbox",
                                details=(
                                    f"Outbox item {oid!r} is terminal ({status}) but "
                                    f"latest receipt is non-terminal ({latest_status}) — "
                                    f"terminal outbox with non-terminal receipt"
                                ),
                                extra={
                                    "outbox_id": oid,
                                    "outbox_status": status,
                                    "latest_receipt_status": latest_status,
                                    "delivery_plan_id": str(
                                        _get(item, "delivery_plan_id", "")
                                    ),
                                },
                            )
                        )

    # -- Reclaimed then orphaned ------------------------------------------
    # Non-terminal outbox item whose event_id is absent from known_event_ids
    # AFTER appearing in a recovery ledger (meaning it was recovered but
    # the event has been deleted or never existed).
    if known_event_ids is not None and recovery_ledger is not None:
        # Reuse actions_list normalized in the recovery_ledger block above.
        actions_l = actions_list

        recovered_outbox_ids: set[str] = set()
        for action in actions_l:
            oa = str(_get(action, "ownership_action", ""))
            if oa in ("recoverable", "reclaimed", "claimed_for_recovery"):
                oid = str(_get(action, "outbox_id", ""))
                if oid:
                    recovered_outbox_ids.add(oid)

        for item in outbox_list:
            oid = str(_get(item, "outbox_id", ""))
            if oid in recovered_outbox_ids:
                event_id = str(_get(item, "event_id", ""))
                if event_id not in known_event_ids:
                    status = str(_get(item, "status", ""))
                    if status.lower() not in _TERMINAL_OUTBOX:
                        findings.append(
                            OrphanFinding(
                                kind=KIND_RECLAIMED_THEN_ORPHANED,
                                severity="inconsistent",
                                record_id=oid,
                                record_type="outbox",
                                details=(
                                    f"Outbox item {oid!r} was recovered but its "
                                    f"event {event_id!r} is absent from the known "
                                    f"event catalogue — reclaimed then orphaned"
                                ),
                                extra={
                                    "outbox_id": oid,
                                    "event_id": event_id,
                                    "status": status,
                                },
                            )
                        )

    # Sort deterministically by (kind, record_id).
    findings.sort(key=lambda f: (f.kind, f.record_id))

    return findings
