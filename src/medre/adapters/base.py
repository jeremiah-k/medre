"""Abstract base classes and value types for the adapter framework.

Re-export shim — definitions moved to ``medre.core.ports`` and
``medre.core.adapter_base``.
"""

from __future__ import annotations

from medre.core.adapter_base import BaseAdapter
from medre.core.ports import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterDeliveryResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "BaseAdapter",
]
