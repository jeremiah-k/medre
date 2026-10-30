"""Opt-in SQLite-backed content read grants; never a deletion authority.

Owner-bound ingress/outbox reads and native dispatch use this authority.
General get/query, delivery evidence and provider stores remain outside it;
this is not whole-runtime retention enforcement.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from medre.core.storage.backend import StorageError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent
    from collections.abc import Callable
    from typing import Concatenate, ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Content authority requires a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class ContentBodyRead:
    """One authorized result; denied results never expose presence or bytes."""

    status: Literal["retained", "unavailable", "denied"]
    body: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"retained", "unavailable", "denied"}:
            raise ValueError("Unknown content body read status")
        if (self.status == "retained" and not isinstance(self.body, str)) or (
            self.status != "retained" and self.body is not None
        ):
            raise ValueError("Only retained reads contain a string body")


def _observe_mandatory_expiry(
    db: sqlite3.Connection, event_id: str, observed: str
) -> None:
    """Permanently close an expired ingress/append dependency.

    This is a logical permission transition, not content collection or erase.
    A later wall-clock rollback cannot resurrect the expired authority.
    Caller holds the connection lock and an IMMEDIATE transaction.
    """
    db.execute(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind IN ('ingress','append') "
        "AND released_at IS NULL AND deadline IS NOT NULL AND deadline<=?",
        (observed, event_id, observed),
    )


def _source_dependency_live(db: sqlite3.Connection, event_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM event_content_dependencies WHERE event_id=? "
            "AND owner_kind IN ('ingress','append') AND released_at IS NULL LIMIT 1",
            (event_id,),
        ).fetchone()
        is not None
    )


def _requires_content_authority(db: sqlite3.Connection, event_id: str) -> bool:
    """Inspect content facts, never infer absence from a missing grant row.

    Secondary text still needs an owner when an explicit non-string body makes
    the canonical body-presence fact false. Missing dependencies must fail
    closed rather than make retained content look like an envelope-only event.
    """
    return bool(
        db.execute(
            "SELECT EXISTS (SELECT 1 FROM event_body_identity "
            "WHERE event_id=? AND present_at_admission=1) "
            "OR EXISTS (SELECT 1 FROM event_body_content WHERE event_id=?) "
            "OR EXISTS (SELECT 1 FROM event_relations r "
            "JOIN event_relation_excerpts x ON x.relation_id=r.id WHERE r.event_id=?)",
            (event_id, event_id, event_id),
        ).fetchone()[0]
    )



def sync_issue_body_hold(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    hold: ContentHold,
    policy: AvailabilityPolicy,
    now: datetime,
) -> None:
    """Atomically install the immutable event ceiling and one exact hold."""
    _utc(now)
    if hold.scope != "body" or hold.mandatory:
        raise ValueError("Only optional body holds are available in this slice")
    if hold.issued_at is None:
        raise ValueError("Hold must be issued by now and live at issue time")
    # Compare instants and elapsed durations, including across DST folds.
    # Python arithmetic between datetimes sharing a tzinfo uses wall time.
    issued_at = hold.issued_at.astimezone(timezone.utc)
    expires_at = hold.expires_at.astimezone(timezone.utc)
    observed_at = now.astimezone(timezone.utc)
    hard_ceiling = policy.hard_ceiling.astimezone(timezone.utc)
    if issued_at > observed_at or expires_at <= observed_at or issued_at >= expires_at:
        raise ValueError("Hold must be issued by now and live at issue time")
    if hold.consumer not in policy.allowed_optional_consumers:
        raise ValueError("Consumer not allowed by content policy")
    if expires_at > hard_ceiling:
        raise ValueError("Hold cannot outlive the hard ceiling")
    if expires_at - issued_at > policy.max_optional_hold:
        raise ValueError("Hold exceeds the bounded optional duration")
    if observed_at >= hard_ceiling:
        raise ValueError("Cannot issue a hold after the hard ceiling")
    ceiling = _utc(policy.hard_ceiling)
    allowed = json.dumps(
        sorted(policy.allowed_optional_consumers), separators=(",", ":")
    )
    seconds = policy.max_optional_hold // timedelta(microseconds=1)
    issue, expiry, observed = _utc(hold.issued_at), _utc(hold.expires_at), _utc(now)
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute(
                "SELECT 1 FROM event_body_identity WHERE event_id=?", (event_id,)
            ).fetchone()
            if event is None:
                raise ValueError("Content policy requires an admitted event")
            _observe_mandatory_expiry(db, event_id, observed)
            owners = db.execute(
                "SELECT COUNT(*) FROM event_content_dependencies "
                "WHERE event_id=? AND owner_kind IN ('ingress','append')",
                (event_id,),
            ).fetchone()[0]
            if (
                owners or _requires_content_authority(db, event_id)
            ) and not _source_dependency_live(db, event_id):
                # Only expiry observations have changed so far. Persist them
                # even though issuance is refused; rollback must not reopen a
                # source when the wall clock subsequently moves backwards.
                db.commit()
                raise ValueError(
                    "Expired mandatory content or a missing owner cannot gain new optional holds"
                )
            current = db.execute(
                "SELECT hard_ceiling, allowed_consumers, max_optional_hold_microseconds "
                "FROM event_content_access_policies WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if current is not None and tuple(current) != (ceiling, allowed, seconds):
                raise ValueError("Content policy is immutable for this event")
            if current is None:
                db.execute(
                    "INSERT INTO event_content_access_policies "
                    "(event_id, version, hard_ceiling, allowed_consumers, "
                    "max_optional_hold_microseconds, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                    (event_id, ceiling, allowed, seconds, observed),
                )
            old = db.execute(
                "SELECT issued_at, expires_at, revoked_at "
                "FROM event_content_access_grants "
                "WHERE event_id=? AND scope='body' AND consumer=?",
                (event_id, hold.consumer),
            ).fetchone()
            if old is not None:
                if tuple(old) != (issue, expiry, None):
                    raise ValueError("Content hold cannot be replaced or resurrected")
            else:
                db.execute(
                    "INSERT INTO event_content_access_grants "
                    "(event_id, scope, consumer, issued_at, expires_at) "
                    "VALUES (?, 'body', ?, ?, ?)",
                    (event_id, hold.consumer, issue, expiry),
                )
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_revoke_body_hold(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    consumer: str,
    now: datetime,
) -> bool:
    with lock:
        try:
            row = db.execute(
                "UPDATE event_content_access_grants SET revoked_at=? "
                "WHERE event_id=? AND scope='body' AND consumer=? AND revoked_at IS NULL",
                (_utc(now), event_id, consumer),
            )
            db.commit()
            return row.rowcount == 1
        except BaseException:
            try:
                db.rollback()
            except Exception:
                pass
            raise


def sync_read_authorized_body(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    consumer: str,
    now: datetime,
) -> ContentBodyRead:
    observed = _utc(now)
    # Only a granted, live, unrevoked requester can project payload into Python.
    # All checks happen in one SQLite snapshot, not a check-then-read pair.
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        _observe_mandatory_expiry(db, event_id, observed)
        if _requires_content_authority(db, event_id) and not _source_dependency_live(
            db, event_id
        ):
            return ContentBodyRead("denied")
        row = db.execute(
            "SELECT (SELECT c.body FROM event_body_content c "
            "WHERE c.event_id=e.event_id ORDER BY c.field='body' DESC LIMIT 1), "
            "b.present_at_admission FROM canonical_events e "
            "LEFT JOIN event_body_identity b ON b.event_id=e.event_id "
            "JOIN event_content_access_policies p ON p.event_id=e.event_id "
            "JOIN event_content_access_grants g ON g.event_id=e.event_id "
            "WHERE e.event_id=? AND g.scope='body' AND g.consumer=? "
            "AND g.revoked_at IS NULL AND g.issued_at<=? AND g.expires_at>? "
            "AND p.hard_ceiling>? "
            "AND NOT EXISTS (SELECT 1 FROM event_content_dependencies d "
            "WHERE d.event_id=e.event_id AND d.owner_kind IN ('ingress','append') "
            "AND d.released_at IS NOT NULL)",
            (event_id, consumer, observed, observed, observed),
        ).fetchone()
    if row is None:
        return ContentBodyRead("denied")
    if type(row[1]) is not int or row[1] not in (0, 1):
        raise StorageError("Admitted event has no valid body identity")
    if row[1] != 1:
        return ContentBodyRead("unavailable")
    if not isinstance(row[0], str):
        raise StorageError("Body admission fact disagrees with separate text content")
    return ContentBodyRead("retained", row[0])


def sync_inspect_content_dependencies(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
) -> tuple[tuple[str, str, str | None, bool], ...]:
    """Return owners, deadlines and release state, never message bytes."""
    with lock:
        rows = db.execute(
            "SELECT owner_kind, owner_id, deadline, released_at "
            "FROM event_content_dependencies WHERE event_id=? "
            "ORDER BY owner_kind, owner_id",
            (event_id,),
        ).fetchall()
    return tuple((str(r[0]), str(r[1]), r[2], r[3] is not None) for r in rows)


def sync_read_outbox_body(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    outbox_id: str,
    now: datetime,
) -> ContentBodyRead:
    """Authorize an outbox-owned body through one SQLite read snapshot.

    A content read is not permission to call a native SDK indefinitely. The
    worker must also own a valid attempt reservation at send time.
    """
    observed = _utc(now)
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        _observe_mandatory_expiry(db, event_id, observed)
        row = db.execute(
            "SELECT (SELECT c.body FROM event_body_content c "
            "WHERE c.event_id=b.event_id ORDER BY c.field='body' DESC LIMIT 1), "
            "b.present_at_admission "
            "FROM event_body_identity b "
            "JOIN event_content_dependencies d ON d.event_id=b.event_id "
            "JOIN delivery_outbox o ON o.outbox_id=d.owner_id AND o.event_id=b.event_id "
            "WHERE b.event_id=? AND d.owner_kind='outbox' AND d.owner_id=? "
            "AND d.released_at IS NULL AND d.acquired_at<=? "
            "AND (d.deadline IS NULL OR d.deadline>?) "
            "AND o.status IN ('pending', 'in_progress', 'retry_wait') "
            "AND EXISTS (SELECT 1 FROM event_content_dependencies s "
            "WHERE s.event_id=b.event_id AND s.owner_kind IN ('ingress','append') "
            "AND s.released_at IS NULL)",
            (event_id, outbox_id, observed, observed),
        ).fetchone()
    if row is None:
        return ContentBodyRead("denied")
    if row[1] not in (0, 1):
        raise StorageError("Admitted body identity is invalid")
    if row[1] == 0:
        return ContentBodyRead("unavailable")
    if not isinstance(row[0], str):
        raise StorageError("Admitted body is missing from separated content storage")
    return ContentBodyRead("retained", row[0])


@dataclass(frozen=True, slots=True)
class AuthorizedEventRead:
    """Closed response; a denied caller receives no event object."""

    status: Literal["retained", "denied"]
    event: "CanonicalEvent | None" = None

    def __post_init__(self) -> None:
        if self.status not in {"retained", "denied"}:
            raise ValueError("Unknown authorized event read status")
        if (self.status == "retained") != (self.event is not None):
            raise ValueError("Only retained authorized reads return an event")


def sync_read_owned_event(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    owner_kind: Literal["ingress", "outbox"],
    owner_id: str,
    now: datetime,
) -> AuthorizedEventRead:
    """Check the durable owner and construct the event in the same snapshot.

    Core data is read only after authority is established. A read is not a
    native-submission reservation; the dispatch fence must check separately.
    """
    from msgspec.structs import replace

    from medre.core.storage.sqlite.serde import _row_to_event, _row_to_relation

    observed = _utc(now)
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        _observe_mandatory_expiry(db, event_id, observed)
        if owner_kind == "outbox":
            owner = db.execute(
                "SELECT 1 FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
                "AND status IN ('pending', 'in_progress', 'retry_wait')",
                (owner_id, event_id),
            ).fetchone()
        else:
            owner = db.execute(
                "SELECT 1 FROM durable_ingress_work WHERE event_id=? "
                "AND status IN ('pending', 'processing')",
                (event_id,),
            ).fetchone()
        if owner is None:
            return AuthorizedEventRead("denied")
        has_content = _requires_content_authority(db, event_id)
        if has_content:
            if not _source_dependency_live(db, event_id):
                return AuthorizedEventRead("denied")
            granted = db.execute(
                "SELECT 1 FROM event_content_dependencies WHERE event_id=? "
                "AND owner_kind=? AND owner_id=? AND released_at IS NULL "
                "AND acquired_at<=? AND (deadline IS NULL OR deadline>?)",
                (event_id, owner_kind, owner_id, observed, observed),
            ).fetchone()
            if granted is None:
                return AuthorizedEventRead("denied")
        row = db.execute(
            "SELECT * FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return AuthorizedEventRead("denied")
        rel_rows = db.execute(
            "SELECT r.*, x.excerpt AS resolved_fallback_text "
            "FROM event_relations r "
            "LEFT JOIN event_relation_excerpts x ON x.relation_id=r.id "
            "WHERE r.event_id=? ORDER BY r.id ASC",
            (event_id,),
        ).fetchall()
        body_rows = db.execute(
            "SELECT field, body FROM event_body_content WHERE event_id=?",
            (event_id,),
        ).fetchall()
        if any(
            r["resolved_fallback_text"] is not None
            and not isinstance(r["resolved_fallback_text"], str)
            for r in rel_rows
        ):
            raise StorageError("Relation excerpt content is not text")
        result = _row_to_event(dict(row), [_row_to_relation(dict(r)) for r in rel_rows])
        identity = db.execute(
            "SELECT present_at_admission FROM event_body_identity WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if (
            identity is None
            or type(identity[0]) is not int
            or identity[0] not in (0, 1)
        ):
            raise StorageError("Admitted event has no valid body identity")
        if any(not isinstance(r["body"], str) for r in body_rows):
            raise StorageError("Admitted body content is not text")
        payload = {**result.payload, **{r["field"]: r["body"] for r in body_rows}}
        present = isinstance(payload.get("body"), str) or (
            "body" not in payload and isinstance(payload.get("text"), str)
        )
        if bool(identity[0]) != present:
            raise StorageError(
                "Body admission fact disagrees with separate text content"
            )
        if body_rows:
            result = replace(result, payload=payload)
        return AuthorizedEventRead("retained", result)


def sync_authorize_native_submission(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    outbox_id: str,
    now: datetime,
) -> bool:
    """Fence native calls and durably observe expiry without erasing evidence."""
    observed = _utc(now)
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        _observe_mandatory_expiry(db, event_id, observed)
        owner = db.execute(
            "SELECT 1 FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
            "AND status='in_progress'",
            (outbox_id, event_id),
        ).fetchone()
        if owner is None:
            return False
        has_content = _requires_content_authority(db, event_id)
        if not has_content:
            return True
        if not _source_dependency_live(db, event_id):
            return False
        return (
            db.execute(
                "SELECT 1 FROM event_content_dependencies WHERE event_id=? "
                "AND owner_kind='outbox' AND owner_id=? AND released_at IS NULL "
                "AND acquired_at<=? AND (deadline IS NULL OR deadline>?)",
                (event_id, outbox_id, observed, observed),
            ).fetchone()
            is not None
        )


class _ContentGrantsMixin:
    """Trusted core callers only; external plugins must not get this handle."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

    async def _run_grant_op(
        self,
        operation: Callable[Concatenate[sqlite3.Connection, threading.Lock, _P], _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        try:
            return await self._run_in_thread(
                operation, self._require_db(), self._lock, *args, **kwargs
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Content grant operation failed: {exc}") from exc

    async def issue_optional_body_hold(
        self,
        event_id: str,
        hold: ContentHold,
        policy: AvailabilityPolicy,
        *,
        now: datetime,
    ) -> None:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event identifier is required")
        await self._run_grant_op(
            sync_issue_body_hold,
            event_id,
            hold,
            policy,
            now,
        )

    async def revoke_optional_body_hold(
        self, event_id: str, consumer: str, *, now: datetime
    ) -> bool:
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(consumer, str)
            or not consumer
        ):
            raise ValueError("A non-empty event ID and consumer are required")
        return await self._run_grant_op(sync_revoke_body_hold, event_id, consumer, now)

    async def read_event_for_outbox(
        self, event_id: str, outbox_id: str, *, now: datetime
    ) -> AuthorizedEventRead:
        if not event_id or not outbox_id:
            raise ValueError("An event and outbox identity are required")
        return await self._run_grant_op(
            sync_read_owned_event, event_id, "outbox", outbox_id, now
        )

    async def read_event_for_ingress(
        self, event_id: str, *, now: datetime
    ) -> AuthorizedEventRead:
        if not event_id:
            raise ValueError("An event identity is required")
        return await self._run_grant_op(
            sync_read_owned_event, event_id, "ingress", event_id, now
        )

    async def authorize_native_submission(
        self, event_id: str, outbox_id: str, *, now: datetime
    ) -> bool:
        if not event_id or not outbox_id:
            raise ValueError("An event and outbox identity are required")
        return await self._run_grant_op(
            sync_authorize_native_submission, event_id, outbox_id, now
        )

    async def list_mandatory_content_dependencies(
        self, event_id: str
    ) -> tuple[tuple[str, str, str | None, bool], ...]:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event ID is required")
        return await self._run_grant_op(sync_inspect_content_dependencies, event_id)

    async def read_body_for_outbox(
        self, event_id: str, outbox_id: str, *, now: datetime
    ) -> ContentBodyRead:
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(outbox_id, str)
            or not outbox_id
        ):
            raise ValueError("An exact event ID and outbox ID are required")
        return await self._run_grant_op(sync_read_outbox_body, event_id, outbox_id, now)

    async def read_body_for_consumer(
        self, event_id: str, consumer: str, *, now: datetime
    ) -> ContentBodyRead:
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(consumer, str)
            or not consumer
        ):
            raise ValueError("A non-empty event ID and consumer are required")
        return await self._run_grant_op(
            sync_read_authorized_body, event_id, consumer, now
        )
