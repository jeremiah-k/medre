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

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    DeliveryIdentity,
    effective_generation,
    receipt_kind,
)
from medre.core.engine.pipeline.delivery_state import (
    NON_TERMINAL_OUTBOX_STATUSES,
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
        "receipt_kind": _getattr_or_get(rec, "receipt_kind"),
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
        "outbox_id": _getattr_or_get(rec, "outbox_id"),
        "sequence": _getattr_or_get(rec, "sequence", 0),
        "created_at": _getattr_or_get(rec, "created_at"),
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
        "active_attempt": _getattr_or_get(item, "active_attempt"),
        "next_attempt_at": _getattr_or_get(item, "next_attempt_at"),
        "error_summary": _getattr_or_get(item, "error_summary"),
        "replay_run_id": _getattr_or_get(item, "replay_run_id"),
        "metadata": _getattr_or_get(item, "metadata"),
        "receipt_id": _getattr_or_get(item, "receipt_id"),
        "parent_receipt_id": _getattr_or_get(item, "parent_receipt_id"),
        "updated_at": _getattr_or_get(item, "updated_at"),
        "created_at": _getattr_or_get(item, "created_at"),
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
_ACTIVE_STATUSES: frozenset[str] = NON_TERMINAL_OUTBOX_STATUSES


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


def _make_group_key(identity: DeliveryIdentity) -> str:
    """Encode the full event-scoped delivery identity deterministically."""
    return json.dumps(
        {
            "event_id": identity.event_id,
            "delivery_plan_id": identity.delivery_plan_id,
            "target_adapter": identity.target_adapter,
            "target_channel": identity.target_channel,
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOutcomeEntry:
    """Operator-facing state for one event-scoped delivery identity.

    Lifecycle authority and dispatch-attempt evidence are intentionally
    separate. ``lifecycle_status`` answers what MEDRE currently owns;
    ``latest_attempt_*`` answers what the latest transport execution did.
    ``source`` / ``replay_run_id`` describe the current mutable execution
    generation when one exists, otherwise the selected immutable evidence.
    """

    delivery_plan_id: str
    event_id: str
    route_id: str | None
    target_adapter: str
    target_channel: str | None
    lifecycle_status: str
    outbox_status: str | None
    authoritative_receipt_id: str | None
    authoritative_receipt_kind: str | None
    current_receipt_id: str | None
    current_receipt_kind: str | None
    current_receipt_status: str | None
    causative_receipt_id: str | None
    current_attempt_receipt_id: str | None
    current_attempt_status: str | None
    current_attempt_number: int | None
    latest_attempt_status: str | None
    latest_attempt_number: int | None
    ambiguous_outcome: bool
    delivery_strategy: str | None
    capability_field: str | None
    capability_level: str | None
    suppression_reason: str | None
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
            "lifecycle_status": self.lifecycle_status,
            "outbox_status": self.outbox_status,
            "authoritative_receipt_id": self.authoritative_receipt_id,
            "authoritative_receipt_kind": self.authoritative_receipt_kind,
            "current_receipt_id": self.current_receipt_id,
            "current_receipt_kind": self.current_receipt_kind,
            "current_receipt_status": self.current_receipt_status,
            "causative_receipt_id": self.causative_receipt_id,
            "current_attempt_receipt_id": self.current_attempt_receipt_id,
            "current_attempt_status": self.current_attempt_status,
            "current_attempt_number": self.current_attempt_number,
            "latest_attempt_status": self.latest_attempt_status,
            "latest_attempt_number": self.latest_attempt_number,
            "ambiguous_outcome": self.ambiguous_outcome,
            "delivery_strategy": self.delivery_strategy,
            "capability_field": self.capability_field,
            "capability_level": self.capability_level,
            "suppression_reason": self.suppression_reason,
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
    """Build an event-scoped operator ledger from immutable + mutable evidence.

    ``DeliveryAuthorityResolver`` is the only current-receipt selector. The
    ledger additionally exposes the latest attempt evidence and current outbox
    state so operators never have to infer one from the other.
    """
    receipt_records = [_normalize_receipt(receipt) for receipt in receipts]
    outbox_records = [_normalize_outbox_item(item) for item in outbox_items]
    resolver: DeliveryAuthorityResolver[dict[str, Any]] = DeliveryAuthorityResolver(
        receipt_records,
        outbox_records,
    )

    if not resolver.identities:
        return DeliveryOutcomeLedger(
            entries={},
            aggregate_counts={"by_status": {}, "by_failure_taxon": {}},
        )

    entries: dict[str, DeliveryOutcomeEntry] = {}
    status_counts: dict[str, int] = {}
    taxon_counts: dict[str, int] = {}

    for snapshot in resolver.ordered_snapshots():
        identity = snapshot.identity
        history = list(snapshot.receipts)
        authority = snapshot.authoritative_receipt
        outbox = snapshot.current_outbox
        current_receipt = snapshot.current_receipt
        current_attempt = snapshot.current_attempt
        latest_attempt = snapshot.latest_attempt

        authority_kind = receipt_kind(authority) if authority is not None else None
        lifecycle_status = str(
            (outbox or {}).get("status") or (authority or {}).get("status") or "unknown"
        )
        outbox_status = str(outbox.get("status")) if outbox is not None else None

        # Every operator-facing field below must describe one coherent execution
        # generation.  When mutable outbox state exists, older authoritative
        # receipts remain immutable history only; they must not donate failure,
        # capability, retry, or transport evidence to the newer generation.
        if outbox is not None:
            generation_evidence = current_attempt or outbox
            failure_kind = (current_attempt or {}).get("failure_kind") or outbox.get(
                "failure_kind"
            )
            error = (current_attempt or {}).get("error") or outbox.get("error_summary")
            rendering_evidence = (current_attempt or {}).get("rendering_evidence")
            next_retry_raw = (current_attempt or {}).get("next_retry_at") or outbox.get(
                "next_attempt_at"
            )
            current_attempt_number = effective_generation(outbox)
            replay_run_id = outbox.get("replay_run_id")
            if current_attempt is not None:
                source = str(current_attempt.get("source") or "live")
                replay_run_id = current_attempt.get("replay_run_id") or replay_run_id
            elif outbox.get("active_attempt") is not None:
                # A durable active-attempt reservation is itself proof that the
                # current dispatch mechanism is retry, even before that attempt
                # emits immutable receipt evidence. Replay origin remains
                # orthogonal on replay_run_id.
                source = "retry"
            elif replay_run_id:
                source = "replay"
            else:
                source = "live"
        else:
            generation_evidence = authority or latest_attempt or {}
            failure_kind = generation_evidence.get("failure_kind")
            error = generation_evidence.get("error")
            rendering_evidence = generation_evidence.get("rendering_evidence")
            next_retry_raw = generation_evidence.get("next_retry_at")
            current_attempt_number = generation_evidence.get("attempt_number")
            source = str(generation_evidence.get("source") or "live")
            replay_run_id = generation_evidence.get("replay_run_id")

        taxon = resolve_taxon(
            failure_kind=failure_kind,
            error=error,
            status=lifecycle_status,
        )
        taxon_str = taxon.value if taxon else None
        taxon_cat = taxon_category(taxon) if taxon else None
        cap = _derive_capability_fields(
            error=error,
            rendering_evidence=rendering_evidence,
            failure_kind=failure_kind,
            status=str(
                (current_attempt or generation_evidence).get("status")
                or lifecycle_status
            ),
        )

        provenance = generation_evidence
        if source == "live":
            replay_run_id = None
        receipt_ids = sorted(
            str(receipt.get("receipt_id"))
            for receipt in history
            if receipt.get("receipt_id")
        )

        entry = DeliveryOutcomeEntry(
            delivery_plan_id=identity.delivery_plan_id,
            event_id=identity.event_id,
            route_id=provenance.get("route_id") or None,
            target_adapter=identity.target_adapter,
            target_channel=identity.target_channel,
            lifecycle_status=lifecycle_status,
            outbox_status=outbox_status,
            authoritative_receipt_id=(authority or {}).get("receipt_id"),
            authoritative_receipt_kind=authority_kind,
            current_receipt_id=(current_receipt or {}).get("receipt_id"),
            current_receipt_kind=(
                receipt_kind(current_receipt) if current_receipt is not None else None
            ),
            current_receipt_status=(current_receipt or {}).get("status"),
            causative_receipt_id=(
                (snapshot.causative_receipt or {}).get("receipt_id")
                or (authority or {}).get("parent_receipt_id")
                if authority_kind == "lifecycle"
                else None
            ),
            current_attempt_receipt_id=(current_attempt or {}).get("receipt_id"),
            current_attempt_status=(current_attempt or {}).get("status"),
            current_attempt_number=current_attempt_number,
            latest_attempt_status=(latest_attempt or {}).get("status"),
            latest_attempt_number=(latest_attempt or {}).get("attempt_number"),
            ambiguous_outcome=(
                (outbox or {}).get("failure_kind_detail") == "dispatch_outcome_unknown"
            ),
            delivery_strategy=cap.get("delivery_strategy"),
            capability_field=cap.get("capability_field"),
            capability_level=cap.get("capability_level"),
            suppression_reason=cap.get("suppression_reason"),
            retry_state=_derive_retry_state(
                lifecycle_status,
                next_retry_raw,
                failure_kind,
            ),
            failure_kind=failure_kind,
            failure_taxon=taxon_str,
            failure_taxon_category=taxon_cat,
            source=source,
            replay_run_id=replay_run_id,
            receipt_ids=receipt_ids,
            outbox_id=(outbox or {}).get("outbox_id"),
            adapter_message_id=(current_attempt or generation_evidence).get(
                "adapter_message_id"
            ),
            next_retry_at=_to_iso_or_none(next_retry_raw),
            error=error,
        )
        entries[_make_group_key(identity)] = entry
        status_counts[lifecycle_status] = status_counts.get(lifecycle_status, 0) + 1
        if taxon_str:
            taxon_counts[taxon_str] = taxon_counts.get(taxon_str, 0) + 1

    return DeliveryOutcomeLedger(
        entries=entries,
        aggregate_counts={
            "by_status": dict(sorted(status_counts.items())),
            "by_failure_taxon": dict(sorted(taxon_counts.items())),
        },
    )
