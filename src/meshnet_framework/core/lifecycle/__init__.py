"""Lifecycle management subsystem for the meshnet framework.

This package provides adapter lifecycle tracking with a formal state
machine and a manager for coordinating startup, shutdown, and health
checks across all registered adapters.

Re-exported symbols
-------------------
* From :mod:`~meshnet_framework.core.lifecycle.states`:
  ``AdapterState``, ``VALID_TRANSITIONS``, ``InvalidStateTransition``,
  ``is_valid_transition``, ``require_valid_transition``.
* From :mod:`~meshnet_framework.core.lifecycle.manager`:
  ``LifecycleManager``, ``ManagedAdapter``.
"""

from meshnet_framework.core.lifecycle.manager import (
    LifecycleManager,
    ManagedAdapter,
)
from meshnet_framework.core.lifecycle.states import (
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
