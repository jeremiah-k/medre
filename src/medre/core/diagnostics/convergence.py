"""Pure recovery convergence diagnostics for persisted outbox+receipt snapshots.

Classifies the convergence state of every delivery target by cross-referencing
outbox item statuses against delivery receipt statuses.  The model is **pure**
and **read-only**: no storage I/O, no state mutation, no side effects.

Design constraints
------------------
* **No I/O, no state mutation.**  All public functions are pure.
* **JSON-safe values.**  Every field is ``str | int | float | bool | None |
  list | dict``; ``datetime`` values are converted to ISO-8601 strings.
* **No storage imports.**  Accepts plain objects or dict-like records via
  duck-typed field access.
* **Deterministic ordering.**  Targets are sorted by group key; receipts
  are ranked by ``(attempt_number, sequence, created_at, receipt_id)``.

Status vocabularies (source: :mod:`medre.core.engine.pipeline.delivery_state`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Receipt statuses:
    ``queued``, ``sent``, ``failed``, ``dead_lettered``, ``suppressed``.
Outbox statuses:
    ``pending``, ``in_progress``, ``queued``, ``sent``, ``retry_wait``,
    ``dead_lettered``, ``cancelled``, ``abandoned``.

Classification rules
--------------------
For each delivery target (grouped by ``delivery_plan_id + target_adapter +
target_channel``):

1. **safe** — outbox terminal ``sent`` and latest receipt terminal ``sent``;
   or both terminal and matching (e.g. ``dead_lettered``/``dead_lettered``);
   or receipt-only terminal evidence (explicit warning emitted).
2. **degraded** — non-terminal outbox (``pending``, ``retry_wait``) with a
   ``failed`` receipt (work stalled, retry expected); ``in_progress``/``queued``
   outbox without any receipt (mid-flight, receipt not yet written);
   missing ``delivery_plan_id`` (degraded with warning).
3. **inconsistent** — terminal outbox but latest receipt is non-terminal;
   non-terminal outbox but latest receipt is terminal ``sent``/``suppressed``;
   status mismatch that cannot be explained by normal flow.

Wave 2 integration hooks
------------------------
* ``orphan_count`` field on :class:`ConvergenceSummary` — reserved for
  orphan SQL integration.
* ``evidence_bundle_ref`` field — reserved for EvidenceBundle cross-reference.
* ``warnings`` list — extensible for future diagnostics messages.

Public symbols
--------------
* :class:`ConvergenceSeverity` — enum with values ``safe``, ``degraded``,
  ``inconsistent``.
* :class:`DeliveryTargetConvergence` — per-target convergence result.
* :class:`ConvergenceSummary` — aggregate summary across all targets.
* :func:`build_convergence_summary` — main entry point.
* :class:`OrphanFinding` — single orphan/invalid-lineage finding.
* :class:`OrphanReport` — aggregate orphan/invalid-lineage report.
* :func:`build_orphan_report` — orphan/invalid-lineage detection entry point.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Iterable

__all__ = [
    "ConvergenceSeverity",
    "DeliveryTargetConvergence",
    "ConvergenceSummary",
    "build_convergence_summary",
    "OrphanFinding",
    "OrphanReport",
    "build_orphan_report",
]


# ---------------------------------------------------------------------------
# Status vocabulary constants (mirrors delivery_state.py — leaf module, no import)
# ---------------------------------------------------------------------------

_TERMINAL_RECEIPT = frozenset({"sent", "dead_lettered", "suppressed"})
_NON_TERMINAL_RECEIPT = frozenset({"queued", "failed"})

_TERMINAL_OUTBOX = frozenset({"sent", "dead_lettered", "cancelled", "abandoned"})
_NON_TERMINAL_OUTBOX = frozenset({"pending", "in_progress", "queued", "retry_wait"})


# ---------------------------------------------------------------------------
# Convergence severity enum
# ---------------------------------------------------------------------------


class ConvergenceSeverity(str, enum.Enum):
    """Canonical convergence severity levels.

    Members are plain lowercase strings that serialise directly via
    ``.value``.
    """

    SAFE = "safe"
    DEGRADED = "degraded"
    INCONSISTENT = "inconsistent"


# ---------------------------------------------------------------------------
# Internal helpers — duck-typed field access
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
    outbox_non_terminal = outbox_status in _NON_TERMINAL_OUTBOX if outbox_status else False
    receipt_terminal = latest_receipt_status in _TERMINAL_RECEIPT if latest_receipt_status else False
    receipt_non_terminal = latest_receipt_status in _NON_TERMINAL_RECEIPT if latest_receipt_status else False

    # --- No delivery_plan_id: degraded with warning ---
    if not plan_id_present:
        if has_outbox or has_receipt:
            warnings.append(
                "delivery_plan_id is empty; target grouped by fallback key"
            )
            # Still classify — but degrade if we can't fully reconcile
            if has_outbox and has_receipt:
                if outbox_terminal and receipt_terminal and outbox_status == latest_receipt_status:
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
# Public data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryTargetConvergence:
    """Convergence state for a single delivery target.

    All fields are JSON-safe.

    Attributes
    ----------
    delivery_plan_id:
        Grouping key component.  Empty string when absent.
    target_adapter:
        Adapter name.
    target_channel:
        Channel identifier or ``None``.
    outbox_status:
        Status of the outbox item for this target, or ``None`` if no
        outbox item exists.
    latest_receipt_status:
        Status of the latest (highest authority) receipt, or ``None``
        if no receipt exists.
    latest_receipt_id:
        Receipt ID of the latest receipt, or ``None``.
    latest_attempt_number:
        Attempt number from the latest receipt, or ``None``.
    severity:
        Convergence severity classification.
    warnings:
        Diagnostic messages explaining the classification.
    outbox_id:
        Outbox item ID, or ``None``.
    """

    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    outbox_status: str | None
    latest_receipt_status: str | None
    latest_receipt_id: str | None
    latest_attempt_number: int | None
    severity: str
    warnings: tuple[str, ...]
    outbox_id: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys.

        Tuples are converted to lists so that ``json.loads(json.dumps(d))``
        round-trips correctly.
        """
        result: dict[str, Any] = {
            name: getattr(self, name)
            for name in sorted(f.name for f in fields(self))
        }
        # Ensure tuple fields become lists for JSON round-trip safety.
        result["warnings"] = list(result["warnings"])
        return result


