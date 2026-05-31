"""First-class read-only evidence bundle model, collector, and helpers.

This package provides a deterministic, JSON-safe evidence bundle for a single
event, aggregating event summary, delivery receipts, parsed rendering evidence,
native refs, outbox state, replay/source context, warnings, and a generated
timestamp — without mutating runtime state.

It also exposes pure helper models for delivery outcome ledgers, retry/outbox
accountability, adapter status evidence, shutdown evidence, and evidence tier
classification.

Internal package — no stable public API guarantees (MEDRE pre-release).
"""

from medre.core.evidence.adapter_status import (
    OPERATOR_STATUSES,
    AdapterStatusEvidence,
    build_adapter_status_evidence,
    derive_operator_status,
)
from medre.core.evidence.bundle import EvidenceBundle, ReceiptSummary
from medre.core.evidence.collector import EvidenceCollector
from medre.core.evidence.delivery_ledger import (
    DeliveryOutcomeEntry,
    DeliveryOutcomeLedger,
    build_delivery_outcome_ledger,
)
from medre.core.evidence.failure_taxonomy import (
    FAILURE_KIND_TO_TAXON,
    FailureTaxon,
    compute_retryable,
    derive_failure_kind_detail,
    resolve_taxon,
    taxon_category,
)
from medre.core.evidence.retry_outbox import (
    RetryOutboxItemSummary,
    RetryOutboxSummary,
    build_retry_outbox_summary,
)
from medre.core.evidence.shutdown import (
    ShutdownEvidence,
    ShutdownStatus,
    build_shutdown_evidence,
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
    "RetryOutboxItemSummary",
    "RetryOutboxSummary",
    "ShutdownEvidence",
    "ShutdownStatus",
    "build_adapter_status_evidence",
    "build_delivery_outcome_ledger",
    "build_retry_outbox_summary",
    "build_shutdown_evidence",
    "compute_retryable",
    "derive_failure_kind_detail",
    "derive_operator_status",
    "infer_evidence_tier",
    "resolve_taxon",
    "taxon_category",
    "tier_is_live",
    "DeliveryOutcomeEntry",
    "DeliveryOutcomeLedger",
]
