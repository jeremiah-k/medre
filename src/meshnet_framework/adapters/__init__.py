"""Adapter framework for the meshnet framework.

This package defines the adapter abstraction layer that bridges the
framework's canonical event model with external transports and
presentation platforms.

Quick-start imports::

    from meshnet_framework.adapters import BaseAdapter, AdapterRole
    from meshnet_framework.adapters import FakeTransportAdapter

Re-exported symbols
-------------------
* From :mod:`~meshnet_framework.adapters.base`:
  ``AdapterCapabilities``, ``AdapterCodec``, ``AdapterContext``,
  ``AdapterInfo``, ``AdapterRole``, ``BaseAdapter``.
* From :mod:`~meshnet_framework.adapters.fake_transport`:
  ``FakeTransportAdapter``.
* From :mod:`~meshnet_framework.adapters.fake_presentation`:
  ``FakePresentationAdapter``.
"""

from meshnet_framework.adapters.base import (
    AdapterCapabilities,
    AdapterCodec,
    AdapterContext,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)
from meshnet_framework.adapters.fake_presentation import FakePresentationAdapter
from meshnet_framework.adapters.fake_transport import FakeTransportAdapter

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
