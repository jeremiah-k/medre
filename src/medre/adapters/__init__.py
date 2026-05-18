"""Concrete and fake adapter implementations.

This package contains concrete transport/presentation adapter implementations
and simulated test doubles (fake adapters).  The adapter abstraction layer
(contract types) lives in ``medre.core.contracts.adapter``.

Quick-start imports::

    from medre.adapters import FakeTransportAdapter, FakeMatrixAdapter

Re-exported symbols
-------------------
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
    # fake adapters
    "FakeLxmfAdapter",
    "FakeMatrixAdapter",
    "FakeMeshCoreAdapter",
    "FakeMeshtasticAdapter",
    "FakePresentationAdapter",
    "FakeTransportAdapter",
    "FaultyPresentationAdapter",
]
