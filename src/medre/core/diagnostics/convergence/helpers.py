"""Internal helper utilities for recovery convergence diagnostics.

Duck-typed field access, datetime normalization, target key construction,
receipt ranking, and severity helpers.  These are package-internal; they
are re-exported via ``__init__.py`` only when needed by sibling submodules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .types import ConvergenceSeverity

__all__ = [
    "_get",
    "_to_iso",
    "_target_key",
    "_TargetKey",
    "_ReverseStr",
    "_receipt_sort_key",
    "_pick_latest_receipt",
    "_worst_severity",
    "_SEVERITY_ORDER",
    "_TERMINAL_RECEIPT",
    "_NON_TERMINAL_RECEIPT",
    "_TERMINAL_OUTBOX",
    "_NON_TERMINAL_OUTBOX",
    "_latest_receipt_for_target",
]


# ---------------------------------------------------------------------------
# Status vocabulary constants (mirrors delivery_state.py — leaf module, no import)
# ---------------------------------------------------------------------------

_TERMINAL_RECEIPT = frozenset({"sent", "dead_lettered", "suppressed"})
_NON_TERMINAL_RECEIPT = frozenset({"queued", "failed"})

_TERMINAL_OUTBOX = frozenset({"sent", "dead_lettered", "cancelled", "abandoned"})
_NON_TERMINAL_OUTBOX = frozenset({"pending", "in_progress", "queued", "retry_wait"})


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

_TargetKey = tuple[str, str, str | None]
"""``(delivery_plan_id, target_adapter, target_channel)``."""


def _target_key(obj: Any) -> _TargetKey:
    """Build a deterministic group key from a record.

    Falls back to ``""`` for missing ``delivery_plan_id`` and
    ``target_adapter``; ``None`` is preserved for ``target_channel`` to
    distinguish "absent" from "empty string".
    """
    plan_id = _get(obj, "delivery_plan_id") or ""
    adapter = _get(obj, "target_adapter") or ""
    channel = _get(obj, "target_channel")
    return (plan_id, adapter, channel)


# ---------------------------------------------------------------------------
# Receipt ranking — deterministic latest-selection
# ---------------------------------------------------------------------------


class _ReverseStr:
    """Wrapper that reverses string comparison order for ``min()`` selection.

    ``_ReverseStr("b") < _ReverseStr("a")`` so that ``min()`` picks
    the lexicographically *latest* string value.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __lt__(self, other: _ReverseStr) -> bool:  # type: ignore[override]
        return self._value > other._value

    def __le__(self, other: _ReverseStr) -> bool:  # type: ignore[override]
        return self._value >= other._value

    def __gt__(self, other: _ReverseStr) -> bool:  # type: ignore[override]
        return self._value < other._value

    def __ge__(self, other: _ReverseStr) -> bool:  # type: ignore[override]
        return self._value <= other._value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ReverseStr):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


def _receipt_sort_key(rec: Any) -> tuple:
    """Sort key for deterministic latest-receipt selection.

    Used with ``min()``.  All components are arranged so that the
    "latest" / most authoritative receipt has the *smallest* key:

    * ``attempt_number`` — negated so higher attempts sort first.
    * ``sequence`` — negated so higher sequences sort first.
    * ``created_at`` — wrapped in :class:`_ReverseStr` so later
      timestamps sort first.
    * ``receipt_id`` — wrapped in :class:`_ReverseStr` so
      lexicographically larger IDs sort first.

    Does not rely on object identity.
    """
    attempt = _get(rec, "attempt_number") or 0
    sequence = _get(rec, "sequence") or 0
    created_at = _to_iso(_get(rec, "created_at")) or ""
    receipt_id = _get(rec, "receipt_id") or ""
    return (
        -attempt,
        -sequence,
        _ReverseStr(created_at),
        _ReverseStr(receipt_id),
    )


def _pick_latest_receipt(receipts: list[Any]) -> Any | None:
    """Select the latest receipt from a list by deterministic ranking.

    Ranking priority (highest wins):
    1. ``attempt_number`` (highest)
    2. ``sequence`` (highest)
    3. ``created_at`` ISO string (lexicographically latest)
    4. ``receipt_id`` (lexicographically latest)

    Does not rely on object identity.
    """
    if not receipts:
        return None
    return min(receipts, key=_receipt_sort_key)


def _latest_receipt_for_target(
    receipts_by_key: dict[_TargetKey, list[Any]],
    key: _TargetKey,
) -> Any | None:
    """Select the latest receipt for a target key (reuses ranking logic)."""
    recs = receipts_by_key.get(key, [])
    return _pick_latest_receipt(recs)


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