@dataclass(frozen=True)
class ConvergenceSummary:
    """Aggregate convergence summary across all delivery targets.

    All fields are JSON-safe.

    Attributes
    ----------
    severity_counts:
        Count of targets per severity level (``safe``, ``degraded``,
        ``inconsistent``).
    targets:
        Per-target convergence results, deterministically sorted by
        group key.
    total_targets:
        Total number of unique delivery targets examined.
    worst_severity:
        The worst (most severe) convergence level across all targets,
        or ``None`` if no targets were examined.
    warnings:
        Aggregate diagnostic messages.
    orphan_count:
        Reserved for Wave 2 orphan SQL integration.  ``None`` until
        implemented.
    evidence_bundle_ref:
        Reserved for Wave 2 EvidenceBundle cross-reference.  ``None``
        until implemented.
    """

    severity_counts: dict[str, int]
    targets: tuple[DeliveryTargetConvergence, ...]
    total_targets: int
    worst_severity: str | None
    warnings: tuple[str, ...]
    orphan_count: int | None
    evidence_bundle_ref: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        return {
            "evidence_bundle_ref": self.evidence_bundle_ref,
            "orphan_count": self.orphan_count,
            "severity_counts": self.severity_counts,
            "targets": [t.to_dict() for t in self.targets],
            "total_targets": self.total_targets,
            "warnings": list(self.warnings),
            "worst_severity": self.worst_severity,
        }


# ---------------------------------------------------------------------------
# Wave 2 — Orphan / invalid lineage detection models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrphanFinding:
    """A single orphan or invalid-lineage finding.

    All fields are JSON-safe.

    Attributes
    ----------
    kind:
        Category of finding.  One of: ``orphaned_outbox``,
        ``orphaned_parent_receipt``, ``cross_plan_parent``,
        ``cross_event_parent``, ``missing_delivery_plan_id``,
        ``dead_lettered_retryable_mismatch``.
    severity:
        Convergence severity string (``inconsistent`` or ``degraded``).
    record_id:
        Identifier of the affected record (``outbox_id`` or
        ``receipt_id``).
    record_type:
        ``"outbox"`` or ``"receipt"``.
    details:
        Human-readable diagnostic message.
    extra:
        Additional JSON-safe context (event IDs, plan IDs, statuses).
    """

    kind: str
    severity: str
    record_id: str
    record_type: str
    details: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        return {name: getattr(self, name) for name in sorted(f.name for f in fields(self))}


