"""Pure runtime delivery outcome ledger derived from receipts and outbox records.

Provides :func:`build_delivery_outcome_ledger`, which accepts existing
:class:`~medre.core.events.canonical.DeliveryReceipt` and
:class:`~medre.core.storage.backend.DeliveryOutboxItem` objects (or their
dict representations) and produces a deterministic, JSON-safe summary of
end-to-end delivery outcome lineage.

Design constraints
------------------
* **No storage schema changes** — reads existing record fields only.
* **No runtime imports** — depends only on
  :mod:`medre.core.evidence.failure_taxonomy`,
  :mod:`medre.core.engine.pipeline.delivery_state` (leaf-level constants),
  and standard library.
* **Pure functions** — no I/O, no state mutation, no side effects.
* **JSON-safe** — all output values survive ``json.dumps`` round-trips.

Public symbols
--------------
* :class:`DeliveryOutcomeEntry` — one delivery target's final state.
* :class:`DeliveryOutcomeLedger` — grouped entries + aggregate counts.
* :func:`build_delivery_outcome_ledger` — main entry point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from medre.core.engine.pipeline.delivery_state import (
    OUTBOX_STATUSES,
    TERMINAL_OUTBOX_STATUSES,
    TERMINAL_RECEIPT_STATUSES,
)
from medre.core.evidence.failure_taxonomy import (
    resolve_taxon,
    taxon_category,
)

__all__ = [
    "DeliveryOutcomeEntry",
    "DeliveryOutcomeLedger",
    "build_delivery_outcome_ledger",
]


# ---------------------------------------------------------------------------
# Internal normalisation helpers
# ---------------------------------------------------------------------------


def _getattr_or_get(obj: Any, name: str, default: Any = None) -> Any:
    """Retrieve *name* from a struct (getattr) or dict (get)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize_receipt(rec: Any) -> dict[str, Any]:
    """Normalise a DeliveryReceipt (struct or dict) to a uniform dict."""
    return {
        "receipt_id": _getattr_or_get(rec, "receipt_id", ""),
        "event_id": _getattr_or_get(rec, "event_id", ""),
        "delivery_plan_id": _getattr_or_get(rec, "delivery_plan_id", ""),
        "target_adapter": _getattr_or_get(rec, "target_adapter", ""),
        "target_channel": _getattr_or_get(rec, "target_channel"),
        "route_id": _getattr_or_get(rec, "route_id", ""),
        "status": _getattr_or_get(rec, "status", "queued"),
        "error": _getattr_or_get(rec, "error"),
        "failure_kind": _getattr_or_get(rec, "failure_kind"),
        "attempt_number": _getattr_or_get(rec, "attempt_number", 1),
        "next_retry_at": _getattr_or_get(rec, "next_retry_at"),
        "source": _getattr_or_get(rec, "source", "live"),
        "replay_run_id": _getattr_or_get(rec, "replay_run_id"),
        "rendering_evidence": _getattr_or_get(rec, "rendering_evidence"),
        "adapter_message_id": _getattr_or_get(rec, "adapter_message_id"),
        "parent_receipt_id": _getattr_or_get(rec, "parent_receipt_id"),
        "retry_max_attempts": _getattr_or_get(rec, "retry_max_attempts"),
        "sequence": _getattr_or_get(rec, "sequence", 0),
    }


def _normalize_outbox_item(item: Any) -> dict[str, Any]:
    """Normalise a DeliveryOutboxItem (struct or dict) to a uniform dict."""
    return {
        "outbox_id": _getattr_or_get(item, "outbox_id", ""),
        "event_id": _getattr_or_get(item, "event_id", ""),
        "delivery_plan_id": _getattr_or_get(item, "delivery_plan_id", ""),
        "target_adapter": _getattr_or_get(item, "target_adapter", ""),
        "target_channel": _getattr_or_get(item, "target_channel"),
        "route_id": _getattr_or_get(item, "route_id", ""),
        "status": _getattr_or_get(item, "status", "pending"),
        "failure_kind": _getattr_or_get(item, "failure_kind"),
        "failure_kind_detail": _getattr_or_get(item, "failure_kind_detail"),
        "attempt_number": _getattr_or_get(item, "attempt_number", 1),
        "error_summary": _getattr_or_get(item, "error_summary"),
        "metadata": _getattr_or_get(item, "metadata"),
        "receipt_id": _getattr_or_get(item, "receipt_id"),
        "parent_receipt_id": _getattr_or_get(item, "parent_receipt_id"),
    }


