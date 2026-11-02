"""Durable retention activation epochs and the forward-only heartbeat.

The epoch table is the activation boundary of the relay-only retention
policy: opening an epoch pins the store's maximum local admission
``created_at`` inside a ``BEGIN IMMEDIATE`` transaction, and every retention
sweep is bounded to events admitted strictly after that watermark, so
pre-activation history is never swept. Watermarks are monotonic across
epochs, the heartbeat never moves backward, and epoch rows are append-only:
no update except the heartbeat, no delete.

This mixin selects no content values: candidate discovery reads only the
``(event_id, created_at)`` ordering key, and nothing here authorizes
deletion. The guarded per-event collector remains the only mutation
boundary.
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
from medre.core.storage.content_retention import (
    COLLECTION_UNMEASURED_SCOPES,
    RETENTION_BATCH_MAX,
    RETENTION_BATCH_MIN,
    RETENTION_EPOCH_ACTIVATORS,
    RETENTION_PREVIEW_OUTCOMES,
    CandidatePage,
    RetentionEpoch,
    normalize_admission_watermark,
)
from medre.core.storage.sqlite._collection import sync_preview_event_collection_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")

_EPOCH_COLUMNS = (
    "epoch, mode, activated_at, admission_watermark, "
    "last_observed_active_at, activated_by"
)


def _utc_iso(now: datetime) -> str:
    """Render a timezone-aware observation time as a comparable UTC ISO string."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Observation time must be timezone aware")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _epoch_from_row(row: tuple[Any, ...]) -> RetentionEpoch:
    return RetentionEpoch(
        epoch=int(row[0]),
        mode=str(row[1]),
        activated_at=str(row[2]),
        admission_watermark=None if row[3] is None else str(row[3]),
        last_observed_active_at=None if row[4] is None else str(row[4]),
        activated_by=str(row[5]),
    )


def sync_open_retention_epoch(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    activated_by: str,
    now: datetime,
) -> RetentionEpoch:
    """Open one relay-only activation epoch under the SQLite write reservation.

    The admission watermark is read as ``MAX(created_at)`` inside the same
    ``BEGIN IMMEDIATE`` transaction that inserts the epoch row, so no
    concurrent admission can fall between the boundary and the record.
    Watermarks are monotonic: an empty store keeps the previous epoch's
    watermark, and a lower observation never pulls the boundary backward.
    """
    if activated_by not in RETENTION_EPOCH_ACTIVATORS:
        raise ValueError("Unknown retention epoch activator")
    activated_at = _utc_iso(now)
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            observed = db.execute(
                "SELECT MAX(created_at) FROM canonical_events"
            ).fetchone()
            watermark = None if observed is None else observed[0]
            if watermark is not None:
                watermark = str(watermark)
            previous = db.execute(
                "SELECT admission_watermark FROM content_retention_epochs "
                "ORDER BY epoch DESC LIMIT 1"
            ).fetchone()
            previous_watermark = None if previous is None else previous[0]
            if previous_watermark is not None:
                if watermark is None or watermark < str(previous_watermark):
                    # The boundary never moves backward: an empty store or a
                    # lower observation keeps the previous epoch's watermark.
                    watermark = str(previous_watermark)
            cursor = db.execute(
                "INSERT INTO content_retention_epochs "
                "(mode, activated_at, admission_watermark, "
                "last_observed_active_at, activated_by) "
                "VALUES ('relay-only', ?, ?, NULL, ?)",
                (activated_at, watermark, activated_by),
            )
            rowid = cursor.lastrowid
            if rowid is None:
                raise StorageError(
                    "Retention epoch activation did not assign an epoch number"
                )
            db.commit()
            return RetentionEpoch(
                epoch=rowid,
                mode="relay-only",
                activated_at=activated_at,
                admission_watermark=watermark,
                last_observed_active_at=None,
                activated_by=activated_by,
            )
        except BaseException:
            db.rollback()
            raise


