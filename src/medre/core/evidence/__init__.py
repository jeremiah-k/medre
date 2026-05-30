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
from medre.core.evidence.failure_taxonomy import (
    FAILURE_KIND_TO_TAXON,
    FailureTaxon,
    compute_retryable,
    derive_failure_kind_detail,
    resolve_taxon,
    taxon_category,
)
from medre.core.evidence.tiers import (
    EVIDENCE_TIER_UNKNOWN,
    EvidenceTier,
    infer_evidence_tier,
    tier_is_live,
)

__all__ = [
    "AdapterStatusEvidence",
    "EVIDENCE_TIER_UNKNOWN",
    "EvidenceBundle",
    "EvidenceCollector",
    "EvidenceTier",
    "FAILURE_KIND_TO_TAXON",
    "FailureTaxon",
    "OPERATOR_STATUSES",
    "ReceiptSummary",
    "build_adapter_status_evidence",
    "compute_retryable",
    "derive_failure_kind_detail",
    "derive_operator_status",
    "infer_evidence_tier",
    "resolve_taxon",
    "taxon_category",
    "tier_is_live",
]
