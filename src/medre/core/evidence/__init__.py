"""First-class read-only evidence bundle model and collector.

This package provides a deterministic, JSON-safe evidence bundle for a single
event, aggregating event summary, delivery receipts, parsed rendering evidence,
native refs, outbox state, replay/source context, warnings, and a generated
timestamp - without mutating runtime state.

Public API
----------
* :class:`EvidenceBundle` - frozen, JSON-safe evidence bundle.
* :class:`EvidenceCollector` - read-only collector backed by
  :class:`~medre.core.storage.backend.StorageBackend`.
"""

from medre.core.evidence.adapter_status import (
    OPERATOR_STATUSES,
    AdapterStatusEvidence,
    build_adapter_status_evidence,
    derive_operator_status,
)
from medre.core.evidence.bundle import EvidenceBundle, ReceiptSummary
from medre.core.evidence.collector import EvidenceCollector

__all__ = [
    "AdapterStatusEvidence",
    "EvidenceBundle",
    "EvidenceCollector",
    "OPERATOR_STATUSES",
    "ReceiptSummary",
    "build_adapter_status_evidence",
    "derive_operator_status",
]
