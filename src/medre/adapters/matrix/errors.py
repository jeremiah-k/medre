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

import math
from typing import Any

from medre.core.ingress.types import DurableIngressDeferredError

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


def retry_after_seconds_from_ms(value: Any) -> float | None:
    """Normalize a Matrix ``retry_after_ms`` value to non-negative seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric / 1000.0


def is_nio_rate_limited_response(response: Any) -> bool:
    """Return whether a nio response represents Matrix rate limiting."""
    if hasattr(response, "event_id"):
        return False
    errcode = getattr(response, "errcode", None) or ""
    if isinstance(errcode, str) and errcode.upper() == "M_LIMIT_EXCEEDED":
        return True
    status = getattr(response, "status_code", None)
    if isinstance(status, str) and status.upper() == "M_LIMIT_EXCEEDED":
        return True
    if status == 429:
        return True
    transport_response = getattr(response, "transport_response", None)
    return getattr(transport_response, "status", None) == 429


# Internal durable-admission metadata. MatrixSession injects the count recorded
# for the stable native (room,event) identity before the adapter decodes a
# fresh canonical UUID. The key is intentionally adapter-private and never
# enters persisted canonical event metadata.
MATRIX_ATTACHMENT_FETCH_DEFERRAL_COUNT_KEY = (
    "_medre_attachment_fetch_deferral_count"
)
MATRIX_ATTACHMENT_FETCH_MAX_DEFERRALS = 3


class MatrixAttachmentFetchDeferredError(DurableIngressDeferredError):
    """Retryable Matrix media acquisition that must keep the native event pending.

    This marker lets :class:`MatrixSession` persist only attachment-fetch
    deferrals at the native event/checkpoint boundary without changing the
    semantics of other :class:`DurableIngressDeferredError` users.
    """


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


class MatrixMediaError(MatrixError):
    """Base error for native media transfers."""


class MatrixMediaUnavailableError(MatrixMediaError):
    """Permanent media input problem; retrying the same input cannot help.

    ``reason`` carries a stable secret-free code matching the canonical
    attachment descriptor's ``unavailable_reason`` vocabulary subset used
    for ingress rejection: ``malformed_source``, ``unsupported_source``,
    ``integrity_failed``, ``oversized``, ``content_missing``.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class MatrixMediaTransientError(MatrixMediaError):
    """Retryable media acquisition/transient transfer failure."""
