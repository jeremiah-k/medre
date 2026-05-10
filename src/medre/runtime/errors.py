"""Runtime error hierarchy for the MEDRE framework.

Defines the exception classes raised during runtime construction,
startup, and shutdown.  All runtime-layer errors inherit from
:class:`RuntimeError` so callers can catch the entire family with a
single ``except`` clause.

Hierarchy::

    RuntimeError
    +-- RuntimeConfigError
    +-- RuntimeStartupError
    |   +-- AdapterStartupError
    +-- RuntimeShutdownError
"""

from __future__ import annotations

__all__ = [
    "RuntimeError",
    "RuntimeConfigError",
    "RuntimeStartupError",
    "RuntimeShutdownError",
    "AdapterStartupError",
]


class RuntimeError(Exception):
    """Base exception for all runtime-layer errors."""


class RuntimeConfigError(RuntimeError):
    """Raised when the runtime configuration is invalid or incomplete."""


class RuntimeStartupError(RuntimeError):
    """Raised when a subsystem or adapter fails to start."""


class RuntimeShutdownError(RuntimeError):
    """Raised when a subsystem or adapter fails to shut down cleanly."""


class AdapterStartupError(RuntimeStartupError):
    """Raised when a specific adapter fails to start.

    Attributes
    ----------
    adapter_id:
        Identifier of the adapter that failed to start.
    """

    def __init__(self, adapter_id: str, message: str = "") -> None:
        self.adapter_id = adapter_id
        detail = f"adapter {adapter_id!r}: {message}" if message else f"adapter {adapter_id!r}"
        super().__init__(f"Failed to start {detail}")
