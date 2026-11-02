"""Persist one immutable canonical body admission fact with the event.

No read grant, text erasure, or inferred expiration is provided by this layer.
The fact separates an event without a body from an absent event without
mistaking empty but explicitly present text for missing content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

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


def frozen_plan_deadline(context: Any) -> str | None:
    """Read the frozen delivery-plan deadline from a dispatch snapshot context.

    Direct snapshots carry the plan deadline at the top level; staged
    snapshots freeze the whole plan. Any other shape — including replay
    contexts and historical inline metadata without a deadline — yields
    ``None``, and the adoption fence then enforces the remaining bounds only.
    """
    if not isinstance(context, Mapping):
        return None
    raw = context.get("deadline")
    if raw is None:
        plan = context.get("plan")
        if isinstance(plan, Mapping):
            raw = plan.get("deadline")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("Frozen plan deadline must be a UTC timestamp string")
    return raw


def _utc_microseconds(value: str, what: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def insert_outbox_dependency(
    db: Any,
    outbox_id: str,
    event_id: str,
    now: str,
    *,
    plan_deadline: str | None = None,
) -> None:
    """Run inside outbox INSERT transaction, never after commit.

    A generation staged after the effective deadline does not receive
    content authority; it remains an auditable obligation and must not
    create a new native send. The effective deadline is the earlier of the
    immutable event ceiling, the source dependency deadline, and the frozen
    delivery-plan deadline. An event governed by an installed ceiling cannot
    adopt an ``append`` dependency that carries no deadline: the ceiling
    could not be validated against it.
    """
    # The general outbox timestamp helper may omit fractional seconds. Use
    # the same fixed UTC representation as admission deadlines and read gates.
    now = (
        datetime.fromisoformat(now)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    )
    frozen_plan = (
        _utc_microseconds(plan_deadline, "Frozen plan deadline")
        if plan_deadline is not None
        else None
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
    source = db.execute(
        "SELECT d.deadline AS deadline, p.hard_ceiling AS hard_ceiling "
        "FROM event_content_dependencies AS d "
        "LEFT JOIN event_content_access_policies AS p ON p.event_id=d.event_id "
        "WHERE d.event_id=? AND d.owner_kind IN ('ingress', 'append') "
        "AND d.released_at IS NULL "
        "ORDER BY d.owner_kind LIMIT 1",
        (event_id,),
    ).fetchone()
    if source is None:
        return
    source_deadline = source["deadline"]
    ceiling = source["hard_ceiling"]
    if ceiling is not None and source_deadline is None:
        # An append dependency with a NULL deadline cannot be treated as
        # eligible for ceiling-governed delivery.
        return
    bounds = [
        bound for bound in (source_deadline, ceiling, frozen_plan) if bound is not None
    ]
    if any(bound <= now for bound in bounds):
        return
    effective_deadline = min(bounds) if bounds else None
    db.execute(
        "INSERT INTO event_content_dependencies "
        "(event_id, owner_kind, owner_id, acquired_at, deadline) "
        "VALUES (?, 'outbox', ?, ?, ?)",
        (event_id, outbox_id, now, effective_deadline),
    )
