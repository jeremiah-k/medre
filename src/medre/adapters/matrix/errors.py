"""Matrix adapter exception hierarchy.

All Matrix-specific errors inherit from :class:`MatrixError` so that
callers can catch the entire family with a single ``except MatrixError``
clause.

Hierarchy::

    MatrixError
    ├── MatrixConnectionError   — connection / authentication failures
    ├── MatrixSendError         — message send failures
    ├── MatrixCodecError        — decode failures
    └── MatrixProvisionError    — provisioning / verification failures
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


class MatrixProvisionError(MatrixError):
    """Raised when room/space provisioning or its state verification fails.

    Partial provisioning is not generally reversible in Matrix.  When a
    failure occurs after resource creation, the error preserves the generated
    room IDs and completed steps so an operator can reconcile the existing
    resources instead of blindly creating another pair.
    """

    space_id: str | None
    room_id: str | None
    completed_steps: tuple[str, ...]
    invited_space_user_ids: tuple[str, ...]
    invited_room_user_ids: tuple[str, ...]

    def __init__(
        self,
        message: str,
        *,
        space_id: str | None = None,
        room_id: str | None = None,
        completed_steps: tuple[str, ...] = (),
        invited_space_user_ids: tuple[str, ...] = (),
        invited_room_user_ids: tuple[str, ...] = (),
    ) -> None:
        self.space_id = space_id
        self.room_id = room_id
        self.completed_steps = completed_steps
        self.invited_space_user_ids = invited_space_user_ids
        self.invited_room_user_ids = invited_room_user_ids
        if space_id is not None or room_id is not None:
            message = (
                f"{message}; partial provisioning preserved "
                f"space_id={space_id!r} room_id={room_id!r} "
                f"completed_steps={list(completed_steps)!r}. "
                "Reconcile these resources before retrying."
            )
        super().__init__(message)
