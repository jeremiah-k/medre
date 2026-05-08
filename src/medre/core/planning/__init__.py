"""Core delivery planning package for the medre.

This package provides the planning layer that determines *how* routed
events are delivered to adapters.  Public symbols:

* From :mod:`~medre.core.planning.delivery_plan`:
  ``DeliveryPlan``, ``DeliveryStrategy``, ``RetryPolicy``.
* From :mod:`~medre.core.planning.fallback_resolution`:
  ``FallbackResolver``.
* From :mod:`~medre.core.planning.relation_resolution`:
  ``RelationResolver``.
"""

from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver

__all__ = [
    "DeliveryPlan",
    "DeliveryStrategy",
    "FallbackResolver",
    "RelationResolver",
    "RetryPolicy",
]
