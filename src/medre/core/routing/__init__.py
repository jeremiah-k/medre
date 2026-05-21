"""Core event routing package for the medre.

This package provides the routing layer that determines which events flow
to which adapters.  Package-level imports:

* From :mod:`~medre.core.routing.models`:
  ``RouteSource``, ``RouteDestination``, ``RouteTarget``, ``Route``.
* From :mod:`~medre.core.routing.router`:
  ``Router``, ``RouteConflictError``.
"""

from medre.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)
from medre.core.routing.router import (
    RouteConflictError,
    Router,
)

__all__ = [
    "Route",
    "RouteConflictError",
    "RouteDestination",
    "Router",
    "RouteSource",
    "RouteTarget",
]
