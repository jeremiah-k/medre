"""Core adapter and delivery boundary contracts."""

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterContract,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.contracts.delivery import (
    ADAPTER_HANDOFF_DISPOSITION_VALUES,
    DEFERRED_FAILURE_OUTCOME_VALUES,
    AdapterHandoffDisposition,
    AdapterHandoffResult,
    DeferredFailureOutcome,
    DeferredHandoffCompleted,
    DeferredHandoffFailed,
    DeliveryFeedback,
    PostHandoffObservation,
)
from medre.core.events.delivery import DeliveryConfirmationLevel

__all__ = [
    "ADAPTER_HANDOFF_DISPOSITION_VALUES",
    "DEFERRED_FAILURE_OUTCOME_VALUES",
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterContract",
    "AdapterHandoffDisposition",
    "AdapterHandoffResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "DeferredFailureOutcome",
    "DeferredHandoffCompleted",
    "DeferredHandoffFailed",
    "DeliveryConfirmationLevel",
    "DeliveryFeedback",
    "PostHandoffObservation",
]
