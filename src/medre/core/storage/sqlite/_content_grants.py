"""Opt-in SQLite-backed content read grants; never a deletion authority.

Owner-bound ingress/outbox reads and native dispatch use this authority.
Ordinary event and evidence inspection use SQL metadata projections. Retained
recovery evidence and provider stores remain sensitive; read mediation does
not implement physical retention enforcement.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from medre.core.storage.backend import StorageError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.read_contracts import (
    AuthorizedEventRead,
    ContentBodyRead,
    ReplyContextRead,
)
from medre.core.storage.sqlite._projections import REPLY_BODY_COLUMNS, REPLY_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Concatenate, ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Content authority requires a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _observe_mandatory_expiry(
    db: sqlite3.Connection, event_id: str, observed: str
) -> None:
    """Permanently close expired mandatory source and target dependencies.

    This is a logical permission transition, not content collection or erase.
    A later wall-clock rollback cannot resurrect the expired authority.
    Caller holds the connection lock and an IMMEDIATE transaction.
    """
    db.execute(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind IN ('ingress','append','outbox') "
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


def _observe_optional_expiry(
    db: sqlite3.Connection, event_id: str, observed: str
) -> None:
    """Close expired grants durably; a clock rollback cannot reopen a grant.

    Revocation and observed expiry have separate immutable tombstones.
    Neither condition may be undone by a backward clock adjustment.
    """
    db.execute(
        "UPDATE event_content_access_grants SET expired_at=? "
        "WHERE event_id=? AND scope='body' AND expired_at IS NULL "
        "AND revoked_at IS NULL "
        "AND expires_at<=?",
        (observed, event_id, observed),
    )


def _reply_quote_grant_live(
    db: sqlite3.Connection, event_id: str, observed: str
) -> bool:
    """Borrow a preissued bounded quote grant, never mint a new owner."""
    return (
        db.execute(
            "SELECT 1 FROM event_content_access_grants g "
            "JOIN event_content_access_policies p ON p.event_id=g.event_id "
            "WHERE g.event_id=? AND g.scope='body' AND g.consumer='reply_quotes' "
            "AND g.revoked_at IS NULL AND g.expired_at IS NULL "
            "AND g.issued_at<=? AND g.expires_at>? "
            "AND p.hard_ceiling>? LIMIT 1",
            (event_id, observed, observed, observed),
        ).fetchone()
        is not None
    )


def _requires_content_authority(db: sqlite3.Connection, event_id: str) -> bool:
    """Every admitted full projection requires an authorized owner.

    Opaque payload, native/custom metadata and relation keys can carry bytes
    without a canonical body. No heuristic may pronounce them content-free.
    """
    return (
        db.execute(
            "SELECT 1 FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()
        is not None
    )


def _outbox_content_authority(
    db: sqlite3.Connection, event_id: str, outbox_id: str, observed: str
) -> bool:
    """Answer in one snapshot whether an outbox may dispatch retained content.

    The ordinary authority is the target-scoped ``outbox`` dependency adopted
    inside the generation's own insert transaction. A replay generation
    deliberately adopts none; its authority is the admission hold itself, so
    replay dispatch stays possible exactly while the retained source is live
    and fails closed once that hold is released or expired. Callers must have
    observed mandatory expiry first, so a backward clock adjustment cannot
    resurrect a released admission through this path.
    """
    if (
        db.execute(
            "SELECT 1 FROM event_content_dependencies WHERE event_id=? "
            "AND owner_kind='outbox' AND owner_id=? AND released_at IS NULL "
            "AND acquired_at<=? AND (deadline IS NULL OR deadline>?)",
            (event_id, outbox_id, observed, observed),
        ).fetchone()
        is not None
    ):
        return True
    replay = db.execute(
        "SELECT 1 FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
        "AND dispatch_source='replay'",
        (outbox_id, event_id),
    ).fetchone()
    if replay is None:
        return False
    return _source_dependency_live(db, event_id)


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
            _observe_optional_expiry(db, event_id, observed)
            # Expiry observations are durable even when the requested policy
            # or grant mutation is refused. The savepoint isolates only those
            # mutations, so rejection cannot resurrect an expired permission.
            db.execute("SAVEPOINT content_hold_mutation")
            try:
                owners = db.execute(
                    "SELECT COUNT(*) FROM event_content_dependencies "
                    "WHERE event_id=? AND owner_kind IN ('ingress','append')",
                    (event_id,),
                ).fetchone()[0]
                if (
                    owners or _requires_content_authority(db, event_id)
                ) and not _source_dependency_live(db, event_id):
                    raise ValueError(
                        "Expired mandatory content or a missing owner cannot gain new optional holds"
                    )
                current = db.execute(
                    "SELECT hard_ceiling, allowed_consumers, max_optional_hold_microseconds "
                    "FROM event_content_access_policies WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if current is not None and tuple(current) != (
                    ceiling,
                    allowed,
                    seconds,
                ):
                    raise ValueError("Content policy is immutable for this event")
                if current is None:
                    db.execute(
                        "INSERT INTO event_content_access_policies "
                        "(event_id, version, hard_ceiling, allowed_consumers, "
                        "max_optional_hold_microseconds, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                        (event_id, ceiling, allowed, seconds, observed),
                    )
                old = db.execute(
                    "SELECT issued_at, expires_at, revoked_at, expired_at "
                    "FROM event_content_access_grants "
                    "WHERE event_id=? AND scope='body' AND consumer=?",
                    (event_id, hold.consumer),
                ).fetchone()
                if old is not None:
                    if tuple(old) != (issue, expiry, None, None):
                        raise ValueError(
                            "Content hold cannot be replaced or resurrected"
                        )
                else:
                    db.execute(
                        "INSERT INTO event_content_access_grants "
                        "(event_id, scope, consumer, issued_at, expires_at) "
                        "VALUES (?, 'body', ?, ?, ?)",
                        (event_id, hold.consumer, issue, expiry),
                    )
            except ValueError:
                db.execute("ROLLBACK TO content_hold_mutation")
                db.execute("RELEASE content_hold_mutation")
                db.commit()
                raise
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
                "WHERE event_id=? AND scope='body' AND consumer=? "
                "AND revoked_at IS NULL AND expired_at IS NULL",
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
    now: datetime | None = None,
) -> ContentBodyRead:
    observed = _utc(now) if now is not None else None
    # Only a granted, live, unrevoked requester can project payload into Python.
    # All checks happen in one SQLite snapshot, not a check-then-read pair.
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        if observed is None:
            observed = _utc(datetime.now(timezone.utc))
        _observe_mandatory_expiry(db, event_id, observed)
        _observe_optional_expiry(db, event_id, observed)
        row = db.execute(
            "SELECT (SELECT c.body FROM event_body_content c "
            "WHERE c.event_id=e.event_id ORDER BY c.field='body' DESC LIMIT 1), "
            "b.present_at_admission FROM canonical_events e "
            "LEFT JOIN event_body_identity b ON b.event_id=e.event_id "
            "JOIN event_content_access_policies p ON p.event_id=e.event_id "
            "JOIN event_content_access_grants g ON g.event_id=e.event_id "
            "WHERE e.event_id=? AND g.scope='body' AND g.consumer=? "
            "AND g.revoked_at IS NULL AND g.expired_at IS NULL "
            "AND g.issued_at<=? AND g.expires_at>? "
            "AND p.hard_ceiling>?",
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
    now: datetime | None = None,
) -> ContentBodyRead:
    """Authorize an outbox-owned body through one SQLite read snapshot.

    A content read is not permission to call a native SDK indefinitely. The
    worker must also own a valid attempt reservation at send time.
    """
    observed = _utc(now) if now is not None else None
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        if observed is None:
            observed = _utc(datetime.now(timezone.utc))
        _observe_mandatory_expiry(db, event_id, observed)
        outbox = db.execute(
            "SELECT 1 FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
            "AND status IN ('pending', 'in_progress', 'retry_wait')",
            (outbox_id, event_id),
        ).fetchone()
        # Replay generations carry no outbox dependency; they dispatch on the
        # still-live admission hold; ordinary target holds are independent.
        authorized = outbox is not None and _outbox_content_authority(
            db, event_id, outbox_id, observed
        )
        row = (
            db.execute(
                "SELECT (SELECT c.body FROM event_body_content c "
                "WHERE c.event_id=b.event_id ORDER BY c.field='body' DESC LIMIT 1), "
                "b.present_at_admission FROM event_body_identity b "
                "WHERE b.event_id=?",
                (event_id,),
            ).fetchone()
            if authorized
            else None
        )
    if row is None:
        return ContentBodyRead("denied")
    if row[1] not in (0, 1):
        raise StorageError("Admitted body identity is invalid")
    if row[1] == 0:
        return ContentBodyRead("unavailable")
    if not isinstance(row[0], str):
        raise StorageError("Admitted body is missing from separated content storage")
    return ContentBodyRead("retained", row[0])


def sync_read_owned_event(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    owner_kind: Literal["ingress", "outbox", "replay", "reply"],
    owner_id: str,
    now: datetime | None = None,
) -> AuthorizedEventRead:
    """Check the durable owner and construct the event in the same snapshot.

    Core data is read only after authority is established. A read is not a
    native-submission reservation; the dispatch fence must check separately.
    """
    from msgspec.structs import replace

    from medre.core.storage.sqlite.serde import _row_to_event, _row_to_relation

    observed = _utc(now) if now is not None else None
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        if observed is None:
            observed = _utc(datetime.now(timezone.utc))
        _observe_mandatory_expiry(db, event_id, observed)
        if owner_kind == "reply":
            _observe_optional_expiry(db, event_id, observed)
        if owner_kind == "outbox":
            owner = db.execute(
                "SELECT 1 FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
                "AND status IN ('pending', 'in_progress', 'retry_wait')",
                (owner_id, event_id),
            ).fetchone()
        elif owner_kind == "ingress":
            owner = db.execute(
                "SELECT 1 FROM durable_ingress_work WHERE event_id=? "
                "AND status IN ('pending', 'processing')",
                (event_id,),
            ).fetchone()
        else:
            # Replay and reply need an admitted identity. Their distinct
            # source/grant permissions are checked before projecting content.
            owner = db.execute(
                "SELECT 1 FROM canonical_events WHERE event_id=?", (event_id,)
            ).fetchone()
        if owner is None:
            return AuthorizedEventRead("denied")
        quote_grant_only = False
        has_content = _requires_content_authority(db, event_id)
        if has_content:
            if owner_kind == "replay" and not _source_dependency_live(db, event_id):
                return AuthorizedEventRead("denied")
            if owner_kind == "reply":
                quote_grant_only = not _source_dependency_live(db, event_id)
                if quote_grant_only and not _reply_quote_grant_live(
                    db, event_id, observed
                ):
                    return AuthorizedEventRead("denied")
            if owner_kind == "outbox":
                # Replay generations dispatch on the live admission hold; they
                # never adopt a target-scoped dependency of their own.
                if not _outbox_content_authority(db, event_id, owner_id, observed):
                    return AuthorizedEventRead("denied")
            elif owner_kind == "ingress":
                granted = db.execute(
                    "SELECT 1 FROM event_content_dependencies WHERE event_id=? "
                    "AND owner_kind=? AND owner_id=? AND released_at IS NULL "
                    "AND acquired_at<=? AND (deadline IS NULL OR deadline>?)",
                    (event_id, owner_kind, owner_id, observed, observed),
                ).fetchone()
                if granted is None:
                    return AuthorizedEventRead("denied")
            # Replay borrows source ownership; reply can also borrow a
            # preissued body grant. Neither creates retention or exempts
            # opaque bodyless payloads from authorization.
        # Live-source reply reads may project sender metadata. A body-only
        # grant cannot authorize opaque metadata; neither reply path selects
        # unrelated payload fields or the target's own quoted relations.
        columns = "*"
        if owner_kind == "reply":
            columns = REPLY_BODY_COLUMNS if quote_grant_only else REPLY_COLUMNS
        # Closed schema projections; event identities are bound parameters.
        row = db.execute(
            f"SELECT {columns} FROM canonical_events WHERE event_id=?",  # nosec B608
            (event_id,),
        ).fetchone()
        if row is None:
            return AuthorizedEventRead("denied")
        rel_rows = (
            []
            if owner_kind == "reply"
            else db.execute(
                "SELECT r.*, x.excerpt AS resolved_fallback_text "
                "FROM event_relations r "
                "LEFT JOIN event_relation_excerpts x ON x.relation_id=r.id "
                "WHERE r.event_id=? ORDER BY r.id ASC",
                (event_id,),
            ).fetchall()
        )
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
        if any(
            isinstance(result.payload.get(field), str) for field in ("body", "text")
        ):
            raise StorageError("Canonical envelope contains separated body content")
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
        if owner_kind == "reply" and identity[0] == 0:
            body_rows = []
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


def sync_read_reply_context(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    now: datetime | None = None,
) -> ReplyContextRead:
    read = sync_read_owned_event(db, lock, event_id, "reply", event_id, now)
    if read.status == "denied" or read.event is None:
        return ReplyContextRead("denied")
    present = any(
        isinstance(read.event.payload.get(key), str) for key in ("body", "text")
    )
    return ReplyContextRead("retained" if present else "unavailable", read.event)


def sync_authorize_native_submission(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    outbox_id: str,
    worker_id: str,
    attempt_number: int,
    now: datetime | None = None,
) -> bool:
    """Fence one live worker and attempt before native submission.

    The SQLite check is atomic with expiry observation. It is not a claim
    that the SDK participates in a distributed exactly-once transaction.
    """
    observed = _utc(now) if now is not None else None
    with lock, db:
        db.execute("BEGIN IMMEDIATE")
        if observed is None:
            observed = _utc(datetime.now(timezone.utc))
        return _native_submission_authorized(
            db, event_id, outbox_id, worker_id, attempt_number, observed
        )


def _native_submission_authorized(
    db: sqlite3.Connection,
    event_id: str,
    outbox_id: str,
    worker_id: str,
    attempt_number: int,
    observed: str,
) -> bool:
    """Validate one native start inside the caller's owned SQLite transaction."""
    _observe_mandatory_expiry(db, event_id, observed)
    owner = db.execute(
        "SELECT lease_until FROM delivery_outbox WHERE outbox_id=? AND event_id=? "
        "AND status='in_progress' AND worker_id=? "
        "AND (active_attempt=? OR (active_attempt IS NULL "
        "AND worker_id LIKE 'pipeline:%' AND attempt_number=?)) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM delivery_outbox AS sibling "
        "WHERE sibling.outbox_id<>delivery_outbox.outbox_id "
        "AND sibling.event_id=delivery_outbox.event_id "
        "AND sibling.delivery_plan_id=delivery_outbox.delivery_plan_id "
        "AND sibling.target_adapter=delivery_outbox.target_adapter "
        "AND COALESCE(sibling.target_channel, '')="
        "COALESCE(delivery_outbox.target_channel, '') "
        "AND COALESCE(sibling.active_attempt,sibling.attempt_number)>="
        "COALESCE(delivery_outbox.active_attempt,delivery_outbox.attempt_number))",
        (outbox_id, event_id, worker_id, attempt_number, attempt_number),
    ).fetchone()
    if owner is None or not isinstance(owner[0], str):
        return False
    try:
        lease = _utc(datetime.fromisoformat(owner[0]))
    except ValueError:
        return False
    if lease <= observed:
        return False
    if not _requires_content_authority(db, event_id):
        return False
    return _outbox_content_authority(db, event_id, outbox_id, observed)


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
        self, event_id: str, outbox_id: str, *, now: datetime | None = None
    ) -> AuthorizedEventRead:
        if not event_id or not outbox_id:
            raise ValueError("An event and outbox identity are required")
        return await self._run_grant_op(
            sync_read_owned_event, event_id, "outbox", outbox_id, now
        )

    async def read_event_for_ingress(
        self, event_id: str, *, now: datetime | None = None
    ) -> AuthorizedEventRead:
        if not event_id:
            raise ValueError("An event identity is required")
        return await self._run_grant_op(
            sync_read_owned_event, event_id, "ingress", event_id, now
        )

    async def read_reply_context(
        self, event_id: str, *, now: datetime | None = None
    ) -> ReplyContextRead:
        """Borrow canonical quote and sender context without adopting a hold."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("An exact event ID is required")
        return await self._run_grant_op(sync_read_reply_context, event_id, now)

    async def read_event_for_replay(
        self, event_id: str, *, now: datetime | None = None
    ) -> AuthorizedEventRead:
        """Read a replay candidate only while its source content remains live.

        The caller must be an explicitly requested replay operation. This
        method does not grant an outbox claim, reset expiry, or create a hold.
        """
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("An exact event ID is required")
        return await self._run_grant_op(
            sync_read_owned_event, event_id, "replay", event_id, now
        )

    async def authorize_native_submission(
        self,
        event_id: str,
        outbox_id: str,
        *,
        worker_id: str,
        attempt_number: int,
        now: datetime | None = None,
    ) -> bool:
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(outbox_id, str)
            or not outbox_id
        ):
            raise ValueError("An event and outbox identity are required")
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("A concrete outbox worker is required")
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("A positive reserved attempt number is required")
        return await self._run_grant_op(
            sync_authorize_native_submission,
            event_id,
            outbox_id,
            worker_id,
            attempt_number,
            now,
        )

    async def list_mandatory_content_dependencies(
        self, event_id: str
    ) -> tuple[tuple[str, str, str | None, bool], ...]:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event ID is required")
        return await self._run_grant_op(sync_inspect_content_dependencies, event_id)

    async def read_body_for_outbox(
        self, event_id: str, outbox_id: str, *, now: datetime | None = None
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
        self, event_id: str, consumer: str, *, now: datetime | None = None
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
