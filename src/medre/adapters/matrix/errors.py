"""Matrix adapter exception hierarchy.

All Matrix-specific errors inherit from :class:`MatrixError` so that
callers can catch the entire family with a single ``except MatrixError``
clause.

Hierarchy::

    MatrixError
    ├── MatrixConnectionError   — connection / authentication failures
    ├── MatrixSendError         — message send failures
    ├── MatrixConfigError       — invalid configuration (also ValueError)
    └── MatrixCodecError        — decode failures
"""
from __future__ import annotations


class MatrixError(Exception):
    """Base exception for all Matrix adapter errors."""


class MatrixConnectionError(MatrixError):
    """Raised when the adapter cannot connect or authenticate with the
    homeserver."""


class MatrixSendError(MatrixError):
    """Raised when a message send operation fails.

    Parameters
    ----------
    transient:
        ``True`` (default) if the error may succeed on retry;
        ``False`` for permanent failures (e.g. encrypted-room rejection,
        startup state missing).
    """

    transient: bool

    def __init__(self, *args: object, transient: bool = True) -> None:
        self.transient = transient
        super().__init__(*args)


class MatrixConfigError(MatrixError, ValueError):
    """Raised when the Matrix configuration is invalid.

    Inherits from both :class:`MatrixError` and :class:`ValueError` so
    that it is caught by either ``except MatrixError`` or
    ``except ValueError``.
    """


class MatrixCodecError(MatrixError):
    """Raised when decode operations fail."""
