"""Pure retry/outbox accountability evidence helpers.

Provides JSON-safe summary models and a pure builder function that
derives retry/outbox accountability evidence from existing receipts
and outbox records — without storage schema changes or runtime I/O.

Design constraints
~~~~~~~~~~~~~~~~~~
- **No I/O, no state mutation.** All public functions are pure.
- **JSON-safe values.** Every field is ``str | int | float | bool | None |
  list | dict``; ``datetime`` values are converted to ISO-8601 strings.
- **No storage imports.** Accepts plain objects or dict-like records via
  duck-typed field access.
- **Deterministic ordering.** Items are sorted by
  ``(event_id, delivery_plan_id, target_adapter, target_channel,
  attempt_number)``.

Public symbols
--------------
* :class:`RetryOutboxItemSummary` — per-item accountability evidence.
* :class:`RetryOutboxSummary` — aggregate accountability summary.
* :func:`build_retry_outbox_summary` — pure builder from receipts/outbox/retry_state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from medre.core.engine.pipeline.delivery_state import (
    NON_TERMINAL_OUTBOX_STATUSES,
)
from medre.core.engine.pipeline.delivery_state import (
    OUTBOX_STATUSES as _OUTBOX_STATUSES,
)
from medre.core.engine.pipeline.delivery_state import (
    RECEIPT_STATUSES as _RECEIPT_STATUSES,
)
from medre.core.engine.pipeline.delivery_state import (
    TERMINAL_OUTBOX_STATUSES,
)
from medre.core.evidence.failure_taxonomy import (
    derive_failure_kind_detail,
    resolve_taxon,
    taxon_category,
)

__all__ = [
    "RetryOutboxItemSummary",
    "RetryOutboxSummary",
    "build_retry_outbox_summary",
]


# ---------------------------------------------------------------------------
# Duck-typed field access
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Retrieve *name* from an object or dict, falling back to *default*.

    NOTE: This is a parallel implementation of
    ``medre.core.diagnostics.convergence.helpers._get``.  The two
    packages are architecturally separate — ``evidence`` must not
    import from ``diagnostics.convergence`` — so the duplication is
    intentional and must be kept in sync manually.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_iso(value: Any) -> str | None:
    """Convert a value to an ISO-8601 string or ``None``.

    NOTE: Parallel implementation of
    ``medre.core.diagnostics.convergence.helpers._to_iso`` — see
    ``_get`` docstring for rationale.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Retry-state label derivation
# ---------------------------------------------------------------------------

# NOTE: Canonical status vocab constants live in
# medre.core.engine.pipeline.delivery_state.  Re-exported here under
# the internal names used throughout this module.  Drift is detected by
# tests/test_evidence_coherence_contract.py.
_TERMINAL_OUTBOX = TERMINAL_OUTBOX_STATUSES
_NON_TERMINAL_OUTBOX = NON_TERMINAL_OUTBOX_STATUSES
# Suppressed and failed are receipt-only statuses (no outbox equivalent).
_RECEIPT_ONLY_STATUSES = _RECEIPT_STATUSES - _OUTBOX_STATUSES


def _retry_state_label(status: str, *, source: str = "outbox") -> str:
    """Derive a human-readable retry-state label from a raw status.

    **These return values are derived display labels, not authoritative
    lifecycle states.**  They are computed from persisted outbox/receipt
    status strings for evidence reporting purposes only.  Pipeline state
    transitions must use the canonical constants and helpers in
    :mod:`~medre.core.engine.pipeline.delivery_state`.

    Parameters
    ----------
    status:
        Raw status string (outbox or receipt).
    source:
        ``"outbox"`` or ``"receipt"`` to disambiguate context.
    """
    if source == "receipt":
        if status == "suppressed":
            return "suppressed"
        if status == "failed":
            return "failed"
        if status == "dead_lettered":
            return "dead_lettered"
        if status == "queued":
            return "queued"
        if status == "sent":
            return "sent"
        return status
    # outbox source
    if status == "retry_wait":
        return "retrying"
    return status


# ---------------------------------------------------------------------------
# Reason-pending derivation
# ---------------------------------------------------------------------------