# ---------------------------------------------------------------------------
# Capability-evidence derivation (pure, mirrors reporting logic)
# ---------------------------------------------------------------------------


def _derive_capability_fields(
    error: str | None,
    rendering_evidence: str | None,
    failure_kind: str | None,
    status: str,
) -> dict[str, Any]:
    """Derive capability-suppression fields from receipt data.

    Pure re-implementation of ``reporting._derive_capability_evidence``
    so the evidence layer does not import from the runtime package.
    """
    result: dict[str, Any] = {
        "suppression_reason": None,
        "capability_field": None,
        "capability_level": None,
        "delivery_strategy": None,
    }

    # 1. Try rendering_evidence JSON first.
    if rendering_evidence is not None:
        try:
            ev = json.loads(rendering_evidence)
            if isinstance(ev, dict):
                result["capability_level"] = ev.get("capability_level")
                result["delivery_strategy"] = ev.get("delivery_strategy")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 2. Suppressed receipts: derive from error text.
    if status == "suppressed" and error:
        import re

        cap_match = re.match(r"^capability_suppressed:\s*(.+)$", error)
        if cap_match:
            reason_text = cap_match.group(1).strip()
            result["suppression_reason"] = reason_text
            field_match = re.match(r"^(\w+)\s+(unsupported|fallback)\b", reason_text)
            if field_match:
                result["capability_field"] = field_match.group(1)
                level = field_match.group(2)
                result["capability_level"] = level
                result["delivery_strategy"] = (
                    "skip" if level == "unsupported" else "fallback_text"
                )
            elif failure_kind == "capability_suppressed":
                result["capability_level"] = "unsupported"
                result["delivery_strategy"] = "skip"
        elif error.startswith("plan_skip:") or error.startswith("delivery_skipped:"):
            result["suppression_reason"] = error
            result["delivery_strategy"] = "skip"
            if failure_kind == "capability_suppressed":
                result["capability_level"] = "unsupported"
        elif failure_kind == "loop_suppressed":
            result["suppression_reason"] = error
        elif failure_kind == "policy_suppressed":
            result["suppression_reason"] = error
        else:
            result["suppression_reason"] = error

    # Safety net for capability_suppressed.
    if failure_kind == "capability_suppressed":
        if result["capability_level"] not in ("unsupported", "fallback"):
            result["capability_level"] = "unsupported"
        if result["delivery_strategy"] not in ("skip", "fallback_text"):
            result["delivery_strategy"] = "skip"

    return result


# ---------------------------------------------------------------------------
# Retry-state derivation
# ---------------------------------------------------------------------------

# NOTE: Intentionally broader than convergence helpers — covers both outbox
# and receipt terminal statuses for retry-state derivation (includes "suppressed").
_TERMINAL_STATUSES: frozenset[str] = (
    TERMINAL_OUTBOX_STATUSES | TERMINAL_RECEIPT_STATUSES
)
# Derived from canonical outbox lifecycle constants: the set of outbox statuses
# that are NOT terminal.  Equivalent to {"pending", "in_progress", "queued",
# "retry_wait"} but kept in sync automatically when statuses are added.
_ACTIVE_STATUSES: frozenset[str] = OUTBOX_STATUSES - TERMINAL_OUTBOX_STATUSES


def _derive_retry_state(
    status: str,
    next_retry_at: Any,
    failure_kind: str | None,
) -> str:
    """Derive a human-readable retry-state label.

    Returns one of: ``"terminal"``, ``"retryable"``, ``"active"``,
    ``"unknown"``.

    **These return values are derived display labels, not authoritative
    lifecycle states.**  They are computed from persisted outbox/receipt
    status strings and retry-scheduling metadata for evidence reporting
    purposes only.  Pipeline state transitions must use the canonical
    constants and helpers in
    :mod:`~medre.core.engine.pipeline.delivery_state`.
    """
    if status in _TERMINAL_STATUSES:
        return "terminal"
    if next_retry_at is not None:
        return "retryable"
    if status == "failed" and failure_kind == "adapter_transient":
        return "retryable"
    if status in _ACTIVE_STATUSES:
        return "active"
    if status == "failed":
        return "retryable"
    return "unknown"


