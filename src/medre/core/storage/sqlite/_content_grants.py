"""Opt-in SQLite-backed content read grants; never a deletion authority.

This is the first closed content-read seam. Existing event.get/query, runtime
renderers, delivery evidence and provider stores are NOT yet mediated by it;
no site may describe this module as whole-runtime retention enforcement.
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
    with lock:
        row = db.execute(
            "SELECT e.payload, b.present_at_admission FROM canonical_events e "
            "LEFT JOIN event_body_identity b ON b.event_id=e.event_id "
            "JOIN event_content_access_policies p ON p.event_id=e.event_id "
            "JOIN event_content_access_grants g ON g.event_id=e.event_id "
            "WHERE e.event_id=? AND g.scope='body' AND g.consumer=? "
            "AND g.revoked_at IS NULL AND g.issued_at<=? AND g.expires_at>? "
            "AND p.hard_ceiling>?",
            (event_id, consumer, observed, observed, observed),
        ).fetchone()
    if row is None:
        return ContentBodyRead("denied")
    if type(row[1]) is not int or row[1] not in (0, 1):
        raise StorageError("Admitted event has no valid body identity")
    if row[1] != 1:
        return ContentBodyRead("unavailable")
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        raise StorageError("Stored body payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise StorageError("Stored body payload is not an object")
    body = payload.get("body")
    if "body" not in payload:
        body = payload.get("text")
    if not isinstance(body, str):
        raise StorageError("Body admission fact disagrees with immutable payload")
    return ContentBodyRead("retained", body)


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
