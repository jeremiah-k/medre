"""Matrix adapter exception hierarchy.

All Matrix-specific errors inherit from :class:`MatrixError` so that
callers can catch the entire family with a single ``except MatrixError``
clause.

Hierarchy::

    MatrixError
    ├── MatrixConnectionError   — connection / authentication failures
    ├── MatrixSendError         — message send failures
    └── MatrixCodecError        — decode failures
"""

from __future__ import annotations

MATRIX_PERMANENT_ERRCODES: frozenset[str] = frozenset(
    {
        "M_FORBIDDEN",
        "M_NOT_FOUND",
        "M_UNAUTHORIZED",
        "M_UNKNOWN_TOKEN",
        "M_USER_DEACTIVATED",
        "M_BAD_JSON",
        "M_NOT_JSON",
        "M_INVALID_PARAM",
        "M_DUPLICATE_ANNOTATION",
    }
)
# ``M_UNKNOWN`` is deliberately absent: the Matrix client-server spec says
# servers use it for any unrecognized failure and clients should prefer the
# HTTP status. A 5xx-triggered M_UNKNOWN is transient (server bug or
# overload), so classifying it as unconditionally permanent terminates
# bounded recovery on recoverable failures. Treating it as transient is
# safe: retries are capped by the retry policy / room-key request budget.


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


class MatrixCodecError(MatrixError):
    """Raised when decode operations fail."""
