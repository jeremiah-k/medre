"""Adapter framework for the medre.

This package defines the adapter abstraction layer that bridges the
framework's canonical event model with external transports and
presentation platforms.

Quick-start imports::

    from medre.adapters import BaseAdapter, AdapterRole
    from medre.adapters import FakeTransportAdapter, FakeMatrixAdapter

Re-exported symbols
-------------------
* From :mod:`~medre.core.ports` and :mod:`~medre.core.adapter_base`:
  ``AdapterCapabilities``, ``AdapterCodec``, ``AdapterContext``,
  ``AdapterInfo``, ``AdapterRole``, ``BaseAdapter``.
* From :mod:`~medre.adapters.fake_transport`:
  ``FakeTransportAdapter``.
* From :mod:`~medre.adapters.fake_presentation`:
  ``FakePresentationAdapter``.
* From :mod:`~medre.adapters.fake_matrix`:
  ``FakeMatrixAdapter``.
* From :mod:`~medre.adapters.fake_meshtastic`:
  ``FakeMeshtasticAdapter``.
* From :mod:`~medre.adapters.fake_meshcore`:
  ``FakeMeshCoreAdapter``.
* From :mod:`~medre.adapters.fake_lxmf`:
  ``FakeLxmfAdapter``.
"""

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
from medre.adapters.fake_lxmf import FakeLxmfAdapter
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.fake_presentation import (
    FakePresentationAdapter,
    FaultyPresentationAdapter,
)
from medre.adapters.fake_transport import FakeTransportAdapter

__all__ = [
    # base
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterDeliveryResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "BaseAdapter",
    # fake adapters
    "FakeLxmfAdapter",
    "FakeMatrixAdapter",
    "FakeMeshCoreAdapter",
    "FakeMeshtasticAdapter",
    "FakePresentationAdapter",
    "FakeTransportAdapter",
    "FaultyPresentationAdapter",
]
