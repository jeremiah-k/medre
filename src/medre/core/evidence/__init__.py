"""First-class read-only evidence bundle model and collector.

This package provides a deterministic, JSON-safe evidence bundle for a single
event, aggregating event summary, delivery receipts, parsed rendering evidence,
native refs, outbox state, replay/source context, warnings, and a generated
timestamp — without mutating runtime state.

Public API
----------
* :class:`EvidenceBundle` — frozen, JSON-safe evidence bundle.
* :class:`EvidenceCollector` — read-only collector backed by
  :class:`~medre.core.storage.backend.StorageBackend`.
"""

from medre.core.evidence.bundle import EvidenceBundle, ReceiptSummary

__all__ = [
    "EvidenceBundle",
    "ReceiptSummary",
]

# Lazy import to avoid circular dependency at module level.
# EvidenceCollector is imported from collector.py on first access.


def __getattr__(name: str) -> object:
    if name == "EvidenceCollector":
        from medre.core.evidence.collector import EvidenceCollector

        return EvidenceCollector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
