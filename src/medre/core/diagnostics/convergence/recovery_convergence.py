"""Recovery-specific convergence findings.

Extends convergence diagnostics with visibility into recovery
ownership accountability: what was recovered, whether it progressed,
and whether repeated recoveries indicate systemic issues.

All functions are pure and read-only — no I/O, no state mutation.
"""

from __future__ import annotations

from typing import Any, Iterable

from .helpers import _get, _latest_receipt_for_target, _target_key
from .types import (
    KIND_RECLAIMED_THEN_ORPHANED,
    KIND_RECLAIMED_THEN_TERMINAL,
    KIND_RECOVERED_NOT_PROGRESSED,
    KIND_REPEATEDLY_RECLAIMED,
    OrphanFinding,
)

__all__ = ["build_recovery_convergence_findings"]

# ---------------------------------------------------------------------------
# Status sets
# ---------------------------------------------------------------------------

_TERMINAL_OUTBOX: frozenset[str] = frozenset(
    {"sent", "dead_lettered", "cancelled", "abandoned"}
)

_TERMINAL_RECEIPT: frozenset[str] = frozenset({"sent", "dead_lettered", "suppressed"})


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
        :class:`~medre.core.recovery._models.StartupRecoveryLedger`
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

    # Index outbox items by target key and by outbox_id.
    outbox_by_target: dict[tuple[str, str, str | None], list[Any]] = {}
    outbox_by_id: dict[str, Any] = {}
    for item in outbox_items:
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
        # Extract actions from ledger (duck-typed).
        actions = _get(recovery_ledger, "actions", ())
        if isinstance(actions, tuple):
            actions_list: list[Any] = list(actions)
        elif isinstance(actions, list):
            actions_list = actions
        else:
            actions_list = list(actions)

        # Track recovery_run_ids to detect repeatedly reclaimed.
        seen_recovery_runs: set[str] = set()
        action_run_ids: dict[str, list[str]] = {}  # outbox_id → list[recovery_run_id]

        for action in actions_list:
            outbox_id = str(_get(action, "outbox_id", ""))
            run_id = str(_get(action, "recovery_run_id", ""))
            ownership_action = str(_get(action, "ownership_action", ""))
            prior_status = str(_get(action, "prior_status", ""))

            if run_id:
                seen_recovery_runs.add(run_id)

            if outbox_id and run_id:
                action_run_ids.setdefault(outbox_id, []).append(run_id)

            # recovered_not_progressed: item was reclaimed/recoverable
            # but its latest receipt is still the same (non-terminal, non-progressed).
            if ownership_action in ("recoverable", "reclaimed", "claimed_for_recovery"):
                item = outbox_by_id.get(outbox_id)
                if item is not None:
                    target_key = _target_key(item)
                    latest = _latest_receipt_for_target(receipts_by_target, target_key)
                    if latest is not None:
                        latest_status = str(_get(latest, "status", ""))
                        if (
                            latest_status == prior_status
                            and latest_status not in _TERMINAL_RECEIPT
                        ):
                            findings.append(
                                OrphanFinding(
                                    kind=KIND_RECOVERED_NOT_PROGRESSED,
                                    severity="degraded",
                                    record_id=outbox_id or latest_status,
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
        # Same outbox item appears in multiple recovery runs.
        for oid, run_ids in action_run_ids.items():
            if len(run_ids) >= 2:
                item = outbox_by_id.get(oid)
                status = str(_get(item, "status", "")) if item else "unknown"
                findings.append(
                    OrphanFinding(
                        kind=KIND_REPEATEDLY_RECLAIMED,
                        severity="degraded",
                        record_id=oid,
                        record_type="outbox",
                        details=(
                            f"Outbox item {oid!r} was reclaimed {len(run_ids)} times "
                            f"across recovery runs {sorted(run_ids)} — repeated "
                            f"recovery without progress"
                        ),
                        extra={
                            "outbox_id": oid,
                            "status": status,
                            "recovery_run_ids": sorted(run_ids),
                            "recovery_count": len(run_ids),
                        },
                    )
                )

    # -- Reclaimed then terminal ------------------------------------------
    # Outbox item is terminal but latest receipt is non-terminal.
    for item in outbox_items:
        status = str(_get(item, "status", ""))
        if status in _TERMINAL_OUTBOX:
            target_key = _target_key(item)
            latest = _latest_receipt_for_target(receipts_by_target, target_key)
            if latest is not None:
                latest_status = str(_get(latest, "status", ""))
                if latest_status not in _TERMINAL_RECEIPT:
                    oid = str(_get(item, "outbox_id", ""))
                    findings.append(
                        OrphanFinding(
                            kind=KIND_RECLAIMED_THEN_TERMINAL,
                            severity="inconsistent",
                            record_id=oid or latest_status,
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
        actions = _get(recovery_ledger, "actions", ())
        if isinstance(actions, tuple):
            actions_l = list(actions)
        elif isinstance(actions, list):
            actions_l = actions
        else:
            actions_l = list(actions)

        recovered_outbox_ids: set[str] = set()
        for action in actions_l:
            oa = str(_get(action, "ownership_action", ""))
            if oa in ("recoverable", "reclaimed", "claimed_for_recovery"):
                oid = str(_get(action, "outbox_id", ""))
                if oid:
                    recovered_outbox_ids.add(oid)

        for item in outbox_items:
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
