"""Persist one immutable canonical body admission fact with the event.

No read grant, text erasure, or inferred expiration is provided by this layer.
The fact separates an event without a body from an absent event without
mistaking empty but explicitly present text for missing content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import StorageError

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent


def body_admission_op(
    event: CanonicalEvent, recorded_at: str
) -> tuple[str, tuple[Any, ...]]:
    """Return an atomic event-admission op, never inspecting external copies."""
    payload = event.payload
    # Preserve the distinction between explicit empty text and absent text.
    # A malformed non-string value cannot be pronounced retained.
    present = isinstance(payload.get("body"), str) or (
        "body" not in payload and isinstance(payload.get("text"), str)
    )
    return (
        "INSERT INTO event_body_identity "
        "(event_id, version, present_at_admission, recorded_at) VALUES (?, 1, ?, ?)",
        (event.event_id, int(present), recorded_at),
    )


class _ContentIdentityMixin:
    """Metadata-only identity lookup; callers get no body bytes or permissions."""

    if TYPE_CHECKING:

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def get_body_presence_at_admission(self, event_id: str) -> bool | None:
        """Immutable admission fact: None means no event, never expired content.

        This returns no bytes, grants, current availability, or read permission.
        """
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event identifier is required")
        row = await self._read_one(
            "SELECT b.present_at_admission FROM canonical_events AS e "
            "LEFT JOIN event_body_identity AS b ON b.event_id=e.event_id "
            "WHERE e.event_id=?",
            (event_id,),
        )
        if row is None:
            return None
        state = row["present_at_admission"]
        if type(state) is not int or state not in (0, 1):
            raise StorageError("Admitted event has no valid body identity")
        return bool(state)
