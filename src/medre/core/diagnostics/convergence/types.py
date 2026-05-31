"""Public data types for recovery convergence diagnostics.

Canonical enums, dataclasses, and finding-kind constants shared across
the convergence package.  Every field is JSON-safe (``str | int | float |
bool | None | list | dict``); ``datetime`` values are converted to
ISO-8601 strings upstream.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, fields
from typing import Any

__all__ = [
    "ConvergenceSeverity",
    "DeliveryTargetConvergence",
    "ConvergenceSummary",
    "OrphanFinding",
    "OrphanReport",
    "KIND_ORPHANED_OUTBOX",
    "KIND_ORPHANED_PARENT_RECEIPT",
    "KIND_CROSS_PLAN_PARENT",
    "KIND_CROSS_EVENT_PARENT",
    "KIND_MISSING_DELIVERY_PLAN_ID",
    "KIND_DEAD_LETTERED_RETRYABLE_MISMATCH",
]


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
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
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
        Count of orphan/invalid-lineage findings.  ``None`` in the
        convergence summary because orphan data is reported separately
        via :class:`OrphanReport` on the evidence bundle.  The orphan
        report is the authoritative source for orphan counts.
    evidence_bundle_ref:
        Reference to the EvidenceBundle for cross-referencing.
        ``None`` until the convergence summary is attached to an
        evidence bundle.
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
# Orphan / invalid lineage detection models
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
        return {
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
        }


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
