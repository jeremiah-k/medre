"""Core delivery planning package for the medre.

This package provides the planning layer that determines *how* routed
events are delivered to adapters.  Package-level imports:

* From :mod:`~medre.core.planning.delivery_plan`:
  ``DeliveryPlan``, ``DeliveryStrategy``, ``RetryPolicy``,
  ``DeliveryFailureKind``, ``RetryExecutor``, ``DeliveryOutcome``.
* From :mod:`~medre.core.planning.fallback_resolution`:
  ``FallbackResolver``.
* From :mod:`~medre.core.planning.relation_resolution`:
  ``RelationResolver``.
* From :mod:`~medre.core.planning.capabilities`:
  ``capability_unsupported``.
* From :mod:`~medre.core.planning.capability_decision`:
  ``CapabilityDecision``, ``CapabilityDecisionResolver``.
"""

from medre.core.planning.capabilities import capability_unsupported
from medre.core.planning.capability_decision import (
    CapabilityDecision,
    CapabilityDecisionResolver,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver

__all__ = [
    "CapabilityDecision",
    "CapabilityDecisionResolver",
    "DeliveryFailureKind",
    "DeliveryOutcome",
    "DeliveryPlan",
    "DeliveryStrategy",
    "FallbackResolver",
    "RelationResolver",
    "RetryExecutor",
    "RetryPolicy",
    "capability_unsupported",
]
