"""Core adapter contract package.

Re-exports all adapter contract types from
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
    OutboundNativeRefRecord,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterContract",
    "AdapterDeliveryResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "OutboundNativeRefRecord",
]