def sync_observe_retention_heartbeat(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    epoch: int,
    now: datetime,
) -> None:
    """Advance one epoch's heartbeat under the SQLite write reservation.

    The conditional UPDATE never moves ``last_observed_active_at`` backward,
    so a clock rollback is a no-op by construction. A missing epoch row is a
    ``StorageError``.
    """
    observed_at = _utc_iso(now)
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT epoch FROM content_retention_epochs "
                "ORDER BY epoch DESC LIMIT 1"
            ).fetchone()
            if latest is None or latest[0] != epoch:
                raise ContentCollectionEpochChanged(
                    "Retention activation changed before heartbeat"
                )
            db.execute(
                "UPDATE content_retention_epochs "
                "SET last_observed_active_at = ? "
                "WHERE epoch = ? AND (last_observed_active_at IS NULL "
                "OR last_observed_active_at < ?)",
                (observed_at, epoch, observed_at),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def sync_latest_retention_epoch(
    db: sqlite3.Connection,
    lock: threading.Lock,
) -> RetentionEpoch | None:
    """Read the newest durable activation epoch; a single read is atomic."""
    with lock:
        row = db.execute(
            f"SELECT {_EPOCH_COLUMNS} FROM content_retention_epochs "  # nosec B608 - column list is a module constant
            "ORDER BY epoch DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return _epoch_from_row(row)


def _no_active_epoch_result() -> dict[str, Any]:
    """The honest cannot-assess result for a store with no activation epoch."""
    result: dict[str, Any] = {
        "format": "medre-content-retention-preview",
        "version": 1,
        "scope": "core_sqlite_epoch",
        "mode": "relay-only",
        "status": "no_active_epoch",
        "events_examined": 0,
        "has_more": False,
        "next_after": None,
        "retention_authorized": False,
        "deletion_authorized": False,
        "physical_erasure_claimed": False,
        "unmeasured": list(COLLECTION_UNMEASURED_SCOPES),
    }
    result.update(dict.fromkeys(RETENTION_PREVIEW_OUTCOMES, 0))
    return result


def _preview_content_retention_in_snapshot(
    db: sqlite3.Connection,
    *,
    limit: int,
    after: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Read bounded preview inputs under the caller's existing snapshot."""
    epoch_row = db.execute(
        "SELECT epoch, activated_at, admission_watermark, activated_by "
        "FROM content_retention_epochs ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if epoch_row is None:
        return _no_active_epoch_result()
    counts = dict.fromkeys(RETENTION_PREVIEW_OUTCOMES, 0)
    watermark = epoch_row[2]
    # Unlike the candidate selector, the preview also assesses each
    # result under a single read snapshot. Its continuation must use
    # BOTH ordering columns; an event_id by itself is not a cursor.
    predicates: list[str] = []
    params: list[Any] = []
    if watermark is not None:
        predicates.append("created_at > ?")
        params.append(normalize_admission_watermark(str(watermark)))
    if after is not None:
        predicates.append("(created_at, event_id) > (?, ?)")
        params.extend(after)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    page = db.execute(
        "SELECT event_id, created_at FROM canonical_events"  # nosec B608 - closed predicates; values remain bound
        + where
        + " ORDER BY created_at, event_id LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    has_more = len(page) > limit
    examined = page[:limit]
    for event_id, _created_at in examined:
        outcome = sync_preview_event_collection_state(db, str(event_id))
        counts[outcome] += 1
    result: dict[str, Any] = {
        "format": "medre-content-retention-preview",
        "version": 1,
        "scope": "core_sqlite_epoch",
        "mode": "relay-only",
        "epoch": int(epoch_row[0]),
        "activated_at": str(epoch_row[1]),
        "admission_watermark": None if watermark is None else str(watermark),
        "activated_by": str(epoch_row[3]),
        "events_examined": len(examined),
        "has_more": has_more,
        "next_after": (
            [str(examined[-1][1]), str(examined[-1][0])] if examined else None
        ),
        "retention_authorized": False,
        "deletion_authorized": False,
        "physical_erasure_claimed": False,
        "unmeasured": list(COLLECTION_UNMEASURED_SCOPES),
    }
    result.update(counts)
    return result


def sync_preview_content_retention(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    limit: int,
    after: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Read the complete aggregate preview in one read-only snapshot."""
    with lock:
        try:
            db.execute("BEGIN")
            return _preview_content_retention_in_snapshot(db, limit=limit, after=after)
        finally:
            db.rollback()


def sync_read_content_retention_status(
    db: sqlite3.Connection,
    lock: threading.Lock,
) -> tuple[RetentionEpoch | None, int, dict[str, Any]]:
    """Observe epoch, receipt count and its preview in one read snapshot."""
    with lock:
        try:
            db.execute("BEGIN")
            row = db.execute(
                f"SELECT {_EPOCH_COLUMNS} FROM content_retention_epochs "  # nosec B608 - closed module-owned columns
                "ORDER BY epoch DESC LIMIT 1"
            ).fetchone()
            epoch = _epoch_from_row(row) if row is not None else None
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM event_content_collection_log"
                ).fetchone()[0]
            )
            preview = _preview_content_retention_in_snapshot(
                db, limit=RETENTION_BATCH_MAX
            )
            return epoch, count, preview
        finally:
            db.rollback()


_CANDIDATE_SELECT_SQL = "SELECT event_id, created_at FROM canonical_events"
_CANDIDATE_ORDER_SQL = " ORDER BY created_at ASC, event_id ASC LIMIT ?"
_CANDIDATE_WATERMARK_SQL = " WHERE created_at > ?"
_CANDIDATE_KEYSET_SQL = " WHERE (created_at, event_id) > (?, ?)"

_CANDIDATE_LIMIT_MAX = RETENTION_BATCH_MAX


def sync_page_collection_candidates(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    after: tuple[str, str] | None,
    limit: int,
    watermark: str | None,
) -> CandidatePage:
    """Page epoch-scoped candidate event IDs in stable admission order.

    One read-only SELECT returns at most ``limit + 1`` ``(event_id,
    created_at)`` rows ordered by the ``idx_events_admission_order`` index.
    The extra row only decides ``has_more`` and never reaches the caller, so
    memory stays bounded by the page limit regardless of table size. Event
    IDs are deliberately not assumed to be chronological: the ordering key is
    the stored local admission ``created_at`` text with ``event_id`` breaking
    ties, and per-event collection outcomes are invisible to this selector,
    so a permanently blocked event can never starve later ones — the page
    advance is outcome-independent.

    The ``watermark`` lower bound is strictly greater: an event admitted at
    exactly the watermark instant is pre-activation and never a candidate. A
    ``None`` watermark records an empty store at activation (the open-epoch
    semantics) and applies no filter. The ``after`` cursor is the previous
    page's ``next_after`` ``(created_at, event_id)`` keyset; a cursor at or
    before the watermark continues from the strict watermark head, which
    cannot return a pre-activation row. A page whose first row does not
    advance past ``after`` raises ``RuntimeError`` instead of looping.

    There is no implicit wraparound: paging past the epoch scope returns an
    empty page with ``has_more=False`` and ``next_after=None``. A new sweep
    cycle is the caller's decision and restarts with ``after=None``, which
    re-visits every epoch-scoped event including already-collected and
    blocked ones; outcome filtering across cycles belongs to the caller.

    Selection grants no mutation authority: nothing here collects, deletes,
    or authorizes anything, and the guarded per-event collector with its
    ``BEGIN IMMEDIATE`` revalidation remains the only mutation boundary.
    """
    if (
        type(limit) is not int
        or not RETENTION_BATCH_MIN <= limit <= _CANDIDATE_LIMIT_MAX
    ):
        raise ValueError(
            "A collection candidate page limit must be an integer in [1, 100]"
        )
    if watermark is not None and (not isinstance(watermark, str) or not watermark):
        raise ValueError("The admission watermark must be a timestamp or absent")
    if after is not None and (
        not isinstance(after, tuple)
        or len(after) != 2
        or not all(isinstance(part, str) and part for part in after)
    ):
        raise ValueError(
            "A collection candidate cursor must be a non-empty "
            "(created_at, event_id) pair"
        )
    watermark = normalize_admission_watermark(watermark)
    fetch = limit + 1
    if after is None:
        if watermark is None:
            # Empty-store epoch semantics: no events existed at activation,
            # so every current event is post-activation (matching the
            # aggregate preview's unfiltered head page).
            sql = _CANDIDATE_SELECT_SQL + _CANDIDATE_ORDER_SQL
            params: tuple[Any, ...] = (fetch,)
        else:
            sql = (
                _CANDIDATE_SELECT_SQL + _CANDIDATE_WATERMARK_SQL + _CANDIDATE_ORDER_SQL
            )
            params = (watermark, fetch)
    elif watermark is None or after[0] > watermark:
        # The cursor sits strictly inside the epoch scope: the keyset alone
        # seeks directly to the continuation point.
        sql = _CANDIDATE_SELECT_SQL + _CANDIDATE_KEYSET_SQL + _CANDIDATE_ORDER_SQL
        params = (*after, fetch)
    else:
        # The cursor is at or before the activation boundary; every event at
        # or before the watermark is pre-activation, so the strict watermark
        # head is the correct continuation and cannot return one.
        sql = _CANDIDATE_SELECT_SQL + _CANDIDATE_WATERMARK_SQL + _CANDIDATE_ORDER_SQL
        params = (watermark, fetch)
    with lock:
        rows = db.execute(sql, params).fetchall()
    if after is not None and rows and (str(rows[0][1]), str(rows[0][0])) <= after:
        raise RuntimeError("collection candidate scan did not advance its keyset")
    page = rows[:limit]
    return CandidatePage(
        event_ids=[str(row[0]) for row in page],
        next_after=(str(page[-1][1]), str(page[-1][0])) if page else None,
        has_more=len(rows) > limit,
    )


class _RetentionMixin:
    """Durable activation epochs and bounded candidate discovery; no scheduler."""

    async def read_content_retention_status(
        self,
    ) -> tuple[RetentionEpoch | None, int, dict[str, Any]]:
        """Read aggregate operator status with one coherent activation epoch."""
        try:
            return await self._run_in_thread(
                sync_read_content_retention_status, self._require_db(), self._lock
            )
        except sqlite3.Error as exc:
            raise StorageError("Reading retention status failed") from exc

    async def count_collected_events(self) -> int:
        """Count the immutable logical-collection receipts in the store.

        Read-only aggregate over ``event_content_collection_log`` for
        operator-facing retention status: one integer, no event identifiers
        and no content. The receipts are the guarded collector's durable
        evidence; no second deletion journal exists.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM event_content_collection_log"
        )
        return int(row["cnt"]) if row is not None else 0

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def open_retention_epoch(
        self, *, activated_by: str, now: datetime
    ) -> RetentionEpoch:
        """Record one relay-only activation epoch with its admission watermark.

        The watermark is the store's maximum local admission ``created_at``
        observed inside the activation transaction; candidates are events
        admitted strictly after it.
        """
        try:
            return await self._run_in_thread(
                sync_open_retention_epoch,
                self._require_db(),
                self._lock,
                activated_by=activated_by,
                now=now,
            )
        except sqlite3.OperationalError as exc:
            raise ContentCollectionTransientError(
                "Opening a retention epoch failed"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError("Opening a retention epoch failed") from exc

    async def latest_retention_epoch(self) -> RetentionEpoch | None:
        """Return the newest durable activation epoch, or ``None`` if absent."""
        try:
            return await self._run_in_thread(
                sync_latest_retention_epoch,
                self._require_db(),
                self._lock,
            )
        except sqlite3.OperationalError as exc:
            raise ContentCollectionTransientError(
                "Reading the retention epoch failed"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError("Reading the retention epoch failed") from exc

    async def observe_retention_heartbeat(self, *, epoch: int, now: datetime) -> None:
        """Advance one epoch's forward-only activity heartbeat.

        A heartbeat observation never moves ``last_observed_active_at``
        backward; a clock rollback is a no-op by construction.
        """
        if type(epoch) is not int or epoch < 1:
            raise ValueError("A retention epoch number must be a positive integer")
        try:
            await self._run_in_thread(
                sync_observe_retention_heartbeat,
                self._require_db(),
                self._lock,
                epoch=epoch,
                now=now,
            )
        except sqlite3.OperationalError as exc:
            raise ContentCollectionTransientError(
                "Recording the retention heartbeat failed"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError("Recording the retention heartbeat failed") from exc

    async def preview_content_retention(
        self, *, limit: int = 100, after: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        """Bounded, metadata-only aggregate preview of one retention epoch.

        Examines at most ``limit`` (1..100) epoch-scoped events with the same
        read-only decision the guarded collector uses, and reports outcome
        counts only — never event IDs, text, or any content value. A store
        with no activation epoch reports the distinct ``no_active_epoch``
        status: collection cannot safely be assessed yet. The preview never
        authorizes retention, deletion, or physical erasure.
        """
        if (
            type(limit) is not int
            or not RETENTION_BATCH_MIN <= limit <= RETENTION_BATCH_MAX
        ):
            raise ValueError("A retention preview limit must be an integer in [1, 100]")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 2
            or not all(isinstance(part, str) and part for part in after)
        ):
            raise ValueError(
                "A retention preview cursor must be a non-empty "
                "(created_at, event_id) pair"
            )
        try:
            return await self._run_in_thread(
                sync_preview_content_retention,
                self._require_db(),
                self._lock,
                limit=limit,
                after=after,
            )
        except sqlite3.Error as exc:
            raise StorageError("Content retention preview failed") from exc

    async def page_collection_candidates(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        watermark: str | None,
    ) -> CandidatePage:
        """Return one bounded, admission-ordered page of candidate event IDs.

        Read-only bounded candidate discovery for a retention sweep: at most
        ``limit`` (1–100) event IDs in the stable ``(created_at, event_id)``
        index ordering, strictly after the activation ``watermark`` when one
        is given, with the ``next_after`` keyset cursor and ``has_more``
        continuing the sweep. There is no implicit wraparound: paging past
        the epoch scope returns an empty page, and a new sweep cycle
        restarts with ``after=None``. The page advance is outcome-
        independent — a permanently blocked event never starves later
        candidates — and a non-advancing cursor is refused. Selection grants
        no mutation authority.
        """
        if (
            type(limit) is not int
            or not RETENTION_BATCH_MIN <= limit <= RETENTION_BATCH_MAX
        ):
            raise ValueError(
                "A collection candidate page limit must be an integer in [1, 100]"
            )
        try:
            return await self._run_in_thread(
                sync_page_collection_candidates,
                self._require_db(),
                self._lock,
                after=after,
                limit=limit,
                watermark=watermark,
            )
        except sqlite3.OperationalError as exc:
            raise ContentCollectionTransientError(
                "Collection candidate paging failed"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError("Collection candidate paging failed") from exc
