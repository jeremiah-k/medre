"""Core event routing package for the meshnet framework.

This package provides the routing layer that determines which events flow
to which adapters.  Public symbols:

* From :mod:`~meshnet_framework.core.routing.models`:
  ``RouteSource``, ``RouteDestination``, ``RouteTarget``, ``Route``.
* From :mod:`~meshnet_framework.core.routing.router`:
  ``Router``, ``RouteConflictError``.
"""

from meshnet_framework.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)
from meshnet_framework.core.routing.router import (
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
