"""Core delivery planning package for the meshnet framework.

This package provides the planning layer that determines *how* routed
events are delivered to adapters.  Public symbols:

* From :mod:`~meshnet_framework.core.planning.delivery_plan`:
  ``DeliveryPlan``, ``DeliveryStrategy``, ``RetryPolicy``.
* From :mod:`~meshnet_framework.core.planning.fallback_resolution`:
  ``FallbackResolver``.
* From :mod:`~meshnet_framework.core.planning.relation_resolution`:
  ``RelationResolver``.
"""

from meshnet_framework.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from meshnet_framework.core.planning.fallback_resolution import FallbackResolver
from meshnet_framework.core.planning.relation_resolution import RelationResolver

__all__ = [
    "DeliveryPlan",
    "DeliveryStrategy",
    "FallbackResolver",
    "RelationResolver",
    "RetryPolicy",
]