def _derive_reason_pending(
    status: str,
    next_attempt_at: str | None,
    worker_id: str | None,
    failure_kind: str | None,
    *,
    source: str = "outbox",
    delivery_plan_id: str | None = None,
    receipt_id: str | None = None,
) -> str | None:
    """Derive a human-readable explanation for why work is still pending.

    Returns ``None`` for terminal states that don't need an explanation.

    Parameters
    ----------
    status:
        Raw status string (outbox or receipt).
    next_attempt_at:
        ISO-8601 next-attempt timestamp, or ``None``.
    worker_id:
        Worker that claimed the item, or ``None``.
    failure_kind:
        Classified failure kind string, or ``None``.
    source:
        ``"outbox"`` or ``"receipt"`` to disambiguate context.
    delivery_plan_id:
        The delivery plan correlation key, or ``None``/empty when absent.
        Used to flag uncorrelated queued items.
    receipt_id:
        The receipt identifier, or ``None`` when no receipt linkage exists.
        Used to flag uncorrelated queued items.
    """
    if source == "receipt":
        if status == "suppressed":
            return "Suppressed, not retryable"
        if status == "failed":
            if failure_kind:
                return f"Failed ({failure_kind}), may have outbox item pending"
            return "Failed"
        return None

    # outbox source
    if status == "retry_wait":
        if next_attempt_at:
            return f"Scheduled retry at {next_attempt_at}"
        return "Awaiting retry scheduling"
    if status == "pending":
        return "Awaiting worker claim"
    if status == "in_progress":
        if worker_id:
            return f"Claimed by worker {worker_id}"
        return "In progress"
    if status == "queued":
        has_plan_id = bool(delivery_plan_id)
        has_receipt = bool(receipt_id)
        if not has_plan_id and not has_receipt:
            return (
                "Queued, uncorrelated (no delivery_plan_id, no receipt linkage) "
                "— awaiting stale-grace reclaim or adapter callback correlation"
            )
        if not has_plan_id:
            return (
                "Queued, uncorrelated (no delivery_plan_id) "
                "— awaiting stale-grace reclaim or adapter callback correlation"
            )
        if not has_receipt:
            return (
                "Queued, uncorrelated (no receipt linkage) "
                "— awaiting stale-grace reclaim or adapter callback correlation"
            )
        return "Queued in adapter-local queue"
    # terminal — no reason needed
    return None


# ---------------------------------------------------------------------------
# Item summary builders
# ---------------------------------------------------------------------------


def _build_outbox_item_summary(item: Any) -> RetryOutboxItemSummary:
    """Build a :class:`RetryOutboxItemSummary` from an outbox item."""
    status = _get(item, "status") or "pending"
    failure_kind = _get(item, "failure_kind")
    error_summary = _get(item, "error_summary")
    next_attempt_at = _to_iso(_get(item, "next_attempt_at"))
    worker_id = _get(item, "worker_id")
    target_channel = _get(item, "target_channel")

    # Failure taxonomy enrichment.
    taxon = resolve_taxon(
        failure_kind=failure_kind,
        error=error_summary,
        status=status,
    )
    taxon_str = taxon.value if taxon is not None else None
    category = taxon_category(taxon) if taxon is not None else None
    kind_detail = derive_failure_kind_detail(failure_kind, error_summary)

    return RetryOutboxItemSummary(
        outbox_id=_get(item, "outbox_id"),
        delivery_plan_id=_get(item, "delivery_plan_id") or "",
        event_id=_get(item, "event_id") or "",
        route_id=_get(item, "route_id"),
        target_adapter=_get(item, "target_adapter") or "",
        target_channel=target_channel,
        status=status,
        retry_state=_retry_state_label(status, source="outbox"),
        attempt_number=_get(item, "attempt_number"),
        next_attempt_at=next_attempt_at,
        next_retry_at=next_attempt_at,  # outbox uses next_attempt_at
        failure_kind=failure_kind,
        failure_taxon=taxon_str,
        failure_category=category,
        failure_kind_detail=kind_detail,
        parent_receipt_id=_get(item, "parent_receipt_id"),
        receipt_id=_get(item, "receipt_id"),
        reason_pending=_derive_reason_pending(
            status,
            next_attempt_at,
            worker_id,
            failure_kind,
            source="outbox",
            delivery_plan_id=_get(item, "delivery_plan_id"),
            receipt_id=_get(item, "receipt_id"),
        ),
    )


