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

Integration hooks
-----------------
* ``orphan_count`` field on :class:`ConvergenceSummary` — populated when
  linked to an orphan report.
* ``evidence_bundle_ref`` field — populated when attached to an
  EvidenceBundle.
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

from .orphans import build_orphan_report
from .summary import build_convergence_summary
from .types import (
    ConvergenceSeverity,
    ConvergenceSummary,
    DeliveryTargetConvergence,
    OrphanFinding,
    OrphanReport,
)

__all__ = [
    "ConvergenceSeverity",
    "DeliveryTargetConvergence",
    "ConvergenceSummary",
    "build_convergence_summary",
    "OrphanFinding",
    "OrphanReport",
    "build_orphan_report",
]
