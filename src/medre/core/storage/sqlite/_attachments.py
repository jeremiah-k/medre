"""Retained attachment content access for ``SQLiteStorage``.

Loading is association-scoped: an event may only read the content it was
admitted with, and every load re-verifies size and digest.  Missing or
corrupt content fails explicitly with a stable reason — there is never an
implicit refetch, source-key recovery, or substitution.

``StorageAttachmentAccess`` adapts the storage methods to the transport
neutral :class:`~medre.core.ingress.content.AttachmentContentAccess`
protocol, refusing loads while generic attachment policy is disabled.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Any

from medre.core.ingress import (
    AttachmentPolicyState,
    StoredAttachmentContent,
)
from medre.core.ingress.content import AttachmentContentUnavailableError

_SELECT_ASSOCIATION = """
SELECT a.content_ref AS association_ref, b.content_ref AS blob_ref,
       b.size_bytes, b.data
FROM event_attachment_associations a
LEFT JOIN attachment_blobs b ON b.content_ref = a.content_ref
WHERE a.event_id = ?
"""


class _AttachmentsMixin:
    """Retained-content read methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def load_attachment_content(
        self, event_id: str, content_ref: str
    ) -> StoredAttachmentContent:
        """Return verified retained bytes for one canonical event.

        The stored event/content association is the only authority: a
        *content_ref* that does not match the event's association is a
        forged reference and fails with ``association_missing``.  Blob
        absence fails with ``content_missing``; a digest or size mismatch
        fails with ``integrity_failed``.  Never refetches or substitutes.
        """
        row = await self._read_one(_SELECT_ASSOCIATION, (event_id,))
        if row is None or row["association_ref"] != content_ref:
            raise AttachmentContentUnavailableError(
                f"no retained attachment association for event {event_id} "
                f"matching the requested content reference",
                reason="association_missing",
            )
        if row["blob_ref"] is None or row["data"] is None:
            raise AttachmentContentUnavailableError(
                f"retained attachment bytes are missing for event {event_id}",
                reason="content_missing",
            )
        data = bytes(row["data"])
        size_bytes = int(row["size_bytes"])
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != size_bytes or f"sha256:{digest}" != content_ref:
            raise AttachmentContentUnavailableError(
                f"retained attachment content failed integrity verification "
                f"for event {event_id}",
                reason="integrity_failed",
            )
        return StoredAttachmentContent(
            content_ref=content_ref,
            size_bytes=size_bytes,
            data=data,
        )

    async def attachment_retained_bytes(self) -> int:
        """Return the total size of unique retained attachment bytes."""
        row = await self._read_one(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM attachment_blobs"
        )
        return int(row["total"]) if row is not None else 0


class StorageAttachmentAccess:
    """Policy-gated association-scoped adapter over storage content reads.

    Implements the
    :class:`~medre.core.ingress.content.AttachmentContentAccess` protocol
    injected into adapters via ``AdapterContext.attachments``.  Disabling
    the generic attachment policy blocks loads of previously stored files
    too — outbound transfers must stop when policy says no, even for
    content that is still durably retained.
    """

    def __init__(
        self,
        storage: _AttachmentsMixin,
        policy: AttachmentPolicyState,
    ) -> None:
        self._storage = storage
        self._policy = policy

    @property
    def policy(self) -> AttachmentPolicyState:
        """Return the immutable policy snapshot gating this access."""
        return self._policy

    async def load_for_event(
        self, event_id: str, content_ref: str
    ) -> StoredAttachmentContent:
        """Return verified retained bytes for *event_id*.

        Raises :class:`AttachmentContentUnavailableError` with a stable
        reason (``policy_disabled``, ``association_missing``,
        ``content_missing``, ``integrity_failed``); never substitutes.
        """
        if not self._policy.enabled:
            raise AttachmentContentUnavailableError(
                "attachment policy is disabled; retained content cannot be "
                "transferred",
                reason="policy_disabled",
            )
        return await self._storage.load_attachment_content(event_id, content_ref)
