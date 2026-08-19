"""Core adapter contract package.

Package-level imports of all adapter contract types from
:mod:`medre.core.contracts.adapter`.
"""

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
    DeliveryConfirmationLevel,
    OutboundNativeRefRecord,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterContract",
    "AdapterDeliveryResult",
    "AdapterInfo",
    "DeliveryConfirmationLevel",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "OutboundNativeRefRecord",
]
