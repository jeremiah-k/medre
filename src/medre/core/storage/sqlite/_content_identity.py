"""Persist one immutable canonical body admission fact with the event.

No read grant, text erasure, or inferred expiration is provided by this layer.
The fact separates an event without a body from an absent event without
mistaking empty but explicitly present text for missing content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from medre.core.storage.backend import StorageError

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent

BodyAvailability = Literal["retained", "unavailable"]


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
        "(event_id, version, availability, recorded_at) VALUES (?, 1, ?, ?)",
        (event.event_id, "retained" if present else "unavailable", recorded_at),
    )


class _ContentIdentityMixin:
    """Metadata-only identity lookup; callers get no body bytes or permissions."""

    if TYPE_CHECKING:

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def get_body_availability(self, event_id: str) -> BodyAvailability | None:
        """None means no event; unavailable means the admitted body was absent."""
        # CanonicalEvent admits every non-empty string, including whitespace
        # and long IDs; metadata inspection must accept the same identity set.
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event identifier is required")
        row = await self._read_one(
            "SELECT b.availability FROM canonical_events AS e "
            "LEFT JOIN event_body_identity AS b ON b.event_id=e.event_id "
            "WHERE e.event_id=?",
            (event_id,),
        )
        if row is None:
            return None
        state = row["availability"]
        if state not in {"retained", "unavailable"}:
            raise StorageError("Admitted event has no valid body identity")
        return cast(BodyAvailability, state)
