"""Core event routing package for the medre.

This package provides the routing layer that determines which events flow
to which adapters.  Package-level imports:

* From :mod:`~medre.core.routing.models`:
  ``RouteSource``, ``RouteDestination``, ``RouteTarget``, ``Route``.
* From :mod:`~medre.core.routing.router`:
  ``Router``, ``RouteConflictError``, ``find_route_conflicts``.
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
    find_route_conflicts,
)

__all__ = [
    "Route",
    "RouteConflictError",
    "RouteDestination",
    "Router",
    "find_route_conflicts",
    "RouteSource",
    "RouteTarget",
]
