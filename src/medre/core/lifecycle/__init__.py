"""Lifecycle management subsystem for medre.

This package provides adapter lifecycle tracking with a formal state
machine defining legal transitions between adapter states.

Re-exported symbols
-------------------
* From :mod:`~medre.core.lifecycle.states`:
  ``AdapterState``, ``VALID_TRANSITIONS``, ``InvalidStateTransition``,
  ``is_valid_transition``, ``require_valid_transition``.
"""

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
    "VALID_TRANSITIONS",
    "is_valid_transition",
    "require_valid_transition",
]
