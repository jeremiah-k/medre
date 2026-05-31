"""Pure recovery evidence builders.

:func:`build_startup_recovery_ledger` and :func:`build_recovery_summary`
are pure functions over outbox item snapshots.  No I/O, no storage
access, no state mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ._helpers import _get, _parse_as_utc, _to_str
from .classification import (
    CLASS_IMMEDIATELY_CLAIMABLE,
    CLASS_INCONSISTENT,
    CLASS_ORPHANED,
    CLASS_RETRY_ELIGIBLE,
    CLASS_STALE,
    CLASS_TERMINAL,
    classify_startup_reclamation,
)
from .models import (
    RecoveryOwnershipAction,
    RecoveryOwnershipStatus,
    RecoverySummary,
    StartupRecoveryLedger,
)
from .recovery_source import RecoverySource

__all__ = ["build_startup_recovery_ledger", "build_recovery_summary"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Clock for deterministic ledger timestamps."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Recovery source inference
# ---------------------------------------------------------------------------


def _infer_recovery_source(
    *,
    startup_timestamp: str | None = None,
    recovery_source: str | None = None,
) -> str:
    """Infer the recovery source for a claimable outbox item.

    Resolution order:

    1. If an explicit ``recovery_source`` is provided, use it directly.
    2. If ``startup_timestamp`` is present, the source is
       ``STARTUP_RECOVERY``.
    3. Otherwise the source defaults to ``SNAPSHOT_DIAGNOSTICS``
       (diagnostic classification from stored snapshots — no runtime
       startup or retry worker involved).

    ``RETRY_WORKER_RECOVERY`` is only used when the caller explicitly
    provides it.  ``REPLAY_EXECUTION`` is reserved for future use and
    not currently produced by any code path.
    """
    if recovery_source is not None:
        return recovery_source

    if startup_timestamp is not None:
        return str(RecoverySource.STARTUP_RECOVERY)

    return str(RecoverySource.SNAPSHOT_DIAGNOSTICS)


# ---------------------------------------------------------------------------
# Classification → ownership action mapping
# ---------------------------------------------------------------------------

_CLASSIFICATION_TO_OWNERSHIP: dict[str, str] = {
    CLASS_IMMEDIATELY_CLAIMABLE: str(RecoveryOwnershipStatus.RECOVERABLE),
    CLASS_RETRY_ELIGIBLE: str(RecoveryOwnershipStatus.SKIPPED),
    CLASS_STALE: str(RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY),
    CLASS_ORPHANED: str(RecoveryOwnershipStatus.UNRECOVERABLE),
    CLASS_TERMINAL: str(RecoveryOwnershipStatus.UNRECOVERABLE),
    CLASS_INCONSISTENT: str(RecoveryOwnershipStatus.UNRECOVERABLE),
}

# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

_DEFAULT_STALE_QUEUED_GRACE: timedelta = timedelta(minutes=5)


def build_startup_recovery_ledger(
    outbox_items: Iterable[Any] = (),
    *,
    startup_timestamp: str | None = None,
    recovery_run_id: str | None = None,
    recovery_source: str | None = None,
    now_fn: Callable[[], str] | None = None,
    known_event_ids: set[str] | frozenset[str] | None = None,
    stale_queued_grace: timedelta | None = None,
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
        UUID identifying this recovery cycle.  ``None`` when no
        runtime context exists (per-event snapshot diagnostics).
    recovery_source:
        Explicit recovery source override.  When provided, all actions
        carry this source regardless of ``startup_timestamp``.  When
        ``None``, the builder infers the source: ``startup_recovery``
        if ``startup_timestamp`` is present, otherwise
        ``snapshot_diagnostics``.  ``retry_worker_recovery`` is only
        used when the caller explicitly provides it.
    now_fn:
        Injectable clock for deterministic testing.  Returns an
        ISO-8601 string.
    known_event_ids:
        Known event IDs for orphan detection.  ``None`` skips orphan
        checks; an empty set flags all non-terminal items.
    stale_queued_grace:
        Grace period before a ``queued`` item with ``updated_at`` is
        considered stale.  Defaults to 5 minutes when ``None``.

    Returns
    -------
    StartupRecoveryLedger
        Frozen, append-only recovery ledger with deterministically
        ordered actions by ``(outbox_id, timestamp)``.
    """
    _now_iso_fn = now_fn or _now_iso
    _run_id = recovery_run_id  # None is valid for per-event snapshot diagnostics
    _source_override = recovery_source  # Explicit caller override; None → infer
    _grace = (
        stale_queued_grace
        if stale_queued_grace is not None
        else _DEFAULT_STALE_QUEUED_GRACE
    )
    generated_at = _now_iso_fn()

    # Derive a datetime for classification timestamp comparisons.
    _now_dt = _parse_as_utc(generated_at)

    actions: list[RecoveryOwnershipAction] = []

    for item in outbox_items:
        status = _to_str(_get(item, "status")).lower()
        outbox_id = _to_str(_get(item, "outbox_id"))
        event_id = _to_str(_get(item, "event_id"))
        delivery_plan_id = _to_str(_get(item, "delivery_plan_id"))
        worker_id = _to_str(_get(item, "worker_id")) or None
        updated_at = _to_str(_get(item, "updated_at"))

        classification, reason = classify_startup_reclamation(
            item,
            known_event_ids=known_event_ids,
            now=_now_dt,
            stale_queued_grace=_grace,
        )

        ownership_action = _CLASSIFICATION_TO_OWNERSHIP.get(
            classification, str(RecoveryOwnershipStatus.UNRECOVERABLE)
        )

        _action_source = _infer_recovery_source(
            startup_timestamp=startup_timestamp,
            recovery_source=_source_override,
        )

        action_timestamp = updated_at or generated_at

        actions.append(
            RecoveryOwnershipAction(
                recovery_run_id=_run_id,
                startup_timestamp=startup_timestamp,
                outbox_id=outbox_id,
                prior_status=status,
                # recovered_status reflects the pre-recovery state —
                # recovery classification is diagnostic and does not
                # mutate outbox status.
                recovered_status=status,
                ownership_action=ownership_action,
                reason=reason,
                worker_identity=worker_id,
                recovery_source=_action_source,
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
        elif oa == str(RecoveryOwnershipStatus.SKIPPED):
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
