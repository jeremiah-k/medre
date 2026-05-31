"""Pure recovery evidence builders.

:func:`build_startup_recovery_ledger` and :func:`build_recovery_summary`
are pure functions over outbox item snapshots.  No I/O, no storage
access, no state mutation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ._classification import (
    CLASS_IMMEDIATELY_CLAIMABLE,
    CLASS_INCONSISTENT,
    CLASS_ORPHANED,
    CLASS_RETRY_ELIGIBLE,
    CLASS_STALE,
    CLASS_TERMINAL,
    classify_startup_reclamation,
)
from ._models import (
    RecoveryOwnershipAction,
    RecoveryOwnershipStatus,
    RecoverySummary,
    StartupRecoveryLedger,
)
from ._recovery_source import RecoverySource

__all__ = ["build_startup_recovery_ledger", "build_recovery_summary"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Duck-typed field access."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_str(val: Any) -> str:
    """Coerce to string safely."""
    if val is None:
        return ""
    return str(val)


def _now_iso() -> str:
    """Clock for deterministic ledger timestamps."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Recovery source inference
# ---------------------------------------------------------------------------


def _infer_recovery_source(
    outbox_item: Any,
    *,
    worker_id: str | None,
    startup_timestamp: str | None = None,
    now_fn: Callable[[], str] | None = None,
) -> str:
    """Infer the recovery source for a claimable outbox item.

    Priority:
    1. ``startup_timestamp`` within the last 60s → ``STARTUP_RECOVERY``
    2. ``worker_id`` set and not in the last startup window → ``RETRY_WORKER_RECOVERY``
    3. Default → ``RETRY_WORKER_RECOVERY``

    **Note:** ``REPLAY_EXECUTION`` is inferred later by the collector
    when receipt-level ``source="replay"`` evidence exists for the same
    ``delivery_plan_id``.  This builder cannot determine replay without
    receipt data.
    """
    if startup_timestamp is not None:
        try:
            startup_dt = datetime.fromisoformat(startup_timestamp)
            # Use injectable now_fn to produce a datetime for comparison.
            if now_fn is not None:
                now_str = now_fn()
                now = datetime.fromisoformat(now_str)
            else:
                now = datetime.now(timezone.utc)
            if (now - startup_dt).total_seconds() <= 60:
                return str(RecoverySource.STARTUP_RECOVERY)
        except (ValueError, TypeError):
            pass

    if worker_id:
        return str(RecoverySource.RETRY_WORKER_RECOVERY)

    return str(RecoverySource.RETRY_WORKER_RECOVERY)


# ---------------------------------------------------------------------------
# Classification → ownership action mapping
# ---------------------------------------------------------------------------

_CLASSIFICATION_TO_OWNERSHIP: dict[str, str] = {
    CLASS_IMMEDIATELY_CLAIMABLE: str(RecoveryOwnershipStatus.RECOVERABLE),
    CLASS_RETRY_ELIGIBLE: str(RecoveryOwnershipStatus.RECOVERABLE),
    CLASS_STALE: str(RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY),
    CLASS_ORPHANED: str(RecoveryOwnershipStatus.UNRECOVERABLE),
    CLASS_TERMINAL: str(RecoveryOwnershipStatus.UNRECOVERABLE),
    CLASS_INCONSISTENT: str(RecoveryOwnershipStatus.UNRECOVERABLE),
}

# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_startup_recovery_ledger(
    outbox_items: Iterable[Any] = (),
    *,
    startup_timestamp: str | None = None,
    recovery_run_id: str | None = None,
    now_fn: Callable[[], str] | None = None,
    known_event_ids: set[str] | frozenset[str] | None = None,
) -> StartupRecoveryLedger:
    """Build a deterministic startup recovery ledger from outbox snapshots.

    Parameters
    ----------
    outbox_items:
        Outbox items to classify.  Duck-typed — accepts
        :class:`~medre.core.storage.backend.DeliveryOutboxItem` or
        plain ``dict`` values.
    startup_timestamp:
        ISO-8601 timestamp of the runtime startup for source inference.
        ``None`` when unavailable.
    recovery_run_id:
        UUID identifying this recovery cycle.  Auto-generated when
        ``None``.
    now_fn:
        Injectable clock for deterministic testing.
    known_event_ids:
        Known event IDs for orphan detection.  ``None`` skips orphan
        checks; an empty set flags all non-terminal items.

    Returns
    -------
    StartupRecoveryLedger
        Frozen, append-only recovery ledger with deterministically
        ordered actions by ``(outbox_id, timestamp)``.
    """
    _now = now_fn or _now_iso
    _run_id = recovery_run_id if recovery_run_id is not None else uuid.uuid4().hex
    generated_at = _now()

    actions: list[RecoveryOwnershipAction] = []

    for item in outbox_items:
        status = _to_str(_get(item, "status")).lower()
        outbox_id = _to_str(_get(item, "outbox_id"))
        event_id = _to_str(_get(item, "event_id"))
        delivery_plan_id = _to_str(_get(item, "delivery_plan_id"))
        worker_id = _to_str(_get(item, "worker_id")) or None
        updated_at = _to_str(_get(item, "updated_at"))
        _to_str(_get(item, "failure_kind"))

        classification, reason = classify_startup_reclamation(
            item,
            startup_timestamp=startup_timestamp,
            known_event_ids=known_event_ids,
        )

        ownership_action = _CLASSIFICATION_TO_OWNERSHIP.get(
            classification, str(RecoveryOwnershipStatus.UNRECOVERABLE)
        )

        recovery_source = _infer_recovery_source(
            item,
            worker_id=worker_id,
            startup_timestamp=startup_timestamp,
            now_fn=now_fn,
        )

        # Handle skipped (retry_eligible) as a distinct group in the summary
        # but map ownership to UNRECOVERABLE since it's intentionally deferred.
        if classification == CLASS_RETRY_ELIGIBLE:
            ownership_action = "skipped"

        action_timestamp = updated_at or generated_at

        actions.append(
            RecoveryOwnershipAction(
                recovery_run_id=_run_id,
                startup_timestamp=startup_timestamp,
                outbox_id=outbox_id,
                prior_status=status,
                recovered_status=status,
                ownership_action=ownership_action,
                reason=reason,
                worker_identity=worker_id,
                recovery_source=recovery_source,
                timestamp=action_timestamp,
                delivery_plan_id=delivery_plan_id,
                event_id=event_id,
            )
        )

    # Sort deterministically by (outbox_id, timestamp).
    actions.sort(key=lambda a: (a.outbox_id, a.timestamp))

    return StartupRecoveryLedger(
        recovery_run_id=_run_id,
        startup_timestamp=startup_timestamp,
        actions=tuple(actions),
        generated_at=generated_at,
    )


def build_recovery_summary(
    ledger: StartupRecoveryLedger,
) -> RecoverySummary:
    """Build a deterministic recovery summary from a ledger.

    Aggregates counts across all recovery actions and validates the
    consistency invariant: ``total_items == recoverable + claimed
    + reclaimed + skipped + abandoned + unrecoverable``.

    Parameters
    ----------
    ledger:
        The startup recovery ledger to summarise.

    Returns
    -------
    RecoverySummary
        Frozen summary with consistency validation.
    """
    recoverable = 0
    claimed = 0
    reclaimed = 0
    skipped = 0
    abandoned = 0
    unrecoverable = 0
    by_source: dict[str, int] = {}

    for a in ledger.actions:
        src = a.recovery_source
        by_source[src] = by_source.get(src, 0) + 1

        oa = a.ownership_action
        if oa == str(RecoveryOwnershipStatus.RECOVERABLE):
            recoverable += 1
        elif oa == str(RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY):
            claimed += 1
        elif oa == str(RecoveryOwnershipStatus.RECLAIMED):
            reclaimed += 1
        elif oa == "skipped":
            skipped += 1
        elif oa == str(RecoveryOwnershipStatus.ABANDONED):
            abandoned += 1
        elif oa == str(RecoveryOwnershipStatus.UNRECOVERABLE):
            unrecoverable += 1
        else:
            unrecoverable += 1

    total = len(ledger.actions)
    computed_sum = (
        recoverable + claimed + reclaimed + skipped + abandoned + unrecoverable
    )

    return RecoverySummary(
        recoverable_items=recoverable,
        claimed_items=claimed,
        reclaimed_items=reclaimed,
        skipped_items=skipped,
        abandoned_items=abandoned,
        unrecoverable_items=unrecoverable,
        total_items=total,
        consistency_valid=(total == computed_sum),
        by_source=dict(sorted(by_source.items())),
        recovery_run_id=ledger.recovery_run_id,
    )
