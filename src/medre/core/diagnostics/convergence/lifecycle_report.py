"""Canonical lifecycle convergence report serialization.

Produces JSON-safe dicts matching the ``LifecycleConvergenceReport`` schema.
"""

from __future__ import annotations

from typing import Any

from .types import OrphanFinding

__all__ = ["build_lifecycle_convergence_report_dict"]


_EMPTY_SEVERITY_COUNTS: dict[str, int] = {"safe": 0, "degraded": 0, "inconsistent": 0}


def build_lifecycle_convergence_report_dict(
    findings: list[OrphanFinding] | None,
) -> dict[str, Any]:
    """Build a lifecycle convergence report dict from findings.

    Produces a dict with keys ``findings``, ``total_findings``,
    ``severity_counts``, and ``worst_severity``, matching the
    ``LifecycleConvergenceReport`` JSON Schema shape.

    This is the canonical serialization helper — used by both
    ``EvidenceCollector`` and runtime evidence storage sections.
    """
    if not findings:
        return {
            "findings": [],
            "total_findings": 0,
            "severity_counts": _EMPTY_SEVERITY_COUNTS.copy(),
            "worst_severity": None,
        }
    severity_counts: dict[str, int] = _EMPTY_SEVERITY_COUNTS.copy()
    for f in findings:
        sev = f.severity
        if sev in severity_counts:
            severity_counts[sev] += 1
    if severity_counts.get("inconsistent", 0) > 0:
        worst = "inconsistent"
    elif severity_counts.get("degraded", 0) > 0:
        worst = "degraded"
    elif severity_counts.get("safe", 0) > 0:
        worst = "safe"
    else:
        worst = None
    return {
        "findings": [
            {
                "kind": f.kind,
                "severity": f.severity,
                "record_id": f.record_id,
                "record_type": f.record_type,
                "details": f.details,
                "extra": dict(f.extra),
            }
            for f in findings
        ],
        "total_findings": len(findings),
        "severity_counts": severity_counts,
        "worst_severity": worst,
    }
