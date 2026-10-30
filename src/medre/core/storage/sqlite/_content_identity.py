"""Persist one immutable canonical body admission fact with the event.

No read grant, text erasure, or inferred expiration is provided by this layer.
The fact separates an event without a body from an absent event without
mistaking empty but explicitly present text for missing content.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def body_content_ops(
    event: CanonicalEvent, recorded_at: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """Every original body/text field is preserved, including empty strings."""
    return [
        (
            "INSERT INTO event_body_content "
            "(event_id, field, body, created_at) VALUES (?, ?, ?, ?)",
            (event.event_id, key, event.payload[key], recorded_at),
        )
        for key in ("body", "text")
        if isinstance(event.payload.get(key), str)
    ]


def envelope_payload(event: CanonicalEvent) -> dict[str, object]:
    """Separate string text fields from the canonical envelope."""
    return {
        key: value
        for key, value in event.payload.items()
        if key not in {"body", "text"} or not isinstance(value, str)
    }


def ingress_dependency_op(
    event: CanonicalEvent,
    recorded_at: str,
    *,
    owner_kind: str = "ingress",
    deadline: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Own every full canonical projection, including opaque payload and metadata.

    This op MUST run in the same transaction as the canonical row. The
    dependency does not itself imply expiry, release, or erasure.
    """
    if owner_kind not in {"ingress", "append"}:
        raise ValueError("invalid admission dependency owner")
    return (
        "INSERT INTO event_content_dependencies "
        "(event_id, owner_kind, owner_id, acquired_at, deadline) "
        "VALUES (?, ?, ?, ?, ?)",
        (event.event_id, owner_kind, event.event_id, recorded_at, deadline),
    )


def insert_outbox_dependency(db: Any, outbox_id: str, event_id: str, now: str) -> None:
    """Run inside outbox INSERT transaction, never after commit.

    A target whose ingress deadline has already passed does not receive
    content authority. It must not create a new native send.
    """
    # The general outbox timestamp helper may omit fractional seconds. Use
    # the same fixed UTC representation as admission deadlines and read gates.
    now = (
        datetime.fromisoformat(now)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    )
    # Mark the source deadline within the same transaction before adoption.
    # A later backwards wall-clock adjustment must not resurrect an expired
    # admission lease or create a new target dependency.
    db.execute(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind IN ('ingress','append') "
        "AND released_at IS NULL AND deadline IS NOT NULL AND deadline<=?",
        (now, event_id, now),
    )
    db.execute(
        "INSERT INTO event_content_dependencies "
        "(event_id, owner_kind, owner_id, acquired_at, deadline) "
        "SELECT d.event_id, 'outbox', ?, ?, d.deadline "
        "FROM event_content_dependencies AS d "
        "WHERE d.event_id=? AND d.owner_kind IN ('ingress', 'append') "
        "AND d.released_at IS NULL "
        "AND (d.deadline IS NULL OR d.deadline > ?) "
        "ORDER BY d.owner_kind LIMIT 1",
        (outbox_id, now, event_id, now),
    )
