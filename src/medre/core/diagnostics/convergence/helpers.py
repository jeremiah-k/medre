"""Internal helper utilities for convergence diagnostics.

Duck-typed field access, datetime normalization, target key construction,
receipt ranking, and severity helpers.  These are package-internal; they
are used by sibling submodules (summary.py, orphans.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from medre.core.delivery_authority import (
    DeliveryIdentity,
    delivery_identity,
    group_outbox_by_identity,
    select_current_outbox,
)

from .types import ConvergenceSeverity

__all__ = [
    "_get",
    "_target_key",
    "_TargetKey",
    "_worst_severity",
    "_SEVERITY_ORDER",
    "_TERMINAL_RECEIPT",
    "_NON_TERMINAL_RECEIPT",
    "_TERMINAL_OUTBOX",
    "_NON_TERMINAL_OUTBOX",
    "_build_outbox_by_key",
    "_parse_iso_timestamp",
    "_ensure_aware",
    "_safe_record_id",
]


# ---------------------------------------------------------------------------
# Status vocabulary constants (canonical source: delivery_state.py)
# ---------------------------------------------------------------------------
# The delivery state module is the authoritative source of truth for
# receipt and outbox status vocabularies.  Convergence diagnostics are
# read-only consumers — they must classify records by status without
# redefining the vocabulary.  Re-exporting the canonical frozensets
# here (under their internal names) preserves the package-private API
# while making drift detectable by
# ``tests/test_evidence_coherence_contract.py``.

from medre.core.engine.pipeline.delivery_state import (
    NON_TERMINAL_OUTBOX_STATUSES,
    NON_TERMINAL_RECEIPT_STATUSES,
    TERMINAL_OUTBOX_STATUSES,
    TERMINAL_RECEIPT_STATUSES,
)

_TERMINAL_RECEIPT = TERMINAL_RECEIPT_STATUSES
_NON_TERMINAL_RECEIPT = NON_TERMINAL_RECEIPT_STATUSES
_TERMINAL_OUTBOX = TERMINAL_OUTBOX_STATUSES
_NON_TERMINAL_OUTBOX = NON_TERMINAL_OUTBOX_STATUSES


# ---------------------------------------------------------------------------
# Duck-typed field access
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Retrieve *name* from an object or dict, falling back to *default*."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_iso(value: Any) -> str | None:
    """Convert a value to an ISO-8601 string or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Group key construction
# ---------------------------------------------------------------------------

_TargetKey = DeliveryIdentity
"""``(event_id, delivery_plan_id, target_adapter, target_channel)``."""


def _target_key(obj: Any) -> _TargetKey:
    """Build the shared event-scoped delivery identity for *obj*."""
    return delivery_identity(obj)


# ---------------------------------------------------------------------------
# Worst-severity helper
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {
    ConvergenceSeverity.SAFE: 0,
    ConvergenceSeverity.DEGRADED: 1,
    ConvergenceSeverity.INCONSISTENT: 2,
}


def _worst_severity(severities: list[ConvergenceSeverity]) -> str | None:
    """Return the worst severity string from a list, or ``None`` if empty."""
    if not severities:
        return None
    worst = max(severities, key=lambda s: _SEVERITY_ORDER[s])
    return worst.value


# ---------------------------------------------------------------------------
# Outbox-by-key deduplication
# ---------------------------------------------------------------------------


def _build_outbox_by_key(
    outbox_items: list[Any],
) -> dict[_TargetKey, Any]:
    """Index current operational outbox generations by delivery identity."""
    return {
        identity: current
        for identity, items in group_outbox_by_identity(outbox_items).items()
        if (current := select_current_outbox(items)) is not None
    }


# ---------------------------------------------------------------------------
# Timestamp parsing helper
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: Any) -> datetime | tuple[None, str]:
    """Parse a value to a timezone-aware ``datetime``.

    Returns ``datetime`` on success or ``(None, error_message)`` on failure.

    NOTE: The storage layer has a separate datetime→ISO path
    (_ensure_iso in serde.py). These serve different contexts
    (storage vs diagnostics) and should not be unified.
    """
    if isinstance(value, datetime):
        return value
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