def _build_receipt_only_summary(receipt: Any) -> RetryOutboxItemSummary:
    """Build a :class:`RetryOutboxItemSummary` from a receipt without outbox."""
    status = _get(receipt, "status") or "queued"
    failure_kind = _get(receipt, "failure_kind")
    error = _get(receipt, "error")
    next_retry_at = _to_iso(_get(receipt, "next_retry_at"))

    taxon = resolve_taxon(
        failure_kind=failure_kind,
        error=error,
        status=status,
    )
    taxon_str = taxon.value if taxon is not None else None
    category = taxon_category(taxon) if taxon is not None else None
    kind_detail = derive_failure_kind_detail(failure_kind, error)

    return RetryOutboxItemSummary(
        outbox_id=None,
        delivery_plan_id=_get(receipt, "delivery_plan_id") or "",
        event_id=_get(receipt, "event_id") or "",
        route_id=_get(receipt, "route_id"),
        target_adapter=_get(receipt, "target_adapter") or "",
        target_channel=_get(receipt, "target_channel"),
        status=status,
        retry_state=_retry_state_label(status, source="receipt"),
        attempt_number=_get(receipt, "attempt_number"),
        next_attempt_at=None,
        next_retry_at=next_retry_at,
        failure_kind=failure_kind,
        failure_taxon=taxon_str,
        failure_category=category,
        failure_kind_detail=kind_detail,
        parent_receipt_id=_get(receipt, "parent_receipt_id"),
        receipt_id=_get(receipt, "receipt_id"),
        reason_pending=_derive_reason_pending(
            status,
            next_retry_at,
            worker_id=None,
            failure_kind=failure_kind,
            source="receipt",
            delivery_plan_id=_get(receipt, "delivery_plan_id"),
            receipt_id=_get(receipt, "receipt_id"),
        ),
    )


# ---------------------------------------------------------------------------
# Sort key for deterministic ordering
# ---------------------------------------------------------------------------


def _item_sort_key(s: RetryOutboxItemSummary) -> tuple:
    return (
        s.event_id or "",
        s.delivery_plan_id or "",
        s.target_adapter or "",
        s.target_channel or "",
        s.attempt_number or 0,
        s.outbox_id or "",
        s.receipt_id or "",
    )


# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryOutboxItemSummary:
    """Per-item accountability evidence for a single delivery work item.

    All fields are JSON-safe (no ``datetime``, no enum values).
    """

    outbox_id: str | None
    delivery_plan_id: str
    event_id: str
    route_id: str | None
    target_adapter: str
    target_channel: str | None
    status: str
    retry_state: str | None
    attempt_number: int | None
    next_attempt_at: str | None
    next_retry_at: str | None
    failure_kind: str | None
    failure_taxon: str | None
    failure_category: str | None
    failure_kind_detail: str | None
    parent_receipt_id: str | None
    receipt_id: str | None
    reason_pending: str | None


