"""Adapter framework for the medre.

This package defines the adapter abstraction layer that bridges the
framework's canonical event model with external transports and
presentation platforms.

Quick-start imports::

    from medre.adapters import BaseAdapter, AdapterRole
    from medre.adapters import FakeTransportAdapter, FakeMatrixAdapter

Re-exported symbols
-------------------
* From :mod:`~medre.adapters.base`:
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

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
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
    "AdapterRole",
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