@dataclass(frozen=True)
class OrphanReport:
    """Aggregate orphan / invalid-lineage report.

    All fields are JSON-safe.

    Attributes
    ----------
    findings:
        Individual findings, sorted deterministically by
        ``(kind, record_id)``.
    total_findings:
        Total number of findings.
    severity_counts:
        Count of findings per severity level.
    worst_severity:
        Worst severity among findings, or ``None`` if empty.
    summary:
        Human-readable summary line.
    """

    findings: tuple[OrphanFinding, ...]
    total_findings: int
    severity_counts: dict[str, int]
    worst_severity: str | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "severity_counts": self.severity_counts,
            "summary": self.summary,
            "total_findings": self.total_findings,
            "worst_severity": self.worst_severity,
        }


# ---------------------------------------------------------------------------
# Finding-kind constants
# ---------------------------------------------------------------------------

#: Non-terminal outbox item whose ``event_id`` is absent from
#: *known_event_ids*.
KIND_ORPHANED_OUTBOX = "orphaned_outbox"

#: Receipt whose ``parent_receipt_id`` does not exist in the supplied
#: receipt set.
KIND_ORPHANED_PARENT_RECEIPT = "orphaned_parent_receipt"

#: Receipt whose parent belongs to a different ``delivery_plan_id``.
KIND_CROSS_PLAN_PARENT = "cross_plan_parent"

#: Receipt whose parent belongs to a different ``event_id``.
KIND_CROSS_EVENT_PARENT = "cross_event_parent"

#: Retry-source receipt with missing or empty ``delivery_plan_id``.
KIND_MISSING_DELIVERY_PLAN_ID = "missing_delivery_plan_id"