# ---------------------------------------------------------------------------
# Group key construction
# ---------------------------------------------------------------------------


def _make_group_key(r: dict[str, Any]) -> str:
    """Build a deterministic group key from a normalised record.

    When ``delivery_plan_id`` is present, uses it as the primary grouping
    dimension together with target/route/source.  When absent, falls back
    to ``event_id``.
    """
    plan_id = r.get("delivery_plan_id") or ""
    event_id = r.get("event_id") or ""
    primary = plan_id if plan_id else event_id
    return json.dumps(
        {
            "primary_id": primary,
            "target_adapter": r.get("target_adapter") or "",
            "target_channel": r.get("target_channel"),
            "route_id": r.get("route_id") or "",
            "source": r.get("source") or "live",
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DeliveryOutcomeEntry:
    """One delivery target's final-state summary in the outcome ledger.

    All fields are JSON-safe (strings, ints, None).  Datetime values are
    converted to ISO-8601 strings or ``None`` during construction.
    """

    delivery_plan_id: str | None
    event_id: str | None
    route_id: str | None
    target_adapter: str | None
    target_channel: str | None
    delivery_strategy: str | None
    capability_field: str | None
    capability_level: str | None
    suppression_reason: str | None
    final_status: str
    attempt_number: int
    retry_state: str
    failure_kind: str | None
    failure_taxon: str | None
    failure_taxon_category: str | None
    source: str
    replay_run_id: str | None
    receipt_ids: list[str]
    outbox_id: str | None
    adapter_message_id: str | None
    next_retry_at: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation."""
        return {
            "delivery_plan_id": self.delivery_plan_id,
            "event_id": self.event_id,
            "route_id": self.route_id,
            "target_adapter": self.target_adapter,
            "target_channel": self.target_channel,
            "delivery_strategy": self.delivery_strategy,
            "capability_field": self.capability_field,
            "capability_level": self.capability_level,
            "suppression_reason": self.suppression_reason,
            "final_status": self.final_status,
            "attempt_number": self.attempt_number,
            "retry_state": self.retry_state,
            "failure_kind": self.failure_kind,
            "failure_taxon": self.failure_taxon,
            "failure_taxon_category": self.failure_taxon_category,
            "source": self.source,
            "replay_run_id": self.replay_run_id,
            "receipt_ids": self.receipt_ids,
            "outbox_id": self.outbox_id,
            "adapter_message_id": self.adapter_message_id,
            "next_retry_at": self.next_retry_at,
            "error": self.error,
        }


@dataclass
class DeliveryOutcomeLedger:
    """Grouped delivery outcome ledger with aggregate counts.

    Attributes
    ----------
    entries:
        Mapping of deterministic group keys to
        :class:`DeliveryOutcomeEntry` values.  Keys are JSON strings
        encoding the group identity.
    aggregate_counts:
        Summary counts keyed by ``"by_status"`` and ``"by_failure_taxon"``.
    """

    entries: dict[str, DeliveryOutcomeEntry] = field(default_factory=dict)
    aggregate_counts: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"by_status": {}, "by_failure_taxon": {}}
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation of the full ledger."""
        entries_dict = {k: v.to_dict() for k, v in sorted(self.entries.items())}
        return {
            "entries": entries_dict,
            "aggregate_counts": self.aggregate_counts,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _to_iso_or_none(value: Any) -> str | None:
    """Convert a datetime to ISO-8601 string, or return None."""
    if value is None:
        return None
    # If it's already a string, pass through.
    if isinstance(value, str):
        return value
    # Assume datetime-like.
    try:
        if value.tzinfo is None:
            from datetime import timezone

            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    except (AttributeError, TypeError):
        return str(value) if value else None


def build_delivery_outcome_ledger(
    receipts: Iterable[Any] = (),
    outbox_items: Iterable[Any] = (),
) -> DeliveryOutcomeLedger:
    """Build a delivery outcome ledger from receipts and outbox records.

    Parameters
    ----------
    receipts:
        Iterable of :class:`~medre.core.events.canonical.DeliveryReceipt`
        objects or dict-like records with matching fields.
    outbox_items:
        Optional iterable of
        :class:`~medre.core.storage.backend.DeliveryOutboxItem` objects
        or dict-like records.

    Returns
    -------
    DeliveryOutcomeLedger
        Deterministic ledger with entries grouped by delivery target and
        aggregate counts by status and failure taxon.

    Notes
    -----
    * When multiple records share the same group key, the one with the
      highest ``attempt_number`` wins (latest known state).  Ties are
      broken by latest append order (last-seen wins).
    * ``replay_run_id`` is populated only when ``source == "replay"``.
    * Fields that cannot be derived from the input records are set to
      ``None``.
    """
    # Phase 1: Normalise all records into a single list.
    normalised: list[dict[str, Any]] = []
    for rec in receipts:
        normalised.append(_normalize_receipt(rec))
    for item in outbox_items:
        normalised.append(_normalize_outbox_item(item))

    if not normalised:
        return DeliveryOutcomeLedger(
            entries={},
            aggregate_counts={"by_status": {}, "by_failure_taxon": {}},
        )

    # Phase 2: Group by key, keep highest attempt (latest wins on tie).
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in normalised:
        key = _make_group_key(r)
        groups.setdefault(key, []).append(r)

    # Phase 3: For each group, select the winning record and build entry.
    entries: dict[str, DeliveryOutcomeEntry] = {}
    status_counts: dict[str, int] = {}
    taxon_counts: dict[str, int] = {}

    for key in sorted(groups):
        group_recs = groups[key]
        # Select highest attempt_number; break ties by last-seen.
        winner = group_recs[0]
        for r in group_recs[1:]:
            if (r.get("attempt_number") or 1) >= (winner.get("attempt_number") or 1):
                winner = r

        # Gather all receipt IDs in the group.
        receipt_ids = sorted(
            str(rid) for rid in {r.get("receipt_id") for r in group_recs} if rid
        )
        outbox_id = next(
            (r.get("outbox_id") for r in group_recs if r.get("outbox_id")),
            None,
        )

        status = winner.get("status", "queued")
        failure_kind = winner.get("failure_kind")
        error = winner.get("error") or winner.get("error_summary")
        source = winner.get("source", "live")
        rendering_evidence = winner.get("rendering_evidence")
        next_retry_at_raw = winner.get("next_retry_at")

        # Taxon resolution.
        taxon = resolve_taxon(
            failure_kind=failure_kind,
            error=error,
            status=status,
        )
        taxon_str = taxon.value if taxon else None
        taxon_cat = taxon_category(taxon) if taxon else None

        # Capability fields.
        cap = _derive_capability_fields(
            error=error,
            rendering_evidence=rendering_evidence,
            failure_kind=failure_kind,
            status=status,
        )

        # Replay run ID only for replay source.
        replay_run_id = winner.get("replay_run_id") if source == "replay" else None

        # Retry state.
        retry_state = _derive_retry_state(
            status=status,
            next_retry_at=next_retry_at_raw,
            failure_kind=failure_kind,
        )

        delivery_plan_id = winner.get("delivery_plan_id") or None
        event_id = winner.get("event_id") or None

        entry = DeliveryOutcomeEntry(
            delivery_plan_id=delivery_plan_id,
            event_id=event_id,
            route_id=winner.get("route_id") or None,
            target_adapter=winner.get("target_adapter") or None,
            target_channel=winner.get("target_channel"),
            delivery_strategy=cap.get("delivery_strategy"),
            capability_field=cap.get("capability_field"),
            capability_level=cap.get("capability_level"),
            suppression_reason=cap.get("suppression_reason"),
            final_status=status,
            attempt_number=winner.get("attempt_number", 1),
            retry_state=retry_state,
            failure_kind=failure_kind,
            failure_taxon=taxon_str,
            failure_taxon_category=taxon_cat,
            source=source,
            replay_run_id=replay_run_id,
            receipt_ids=receipt_ids,
            outbox_id=outbox_id,
            adapter_message_id=winner.get("adapter_message_id"),
            next_retry_at=_to_iso_or_none(next_retry_at_raw),
            error=error,
        )
        entries[key] = entry

        # Aggregate counts.
        status_counts[status] = status_counts.get(status, 0) + 1
        if taxon_str:
            taxon_counts[taxon_str] = taxon_counts.get(taxon_str, 0) + 1

    return DeliveryOutcomeLedger(
        entries=entries,
        aggregate_counts={
            "by_status": dict(sorted(status_counts.items())),
            "by_failure_taxon": dict(sorted(taxon_counts.items())),
        },
    )
