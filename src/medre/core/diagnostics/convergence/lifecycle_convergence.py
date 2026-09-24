"""Pure delivery lifecycle convergence diagnostics — orchestration entrypoint.

Detects inconsistencies between outbox item states and delivery receipt
states. All functions are pure and read-only — no I/O, no state mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ...delivery_authority import DeliveryAuthorityResolver
from .helpers import _ensure_aware
from .lifecycle_checks import (
    _check_attempt_count_regression,
    _check_receipt_sequence_gap,
    _check_retry_wait_outboxes,
    _check_retryable_without_metadata,
    _check_stalled_delivery_plans,
    _check_target_mismatches,
)
from .types import OrphanFinding

__all__ = [
    "build_lifecycle_convergence_findings",
]


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

    now = _ensure_aware(now_fn())

    # -- Resolve each delivery once -----------------------------------------
    authority = DeliveryAuthorityResolver(receipt_list, outbox_list)
    snapshots = authority.ordered_snapshots()

    findings.extend(_check_target_mismatches(snapshots))
    findings.extend(_check_retry_wait_outboxes(outbox_list, now))
    findings.extend(_check_retryable_without_metadata(snapshots))
    findings.extend(
        _check_stalled_delivery_plans(outbox_list, now, stall_threshold_seconds)
    )
    findings.extend(_check_attempt_count_regression(snapshots))
    findings.extend(_check_receipt_sequence_gap(snapshots))

    findings.sort(key=lambda f: (f.kind, f.record_id))
    return findings
