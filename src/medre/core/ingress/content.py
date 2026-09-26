"""Runtime attachment seam: policy view, transfer permits, content access.

This module defines the narrow transport-neutral seam injected into
adapters at start-up.  It bundles three runtime-owned concerns:

* :class:`AttachmentPolicyState` — the validated generic policy snapshot
  (enabled flag, per-attachment byte cap, transfer deadline).
* :class:`AttachmentTransferPermits` — the runtime-wide bounded concurrency
  gate for binary transfers (downloads and uploads).
* :class:`AttachmentContentAccess` — the protocol for reading retained
  bytes, scoped to the canonical event that owns them.

Adapters receive the bundle through ``AdapterContext.attachments``.  The
seam never exposes generic database access, credentials, or another
adapter's session; loading is always scoped to one event/content pair that
storage has associated durably.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AttachmentContentError(RuntimeError):
    """Base error for retained attachment content access failures."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class AttachmentContentUnavailableError(AttachmentContentError):
    """Raised when retained bytes for an event cannot be loaded.

    ``reason`` is one of the stable secret-free codes such as
    ``"content_missing"``, ``"association_missing"``, ``"integrity_failed"``,
    or ``"policy_disabled"``.  Loading never triggers an implicit refetch,
    source-key recovery, or substitution.
    """


@dataclass(frozen=True)
class StoredAttachmentContent:
    """Retained bytes returned by the content access seam."""

    content_ref: str
    size_bytes: int
    data: bytes


@runtime_checkable
class AttachmentContentAccess(Protocol):
    """Read retained attachment bytes scoped to one canonical event.

    Implementations must verify the stored event/content association,
    re-verify content integrity, refuse loads while generic policy is
    disabled, and fail explicitly (never substitute) when bytes are
    missing or corrupt.
    """

    async def load_for_event(
        self, event_id: str, content_ref: str
    ) -> StoredAttachmentContent:
        """Return verified retained bytes for *event_id*.

        Raises :class:`AttachmentContentUnavailableError` with a stable
        reason when the association does not exist, the blob is missing,
        integrity verification fails, or policy is disabled.
        """
        ...


@dataclass(frozen=True)
class AttachmentPolicyState:
    """Immutable generic attachment policy snapshot."""

    enabled: bool
    max_attachment_bytes: int
    transfer_timeout_seconds: float


@dataclass(frozen=True)
class AttachmentLimits:
    """Storage-side byte bounds for one atomic attachment admission.

    ``max_attachment_bytes`` bounds one primary attachment's measured
    length; ``max_retained_bytes`` bounds the total of unique retained
    bytes (duplicates deduplicate and never consume quota twice).  Both
    must be positive and finite; booleans are not integers.
    """

    max_attachment_bytes: int = 10_485_760
    max_retained_bytes: int = 268_435_456

    def __post_init__(self) -> None:
        for name in ("max_attachment_bytes", "max_retained_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")


class AttachmentTransferPermitTimeoutError(TimeoutError):
    """Raised when a transfer permit could not be acquired in time."""


class AttachmentTransferPermits:
    """Runtime-wide bounded concurrency gate for binary transfers.

    ``acquire`` is an async context manager.  Permits must be acquired
    before allocating/reading a full blob or starting a download/upload,
    and are released on every exit path including errors, cancellation,
    and shutdown.  Acquisition waits at most ``acquire_timeout_seconds``
    so callers remain bounded; expiry is a transient failure the existing
    retry ownership classifies.
    """

    def __init__(
        self,
        *,
        max_concurrent: int,
        acquire_timeout_seconds: float,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        if acquire_timeout_seconds <= 0:
            raise ValueError(
                f"acquire_timeout_seconds must be > 0, got {acquire_timeout_seconds}"
            )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether the gate stopped accepting new acquisitions."""
        return self._closed

    def close(self) -> None:
        """Stop accepting new acquisitions (shutdown path)."""
        self._closed = True

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Hold one transfer permit for the duration of the block."""
        if self._closed:
            raise AttachmentTransferPermitTimeoutError(
                "attachment transfer permits are closed"
            )
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._acquire_timeout_seconds
            )
        except TimeoutError as exc:
            raise AttachmentTransferPermitTimeoutError(
                "attachment transfer permit acquisition timed out"
            ) from exc
        if self._closed:
            # ``close()`` may race a waiter that already passed the first
            # check.  Such a task was not an in-flight holder at shutdown,
            # so return the acquired slot and fail instead of starting a new
            # binary transfer after the gate closed.
            self._semaphore.release()
            raise AttachmentTransferPermitTimeoutError(
                "attachment transfer permits are closed"
            )
        try:
            yield
        finally:
            self._semaphore.release()


@dataclass(frozen=True)
class AttachmentRuntimeSeam:
    """The bundle injected into adapters via ``AdapterContext.attachments``."""

    policy: AttachmentPolicyState
    permits: AttachmentTransferPermits
    content: AttachmentContentAccess


__all__ = [
    "AttachmentContentAccess",
    "AttachmentContentError",
    "AttachmentContentUnavailableError",
    "AttachmentLimits",
    "AttachmentPolicyState",
    "AttachmentRuntimeSeam",
    "AttachmentTransferPermitTimeoutError",
    "AttachmentTransferPermits",
    "StoredAttachmentContent",
]
