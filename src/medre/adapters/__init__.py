"""Adapter framework for the medre.

This package defines the adapter abstraction layer that bridges the
framework's canonical event model with external transports and
presentation platforms.

Quick-start imports::

    from medre.adapters import BaseAdapter, AdapterRole
    from medre.adapters import FakeTransportAdapter

Re-exported symbols
-------------------
* From :mod:`~medre.adapters.base`:
  ``AdapterCapabilities``, ``AdapterCodec``, ``AdapterContext``,
  ``AdapterInfo``, ``AdapterRole``, ``BaseAdapter``.
* From :mod:`~medre.adapters.fake_transport`:
  ``FakeTransportAdapter``.
* From :mod:`~medre.adapters.fake_presentation`:
  ``FakePresentationAdapter``.
"""

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)
from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter

__all__ = [
    # base
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterInfo",
    "AdapterRole",
    "BaseAdapter",
    # fake adapters
    "FakePresentationAdapter",
    "FakeTransportAdapter",
]
