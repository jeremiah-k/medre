"""Conservative event-scoped logical collection; the single mutation boundary.

This module holds the guarded per-event collector shared by the operator
CLI and the runtime's policy-governed collection worker. Authority is
decided inside BEGIN IMMEDIATE, never from a prior assessment or preview.
No free-form content values leave the database as part of a report. This API
removes only canonical body and same-event excerpt rows when every retained
evidence family carries no content-potential values and every live owner,
wholesale policy blocker and opaque copy is absent. Retained durable evidence
(surviving rows, including the separated delivery diagnostics) is never
deleted. It does not claim physical erasure. The storage layer itself
installs no scheduler: automatic invocation lives in the runtime worker
and is gated on the operator's declared relay-only policy.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import (
    ContentCollectionEpochChanged,
    ContentCollectionTransientError,
    StorageError,
)
from medre.core.storage.content import UNMEASURED_CONTENT_SCOPES
from medre.core.storage.content_retention import normalize_admission_watermark
from medre.core.storage.sqlite._count import (
    _ALL_EVENT_COPY_PROBES,
    _EVENT_COPY_SQL,
    _release_blockers,
)

COLLECTION_UNMEASURED_SCOPES = UNMEASURED_CONTENT_SCOPES

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


def _read_collection_state(
    db: sqlite3.Connection, event_id: str
) -> tuple[str, list[str], int, int]:
    """Read the collector's decision inputs for one event in the caller's snapshot.

    Shared by the guarded collector and the read-only retention preview: the
    same compound projection, the same receipt lookup, the same candidate
    measurement, and the same fail-closed consistency guards. Returns
    ``(base_state, blockers, rows, logical_bytes)`` where ``base_state`` is
    ``already_collected``, ``blocked``, ``empty`` or ``eligible`` — the
    dry-run collector reports ``eligible`` as ``dry_run`` and the apply
    collector mutates on it. Never selects a content value; never mutates;
    raises ``StorageError`` on missing events, invalid admission identity,
    and unexpectedly recreated content.
    """
    row = db.execute(_EVENT_COPY_SQL, (event_id,)).fetchone()
    if row is None:
        raise StorageError("Content collection requires an admitted event")
    counts = dict(zip(_ALL_EVENT_COPY_PROBES, row, strict=True))
    blockers = _release_blockers(counts)
    previous = db.execute(
        "SELECT rows_removed, logical_bytes_removed FROM "
        "event_content_collection_log WHERE event_id=?",
        (event_id,),
    ).fetchone()
    measure = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(body AS BLOB))), 0) "
        "FROM event_body_content WHERE event_id=?",
        (event_id,),
    ).fetchone()
    excerpts = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(x.excerpt AS BLOB))), 0) "
        "FROM event_relation_excerpts x "
        "JOIN event_relations r ON r.id=x.relation_id WHERE r.event_id=?",
        (event_id,),
    ).fetchone()
    rows = int(measure[0]) + int(excerpts[0])
    logical_bytes = int(measure[1]) + int(excerpts[1])
    identity = db.execute(
        "SELECT present_at_admission FROM event_body_identity WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if identity is None or type(identity[0]) is not int or identity[0] not in (0, 1):
        raise StorageError("Collection requires a valid admission identity")
    if previous is not None and rows:
        raise StorageError("Collected content was unexpectedly recreated")
    if previous is None and identity[0] == 0 and measure[0]:
        raise StorageError("Body row exists but admission recorded no body")
    if previous is None and identity[0] and not measure[0]:
        raise StorageError("Admission body is missing without collection record")
    if previous is not None:
        base_state = "already_collected"
    elif blockers:
        base_state = "blocked"
    elif not rows:
        base_state = "empty"
    else:
        base_state = "eligible"
    return base_state, blockers, rows, logical_bytes


def sync_preview_event_collection_state(db: sqlite3.Connection, event_id: str) -> str:
    """Read-only collector-equivalent status for one event, without mutation.

    Maps the guarded collector's dry-run vocabulary onto the retention
    preview's closed outcome vocabulary: ``dry_run`` becomes ``eligible``,
    and the collector's fail-closed ``StorageError`` refusals (missing
    event, invalid admission identity, recreated content) become
    ``inconsistent`` so an aggregate preview counts them instead of
    aborting. Must run inside the caller's transaction; the caller owns
    transaction control.
    """
    try:
        base_state, _blockers, _rows, _logical_bytes = _read_collection_state(
            db, event_id
        )
    except StorageError:
        return "inconsistent"
    return base_state


def sync_collect_event_content(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    dry_run: bool,
    retention_epoch: int | None = None,
) -> dict[str, Any]:
    """Check and optionally mutate in one serialized transaction.

    A blocked attempt is a non-mutating refusal, not a partial collection.
    The count query is a closed, event-ID-bound SQL projection.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
            if retention_epoch is not None:
                # Runtime collection is authorized by the *current* epoch,
                # not the one observed before paging. This check shares the
                # collector's write reservation with the content mutation.
                active = db.execute(
                    "SELECT epoch, mode, admission_watermark "
                    "FROM content_retention_epochs ORDER BY epoch DESC LIMIT 1"
                ).fetchone()
                if (
                    active is None
                    or active[0] != retention_epoch
                    or active[1] != "relay-only"
                ):
                    raise ContentCollectionEpochChanged(
                        "Retention activation changed during collection"
                    )
                admission = db.execute(
                    "SELECT created_at FROM canonical_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if admission is None or (
                    active[2] is not None
                    and admission[0] <= normalize_admission_watermark(active[2])
                ):
                    raise ContentCollectionEpochChanged(
                        "Candidate is outside the current activation boundary"
                    )
            base_state, blockers, rows, logical_bytes = _read_collection_state(
                db, event_id
            )
            if base_state == "already_collected":
                state = "already_collected"
            elif base_state == "blocked":
                state = "blocked"
            elif base_state == "empty":
                state = "empty"
            elif dry_run:
                state = "dry_run"
            else:
                # No live owner, wholesale policy blocker, opaque copy, or
                # content-bearing evidence row was observed inside this
                # transaction. Both deletes and the audit receipt commit
                # together; a crash cannot publish a partial collection.
                db.execute(
                    "DELETE FROM event_relation_excerpts WHERE relation_id IN "
                    "(SELECT id FROM event_relations WHERE event_id=?)",
                    (event_id,),
                )
                db.execute(
                    "DELETE FROM event_body_content WHERE event_id=?", (event_id,)
                )
                db.execute(
                    "INSERT INTO event_content_collection_log "
                    "(event_id,collected_at,policy,rows_removed,logical_bytes_removed) "
                    "VALUES (?, ?, 'relay-only', ?, ?)",
                    (
                        event_id,
                        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                        rows,
                        logical_bytes,
                    ),
                )
                state = "collected"
            result: dict[str, Any] = {
                "format": "medre-content-collection",
                "version": 1,
                "event_id": event_id,
                "mode": "relay-only",
                "status": state,
                "dry_run": dry_run,
                "blockers": blockers,
                "candidate_rows": rows,
                "candidate_logical_bytes": logical_bytes,
                "rows_removed": rows if state == "collected" else 0,
                "logical_bytes_removed": logical_bytes if state == "collected" else 0,
                "unmeasured": list(COLLECTION_UNMEASURED_SCOPES),
                "physical_erasure_claimed": False,
            }
            if dry_run:
                db.rollback()
            else:
                db.commit()
            return result
        except BaseException:
            db.rollback()
            raise


class _CollectionMixin:
    """Exact-event guarded collection for the operator CLI and the
    policy-governed runtime worker; this layer installs no scheduler."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

    async def collect_event_content(
        self,
        event_id: str,
        *,
        dry_run: bool = True,
        retention_epoch: int | None = None,
    ) -> dict[str, Any]:
        if retention_epoch is not None and (
            type(retention_epoch) is not int or retention_epoch < 1
        ):
            raise ValueError("Retention epoch must be a positive integer")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A bounded event ID is required")
        try:
            return await self._run_in_thread(
                sync_collect_event_content,
                self._require_db(),
                self._lock,
                event_id,
                dry_run,
                retention_epoch,
            )
        except sqlite3.OperationalError as exc:
            # Transient SQLite condition (locked/busy/I/O): a later attempt
            # may clear it. Semantic integrity refusals are raised inside the
            # sync collector as StorageError and never take this path, so a
            # caller may treat this subclass as defer-only.
            raise ContentCollectionTransientError(
                "Core content collection failed"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError("Core content collection failed") from exc
