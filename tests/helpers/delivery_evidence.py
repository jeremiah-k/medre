"""Evidence wrapping helpers for delivery-execution test doubles.

``TargetDeliveryService.deliver_execution`` and
``PipelineRunner.deliver_execution_to_target`` return validated
``DeliveryExecutionEvidence``. Test doubles that stand in for those methods
wrap their receipt through :func:`evidence_for_receipt` so the doubled
surface matches the real execution contract.
"""

from __future__ import annotations

from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.events.canonical import DeliveryReceipt


def evidence_for_receipt(receipt: DeliveryReceipt) -> DeliveryExecutionEvidence:
    """Wrap one receipt as the primary evidence of an execution generation.

    Attempt receipts are the primary attempt evidence; lifecycle receipts
    (for example suppression or dead-letter transitions) are the lifecycle
    authority of the same generation. The evidence constructor validates
    the receipt kind either way.
    """
    if receipt.receipt_kind == "attempt":
        return DeliveryExecutionEvidence(attempt_receipt=receipt)
    return DeliveryExecutionEvidence(authority_receipt=receipt)