#: Dead-lettered outbox item whose latest receipt is non-terminal,
#: suggesting the item may still be retryable.
KIND_DEAD_LETTERED_RETRYABLE_MISMATCH = "dead_lettered_retryable_mismatch"


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
    * ``orphan_count`` and ``evidence_bundle_ref`` are reserved for
      Wave 2 integration and will be ``None`` until implemented.
    """
    receipt_list = list(receipts)
    outbox_list = list(outbox_items)

    # --- Build target-keyed maps ------------------------------------------
    # Outbox items: at most one per target key (latest by attempt_number).
    outbox_by_key: dict[_TargetKey, Any] = {}
    for obx in outbox_list:
        key = _target_key(obx)
        existing = outbox_by_key.get(key)
        if existing is None:
            outbox_by_key[key] = obx
        else:
            # Keep higher attempt_number; break ties by outbox_id
            existing_attempt = _get(existing, "attempt_number") or 0
            new_attempt = _get(obx, "attempt_number") or 0
            if new_attempt > existing_attempt:
                outbox_by_key[key] = obx
            elif new_attempt == existing_attempt:
                existing_id = _get(existing, "outbox_id") or ""
                new_id = _get(obx, "outbox_id") or ""
                if new_id > existing_id:
                    outbox_by_key[key] = obx

    # Receipts: group by target key.
    receipts_by_key: dict[_TargetKey, list[Any]] = {}
    for rec in receipt_list:
        key = _target_key(rec)
        receipts_by_key.setdefault(key, []).append(rec)

    # --- Collect all target keys (union) ----------------------------------
    all_keys = sorted(set(outbox_by_key.keys()) | set(receipts_by_key.keys()))

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
        latest_attempt_number = _get(latest_rec, "attempt_number") if latest_rec else None

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


# ---------------------------------------------------------------------------
# Wave 2 — Orphan / invalid lineage builder
# ---------------------------------------------------------------------------


def _latest_receipt_for_target(
    receipts_by_key: dict[_TargetKey, list[Any]],
    key: _TargetKey,
) -> Any | None:
    """Select the latest receipt for a target key (reuses ranking logic)."""
    recs = receipts_by_key.get(key, [])
    return _pick_latest_receipt(recs)


def build_orphan_report(
    receipts: Iterable[Any] = (),
    outbox_items: Iterable[Any] = (),
    known_event_ids: set[str] | frozenset[str] | None = None,
) -> OrphanReport:
    """Build an orphan / invalid-lineage report from receipt and outbox snapshots.

    Pure function — no I/O, no state mutation, no storage access.
    Accepts dataclass/struct objects or dict-like records for each
    parameter.

    Parameters
    ----------
    receipts:
        Delivery receipt records (``DeliveryReceipt`` objects or dicts).
    outbox_items:
        Outbox item records (``DeliveryOutboxItem`` objects or dicts).
    known_event_ids:
        Set of event IDs known to exist in the event store.  When
        supplied, non-terminal outbox items whose ``event_id`` is not
        in this set are flagged as orphaned.  ``None`` (default) means
        the caller has no event catalogue and orphaned-outbox checks
        are skipped.

    Returns
    -------
    OrphanReport
        Frozen, JSON-safe orphan report with deterministic ordering.

    Detection rules
    ---------------
    ``orphaned_outbox`` (inconsistent):
        Non-terminal outbox item whose ``event_id`` is absent from
        *known_event_ids*.  Only checked when *known_event_ids* is
        provided.

    ``orphaned_parent_receipt`` (inconsistent):
        Receipt with a non-empty ``parent_receipt_id`` that does not
        exist in the supplied receipt set.

    ``cross_plan_parent`` (inconsistent):
        Receipt whose parent exists but has a different
        ``delivery_plan_id``.

    ``cross_event_parent`` (inconsistent):
        Receipt whose parent exists but has a different ``event_id``.

    ``missing_delivery_plan_id`` (degraded):
        Receipt with ``source="retry"`` whose ``delivery_plan_id`` is
        empty or ``None``.  Degraded because the retry may still succeed
        once the plan ID is resolved.

    ``dead_lettered_retryable_mismatch`` (degraded):
        Outbox item with ``dead_lettered`` status whose latest receipt
        for the same target key is non-terminal (``failed`` or
        ``queued``), suggesting the item may still be retryable despite
        the terminal outbox status.  Degraded because the discrepancy
        is recoverable through re-delivery.
    """
    receipt_list = list(receipts)
    outbox_list = list(outbox_items)

    findings: list[OrphanFinding] = []

    # --- Index receipts by receipt_id for parent lookups ------------------
    receipt_by_id: dict[str, Any] = {}
    for rec in receipt_list:
        rid = _get(rec, "receipt_id") or ""
        if rid:
            receipt_by_id[rid] = rec

    # --- Index outbox items by target key ---------------------------------
    outbox_by_key: dict[_TargetKey, Any] = {}
    for obx in outbox_list:
        key = _target_key(obx)
        existing = outbox_by_key.get(key)
        if existing is None:
            outbox_by_key[key] = obx
        else:
            existing_attempt = _get(existing, "attempt_number") or 0
            new_attempt = _get(obx, "attempt_number") or 0
            if new_attempt > existing_attempt:
                outbox_by_key[key] = obx
            elif new_attempt == existing_attempt:
                existing_id = _get(existing, "outbox_id") or ""
                new_id = _get(obx, "outbox_id") or ""
                if new_id > existing_id:
                    outbox_by_key[key] = obx

    # --- Index receipts by target key -------------------------------------
    receipts_by_key: dict[_TargetKey, list[Any]] = {}
    for rec in receipt_list:
        key = _target_key(rec)
        receipts_by_key.setdefault(key, []).append(rec)

    # --- 1. Orphaned outbox (event_id not in known_event_ids) -------------
    if known_event_ids is not None:
        event_id_set = (
            known_event_ids
            if isinstance(known_event_ids, (set, frozenset))
            else set(known_event_ids)
        )
        for obx in outbox_list:
            status = _get(obx, "status")
            if status in _NON_TERMINAL_OUTBOX:
                eid = _get(obx, "event_id") or ""
                if eid and eid not in event_id_set:
                    oid = _get(obx, "outbox_id") or ""
                    findings.append(
                        OrphanFinding(
                            kind=KIND_ORPHANED_OUTBOX,
                            severity=ConvergenceSeverity.INCONSISTENT.value,
                            record_id=oid,
                            record_type="outbox",
                            details=(
                                f"Non-terminal outbox item {oid} references "
                                f"event_id {eid!r} not found in known_event_ids"
                            ),
                            extra={
                                "event_id": eid,
                                "outbox_id": oid,
                                "status": status,
                            },
                        )
                    )

    # --- 2. Receipt parent lineage checks ---------------------------------
    for rec in receipt_list:
        parent_id = _get(rec, "parent_receipt_id")
        if not parent_id:
            continue

        receipt_id = _get(rec, "receipt_id") or ""

        parent = receipt_by_id.get(parent_id)

        # 2a. Orphaned parent receipt
        if parent is None:
            findings.append(
                OrphanFinding(
                    kind=KIND_ORPHANED_PARENT_RECEIPT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} references parent_receipt_id "
                        f"{parent_id!r} which does not exist in the receipt set"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                    },
                )
            )
            continue

        # 2b. Cross-plan parent
        parent_plan = _get(parent, "delivery_plan_id") or ""
        child_plan = _get(rec, "delivery_plan_id") or ""
        if parent_plan != child_plan:
            findings.append(
                OrphanFinding(
                    kind=KIND_CROSS_PLAN_PARENT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} (plan={child_plan!r}) has "
                        f"parent_receipt_id {parent_id!r} belonging to "
                        f"different plan {parent_plan!r}"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                        "delivery_plan_id": child_plan,
                        "parent_delivery_plan_id": parent_plan,
                    },
                )
            )

        # 2c. Cross-event parent
        parent_event = _get(parent, "event_id") or ""
        child_event = _get(rec, "event_id") or ""
        if parent_event != child_event:
            findings.append(
                OrphanFinding(
                    kind=KIND_CROSS_EVENT_PARENT,
                    severity=ConvergenceSeverity.INCONSISTENT.value,
                    record_id=receipt_id,
                    record_type="receipt",
                    details=(
                        f"Receipt {receipt_id} (event={child_event!r}) has "
                        f"parent_receipt_id {parent_id!r} belonging to "
                        f"different event {parent_event!r}"
                    ),
                    extra={
                        "receipt_id": receipt_id,
                        "parent_receipt_id": parent_id,
                        "event_id": child_event,
                        "parent_event_id": parent_event,
                    },
                )
            )

    # --- 3. Missing delivery_plan_id on retry receipts --------------------
    for rec in receipt_list:
        source = _get(rec, "source") or ""
        if source == "retry":
            plan_id = _get(rec, "delivery_plan_id") or ""
            if not plan_id:
                receipt_id = _get(rec, "receipt_id") or ""
                findings.append(
                    OrphanFinding(
                        kind=KIND_MISSING_DELIVERY_PLAN_ID,
                        severity=ConvergenceSeverity.DEGRADED.value,
                        record_id=receipt_id,
                        record_type="receipt",
                        details=(
                            f"Retry receipt {receipt_id} has missing or "
                            f"empty delivery_plan_id"
                        ),
                        extra={
                            "receipt_id": receipt_id,
                            "source": source,
                        },
                    )
                )

    # --- 4. Dead-lettered outbox with retryable receipt -------------------
    for key, obx in outbox_by_key.items():
        outbox_status = _get(obx, "status")
        if outbox_status != "dead_lettered":
            continue

        latest_rec = _latest_receipt_for_target(receipts_by_key, key)
        if latest_rec is None:
            continue

        receipt_status = _get(latest_rec, "status") or ""
        if receipt_status in _NON_TERMINAL_RECEIPT:
            oid = _get(obx, "outbox_id") or ""
            rid = _get(latest_rec, "receipt_id") or ""
            findings.append(
                OrphanFinding(
                    kind=KIND_DEAD_LETTERED_RETRYABLE_MISMATCH,
                    severity=ConvergenceSeverity.DEGRADED.value,
                    record_id=oid,
                    record_type="outbox",
                    details=(
                        f"Dead-lettered outbox item {oid} has latest "
                        f"receipt {rid} with non-terminal status "
                        f"{receipt_status!r}; item may be retryable"
                    ),
                    extra={
                        "outbox_id": oid,
                        "receipt_id": rid,
                        "outbox_status": outbox_status,
                        "receipt_status": receipt_status,
                    },
                )
            )

    # --- Deterministic sort and aggregate ---------------------------------
    findings.sort(key=lambda f: (f.kind, f.record_id))

    severity_counts: dict[str, int] = {
        ConvergenceSeverity.SAFE.value: 0,
        ConvergenceSeverity.DEGRADED.value: 0,
        ConvergenceSeverity.INCONSISTENT.value: 0,
    }
    finding_severities: list[ConvergenceSeverity] = []
    for finding in findings:
        sev = ConvergenceSeverity(finding.severity)
        severity_counts[sev.value] += 1
        finding_severities.append(sev)

    total = len(findings)
    worst = _worst_severity(finding_severities)

    summary = (
        f"{total} finding(s): "
        f"{severity_counts[ConvergenceSeverity.INCONSISTENT.value]} inconsistent, "
        f"{severity_counts[ConvergenceSeverity.DEGRADED.value]} degraded"
        if total > 0
        else "No orphan or invalid-lineage findings"
    )

    return OrphanReport(
        findings=tuple(findings),
        total_findings=total,
        severity_counts=severity_counts,
        worst_severity=worst,
        summary=summary,
    )
