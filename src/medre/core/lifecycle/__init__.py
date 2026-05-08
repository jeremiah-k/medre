"""Lifecycle management subsystem for the medre.

This package provides adapter lifecycle tracking with a formal state
machine and a manager for coordinating startup, shutdown, and health
checks across all registered adapters.

Re-exported symbols
-------------------
* From :mod:`~medre.core.lifecycle.states`:
  ``AdapterState``, ``VALID_TRANSITIONS``, ``InvalidStateTransition``,
  ``is_valid_transition``, ``require_valid_transition``.
* From :mod:`~medre.core.lifecycle.manager`:
  ``LifecycleManager``, ``ManagedAdapter``.
"""

from medre.core.lifecycle.manager import (
    LifecycleManager,
    ManagedAdapter,
)
from medre.core.lifecycle.states import (
    AdapterState,
    InvalidStateTransition,
    VALID_TRANSITIONS,
    is_valid_transition,
    require_valid_transition,
)

__all__ = [
    "AdapterState",
    "InvalidStateTransition",
    "LifecycleManager",
    "ManagedAdapter",
    "VALID_TRANSITIONS",
    "is_valid_transition",
    "require_valid_transition",
]
