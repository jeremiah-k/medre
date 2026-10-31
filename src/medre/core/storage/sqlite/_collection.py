"""Conservative event-scoped logical collection; no background execution.

Authority is decided inside BEGIN IMMEDIATE, never from a prior assessment.
No free-form content values leave the database as part of a report. This API
removes only canonical body and same-event excerpt rows when every other
content-bearing surface is proven absent. It does not claim physical erasure.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._count import (
    _EVENT_COPY_PROBES,
    _EVENT_COPY_SQL,
    _LIVE_BLOCKER_PROBES,
    _UNCLASSIFIED_EVENT_COPIES,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


def sync_collect_event_content(
    db: sqlite3.Connection,
    lock: threading.Lock,
    event_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Check and optionally mutate in one serialized transaction.

    A blocked attempt is a non-mutating refusal, not a partial collection.
    The count query is a closed, event-ID-bound SQL projection.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
            row = db.execute(_EVENT_COPY_SQL, (event_id,)).fetchone()
            if row is None:
                raise StorageError("Content collection requires an admitted event")
            counts = dict(zip(_EVENT_COPY_PROBES, row, strict=True))
            blockers = [
                name
                for name in (*_LIVE_BLOCKER_PROBES, *sorted(_UNCLASSIFIED_EVENT_COPIES))
                if counts[name]
            ]
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
            if (
                identity is None
                or type(identity[0]) is not int
                or identity[0] not in (0, 1)
            ):
                raise StorageError("Collection requires a valid admission identity")
            if previous is not None and rows:
                raise StorageError("Collected content was unexpectedly recreated")
            if previous is None and identity[0] and not measure[0]:
                raise StorageError(
                    "Admission body is missing without collection record"
                )
            if previous is not None:
                state = "already_collected"
            elif blockers:
                state = "blocked"
            elif not rows:
                state = "empty"
            elif dry_run:
                state = "dry_run"
            else:
                # No active claims or unclassified copies were observed inside
                # this transaction. Both deletes and the audit receipt commit
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
                "unmeasured": [
                    "provider_native_queues_histories_and_peers",
                    "sqlite_free_pages_and_wal",
                    "backups_exports_and_snapshots",
                    "application_and_provider_logs",
                    "copies_embedded_in_other_events",
                ],
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
    """Operator-invoked, exact-event collection only (no runtime scheduler)."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

    async def collect_event_content(
        self, event_id: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            raise ValueError("A bounded event ID is required")
        try:
            return await self._run_in_thread(
                sync_collect_event_content,
                self._require_db(),
                self._lock,
                event_id,
                dry_run,
            )
        except sqlite3.Error as exc:
            raise StorageError("Core content collection failed") from exc