@dataclass(frozen=True)
class RetryOutboxSummary:
    """Aggregate retry/outbox accountability evidence summary.

    All fields are JSON-safe.

    Attributes
    ----------
    counts:
        Aggregate counts keyed by status string.  Includes outbox statuses
        (``pending``, ``in_progress``, ``queued``, ``retry_wait``, ``sent``,
        ``dead_lettered``, ``cancelled``, ``abandoned``), receipt-only
        statuses (``suppressed``, ``failed``), and the derived
        ``shutdown_pending`` count (all non-terminal outbox items).
    items:
        Per-item summaries, deterministically sorted.
    retry_worker:
        Retry worker counters if *retry_state* was provided, otherwise
        ``None``.  When present, contains keys ``enabled``, ``running``,
        ``last_run_at``, ``processed``, ``succeeded``, ``failed``,
        ``dead_lettered``.
    """

    counts: dict[str, int]
    items: list[RetryOutboxItemSummary]
    retry_worker: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_retry_outbox_summary(
    receipts: Iterable[Any] = (),
    outbox_items: Iterable[Any] = (),
    retry_state: Any | None = None,
) -> RetryOutboxSummary:
    """Build a retry/outbox accountability evidence summary.

    Pure function — no I/O, no state mutation, no storage access.
    Accepts dataclass/struct objects or dict-like records for each
    parameter.

    Parameters
    ----------
    receipts:
        Delivery receipt records (``DeliveryReceipt`` objects or dicts).
    outbox_items:
        Outbox item records (``DeliveryOutboxItem`` objects or dicts).
    retry_state:
        Retry worker state (``RetryWorkerState`` object or dict), or
        ``None`` if unavailable.

    Returns
    -------
    RetryOutboxSummary
        Frozen, JSON-safe accountability summary.
    """
    outbox_list = list(outbox_items)
    receipt_list = list(receipts)

    # --- Build outbox-keyed set for receipt deduplication -----------------
    # Outbox items are the authoritative operational state.  Receipts whose
    # plan+adapter+channel match an outbox item are already represented.
    outbox_keys: set[tuple[str, str, str | None]] = set()
    for obx in outbox_list:
        key = (
            _get(obx, "delivery_plan_id") or "",
            _get(obx, "target_adapter") or "",
            _get(obx, "target_channel"),
        )
        outbox_keys.add(key)

    # --- Build per-item summaries from outbox items ----------------------
    items: list[RetryOutboxItemSummary] = [
        _build_outbox_item_summary(obx) for obx in outbox_list
    ]

    # --- Add receipt-only items (suppressed / failed without outbox) -----
    for rcpt in receipt_list:
        status = _get(rcpt, "status") or "queued"
        # Only include receipt-only statuses that are not covered by outbox.
        if status not in _RECEIPT_ONLY_STATUSES:
            continue
        # Skip if an outbox item already covers this delivery target.
        rcpt_key = (
            _get(rcpt, "delivery_plan_id") or "",
            _get(rcpt, "target_adapter") or "",
            _get(rcpt, "target_channel"),
        )
        if rcpt_key in outbox_keys:
            continue
        items.append(_build_receipt_only_summary(rcpt))

    # --- Deterministic sort ----------------------------------------------
    items.sort(key=_item_sort_key)

    # --- Aggregate counts ------------------------------------------------
    counts: dict[str, int] = {}

    # Outbox status counts.
    for obx in outbox_list:
        st = _get(obx, "status") or "pending"
        counts[st] = counts.get(st, 0) + 1

    # Ensure all known outbox statuses are present (even if zero).
    for st in sorted(_OUTBOX_STATUSES):
        counts.setdefault(st, 0)

    # Receipt-only status counts (suppressed, failed without outbox).
    suppressed_count = 0
    failed_receipt_count = 0
    for rcpt in receipt_list:
        status = _get(rcpt, "status") or "queued"
        rcpt_key = (
            _get(rcpt, "delivery_plan_id") or "",
            _get(rcpt, "target_adapter") or "",
            _get(rcpt, "target_channel"),
        )
        # Skip receipts whose key is already covered by an outbox item.
        if rcpt_key in outbox_keys:
            continue
        if status == "suppressed":
            suppressed_count += 1
        elif status == "failed":
            failed_receipt_count += 1

    counts["suppressed"] = suppressed_count
    counts["failed"] = counts.get("failed", 0) + failed_receipt_count

    # Derived: shutdown_pending = all non-terminal outbox items.
    shutdown_pending = sum(counts.get(st, 0) for st in _NON_TERMINAL_OUTBOX)
    counts["shutdown_pending"] = shutdown_pending

    # --- Retry worker state ----------------------------------------------
    retry_worker: dict[str, Any] | None = None
    if retry_state is not None:
        retry_worker = {
            "enabled": bool(_get(retry_state, "enabled", False)),
            "running": bool(_get(retry_state, "running", False)),
            "last_run_at": _to_iso(_get(retry_state, "last_run_at")),
            "processed": int(_get(retry_state, "processed", 0)),
            "succeeded": int(_get(retry_state, "succeeded", 0)),
            "failed": int(_get(retry_state, "failed", 0)),
            "dead_lettered": int(_get(retry_state, "dead_lettered", 0)),
        }

    return RetryOutboxSummary(
        counts=counts,
        items=items,
        retry_worker=retry_worker,
    )
