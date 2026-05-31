"""Delivery outbox mixins for SQLiteStorage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS
from medre.core.storage.sqlite.serde import (
    _add_seconds_iso,
    _encode_json,
    _ensure_iso,
    _now_iso,
    _row_to_outbox_item,
)


class _OutboxMixin:
    """Delivery outbox methods for SQLiteStorage.

    Accesses ``self._read_one``, ``self._read_all``, ``self._write``,
    ``self._run_in_thread``, ``self._require_db``, ``self._async_write_lock``,
    and ``self._lock`` from the base class via MRO.
    """

    async def create_outbox_item(self, item: DeliveryOutboxItem) -> DeliveryOutboxItem:
        """Create a new outbox item.

        Checks for an existing item with the same key tuple
        ``(delivery_plan_id, target_adapter, target_channel, attempt_number)``
        before inserting.  If an existing item has a **reclaimable** status
        (``pending`` or ``retry_wait``), it is reclaimed: its ``status``,
        ``worker_id``, ``locked_at``, and ``lease_until`` are updated to
        match the new item's values so the caller always receives a
        properly-claimed row.  If the existing item is **active**
        (``in_progress`` or ``queued``), it is returned unchanged — active
        work is never stolen.  If the existing item is terminal it is
        deleted first so a new row for re-delivery can be inserted without
        violating the UNIQUE constraint.

        The entire SELECT + conditional DELETE + INSERT runs inside a
        single ``BEGIN IMMEDIATE`` transaction so that two concurrent
        callers cannot both pass the existence check and race on INSERT.
        If the INSERT still fails with a UNIQUE constraint violation
        (extreme edge case), the existing row is re-read and returned.
        """
        _terminal = frozenset({"sent", "dead_lettered", "cancelled", "abandoned"})
        _reclaimable = frozenset({"pending", "retry_wait"})
        now = _now_iso()
        meta_json = _encode_json(item.metadata or {})

        select_sql = (
            "SELECT outbox_id, status FROM delivery_outbox"
            " WHERE delivery_plan_id = ? AND target_adapter = ?"
            " AND target_channel IS ? AND attempt_number = ?"
        )
        select_params = (
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel or None,
            item.attempt_number,
        )
        delete_sql = "DELETE FROM delivery_outbox WHERE outbox_id = ?"
        insert_sql = (
            "INSERT INTO delivery_outbox"
            " (outbox_id, event_id, route_id, delivery_plan_id,"
            "  target_adapter, target_channel, target_address,"
            "  attempt_number, status, failure_kind, failure_kind_detail,"
            "  next_attempt_at, created_at, updated_at, last_attempt_at,"
            "  locked_at, lease_until, worker_id, payload_hash,"
            "  receipt_id, parent_receipt_id, error_summary, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        insert_params = (
            item.outbox_id,
            item.event_id,
            item.route_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel or None,
            item.target_address,
            item.attempt_number,
            item.status or "pending",
            item.failure_kind,
            item.failure_kind_detail,
            _ensure_iso(item.next_attempt_at),
            _ensure_iso(item.created_at) or now,
            _ensure_iso(item.updated_at) or now,
            _ensure_iso(item.last_attempt_at),
            _ensure_iso(item.locked_at),
            _ensure_iso(item.lease_until),
            item.worker_id,
            item.payload_hash,
            item.receipt_id,
            item.parent_receipt_id,
            item.error_summary,
            meta_json,
        )

        if self._use_aiosqlite:
            async with self._async_write_lock:
                db = self._require_db()
                try:
                    await db.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
                    # SELECT existing
                    async with db.execute(select_sql, select_params) as cur:  # type: ignore[union-attr]
                        row = await cur.fetchone()
                    if row is not None:
                        existing = dict(row)
                        if existing["status"] in _reclaimable:
                            # Re-claim: update status, worker, and lease
                            # so the pipeline always gets an in_progress
                            # row with a valid lease for finalization.
                            # Clear next_attempt_at since the item is no
                            # longer waiting for a scheduled retry.
                            await db.execute(  # type: ignore[union-attr]
                                """UPDATE delivery_outbox
                                   SET status = ?, worker_id = ?,
                                       locked_at = ?, lease_until = ?,
                                       updated_at = ?,
                                       next_attempt_at = NULL
                                   WHERE outbox_id = ?""",
                                (
                                    item.status or "pending",
                                    item.worker_id,
                                    _ensure_iso(item.locked_at),
                                    _ensure_iso(item.lease_until),
                                    now,
                                    existing["outbox_id"],
                                ),
                            )
                            await db.execute("COMMIT")  # type: ignore[union-attr]
                            return (
                                await self.get_outbox_item(existing["outbox_id"])
                                or item
                            )
                        if existing["status"] in _terminal:
                            # Terminal — delete so re-insertion can proceed.
                            await db.execute(delete_sql, (existing["outbox_id"],))  # type: ignore[union-attr]
                        else:
                            # Active (in_progress or queued) — return
                            # unchanged to avoid stealing active work.
                            await db.execute("COMMIT")  # type: ignore[union-attr]
                            return (
                                await self.get_outbox_item(existing["outbox_id"])
                                or item
                            )
                    await db.execute(insert_sql, insert_params)  # type: ignore[union-attr]
                    await db.execute("COMMIT")  # type: ignore[union-attr]
                except sqlite3.IntegrityError:
                    # UNIQUE race: another writer inserted between our SELECT
                    # and INSERT.  Rollback if the transaction is still active,
                    # then re-read the winning row.
                    try:
                        await db.execute("ROLLBACK")  # type: ignore[union-attr]
                    except Exception:
                        pass
                    existing = await self._read_one(select_sql, select_params)
                    if existing is not None:
                        return await self.get_outbox_item(existing["outbox_id"]) or item
                    raise
                except BaseException:
                    # Ensure the transaction is rolled back on any other failure
                    # (e.g. operational error between BEGIN and COMMIT) so the
                    # connection is not left with an open transaction.
                    try:
                        await db.execute("ROLLBACK")  # type: ignore[union-attr]
                    except Exception:
                        pass
                    raise
        else:
            try:
                existing_id = await self._run_in_thread(
                    self._sync_atomic_create_outbox,
                    self._require_db(),
                    select_sql,
                    select_params,
                    delete_sql,
                    insert_sql,
                    insert_params,
                    _terminal,
                    _reclaimable,
                    reclaim_status=item.status or "pending",
                    reclaim_worker_id=item.worker_id,
                    reclaim_locked_at=_ensure_iso(item.locked_at),
                    reclaim_lease_until=_ensure_iso(item.lease_until),
                    reclaim_now=now,
                )
                if existing_id is not None:
                    return await self.get_outbox_item(existing_id) or item
            except sqlite3.IntegrityError:
                existing = await self._read_one(select_sql, select_params)
                if existing is not None:
                    return await self.get_outbox_item(existing["outbox_id"]) or item
                raise

        return await self.get_outbox_item(item.outbox_id) or item

    def _sync_atomic_create_outbox(
        self,
        db: sqlite3.Connection,
        select_sql: str,
        select_params: tuple[Any, ...],
        delete_sql: str,
        insert_sql: str,
        insert_params: tuple[Any, ...],
        terminal: frozenset[str],
        reclaimable: frozenset[str],
        *,
        reclaim_status: str = "pending",
        reclaim_worker_id: str | None = None,
        reclaim_locked_at: str | None = None,
        reclaim_lease_until: str | None = None,
        reclaim_now: str = "",
    ) -> str | None:
        """Synchronous helper: BEGIN IMMEDIATE, SELECT, optional DELETE/UPDATE, INSERT, COMMIT.

        Returns the existing outbox_id when a reclaimable row was found
        (idempotent — the row is reclaimed with new status/worker/lease),
        or the existing outbox_id when an active (in_progress/queued) row
        was found (returned unchanged).  Returns None when a new row was
        inserted.
        """
        with self._lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(select_sql, select_params).fetchone()
                if row is not None:
                    existing = dict(row)
                    if existing["status"] in reclaimable:
                        # Re-claim: update status, worker, and lease.
                        # Clear next_attempt_at since the item is no
                        # longer waiting for a scheduled retry.
                        db.execute(
                            """UPDATE delivery_outbox
                               SET status = ?, worker_id = ?,
                                   locked_at = ?, lease_until = ?,
                                   updated_at = ?,
                                   next_attempt_at = NULL
                               WHERE outbox_id = ?""",
                            (
                                reclaim_status,
                                reclaim_worker_id,
                                reclaim_locked_at,
                                reclaim_lease_until,
                                reclaim_now,
                                existing["outbox_id"],
                            ),
                        )
                        db.execute("COMMIT")
                        return existing["outbox_id"]
                    if existing["status"] in terminal:
                        db.execute(delete_sql, (existing["outbox_id"],))
                    else:
                        # Active (in_progress or queued) — return unchanged.
                        db.execute("COMMIT")
                        return existing["outbox_id"]
                db.execute(insert_sql, insert_params)
                db.execute("COMMIT")
                return None
            except BaseException:
                try:
                    db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None:
        """Retrieve a single outbox item by its ID."""
        row = await self._read_one(
            "SELECT * FROM delivery_outbox WHERE outbox_id = ?",
            (outbox_id,),
        )
        if row is None:
            return None
        return _row_to_outbox_item(row)

    async def get_outbox_item_for_delivery(
        self,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        status: str | None = None,
    ) -> DeliveryOutboxItem | None:
        """Retrieve an outbox item by its delivery target key.

        Performs a targeted SELECT matching *event_id*,
        *delivery_plan_id*, *target_adapter*, *target_channel*
        (using ``IS`` for proper ``NULL`` handling) and optionally
        *status*.  Returns the first match or ``None``.
        """
        clauses = [
            "event_id = ?",
            "delivery_plan_id = ?",
            "target_adapter = ?",
            "target_channel IS ?",
        ]
        channel = target_channel or None
        params: list[Any] = [event_id, delivery_plan_id, target_adapter, channel]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        row = await self._read_one(
            f"SELECT * FROM delivery_outbox WHERE {where} LIMIT 1",  # nosec: where clause built from hardcoded identifiers only, values via ? params
            tuple(params),
        )
        if row is None:
            return None
        return _row_to_outbox_item(row)

    async def list_outbox_items(
        self,
        status_filter: list[str] | None = None,
        due_before: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DeliveryOutboxItem]:
        """List outbox items matching optional status and due filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if status_filter:
            holders = ",".join("?" for _ in status_filter)
            clauses.append(f"status IN ({holders})")
            params.extend(status_filter)

        if due_before is not None:
            clauses.append("(next_attempt_at IS NOT NULL AND next_attempt_at <= ?)")
            params.append(due_before)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM delivery_outbox{where} ORDER BY next_attempt_at ASC, created_at ASC LIMIT ? OFFSET ?"  # nosec
        params.append(limit)
        params.append(offset)

        rows = await self._read_all(sql, tuple(params))
        return [_row_to_outbox_item(r) for r in rows]

    async def list_outbox_items_for_event(
        self,
        event_id: str,
    ) -> list[DeliveryOutboxItem]:
        """Return all outbox items for a specific event.

        Ordered by ``created_at ASC, outbox_id ASC`` for deterministic
        output.  Read-only — does not mutate storage.
        """
        rows = await self._read_all(
            "SELECT * FROM delivery_outbox WHERE event_id = ? "
            "ORDER BY created_at ASC, outbox_id ASC",
            (event_id,),
        )
        return [_row_to_outbox_item(r) for r in rows]

    async def claim_due_outbox_items(
        self,
        now: str,
        worker_id: str,
        lease_seconds: int = 30,
        limit: int = 20,
    ) -> list[DeliveryOutboxItem]:
        """Atomically claim due outbox items for processing.

        Uses a transaction to SELECT FOR UPDATE equivalent (rowid-based)
        and updates in one step.  Claims items that are:

        - ``status IN ('pending', 'retry_wait')`` — directly claimable;
        - ``status = 'in_progress' AND lease_until <= now`` — expired leases;
        - ``status = 'queued' AND updated_at <= now - GRACE`` — stale
          queued items past the grace threshold
          (:data:`STALE_QUEUED_GRACE_SECONDS`).

        Additional guards:

        - ``(next_attempt_at IS NULL OR next_attempt_at <= now)``
        - ``(lease_until IS NULL OR lease_until <= now)``

        When moving a row to ``in_progress``, ``next_attempt_at`` is
        cleared (set to ``NULL``) since the item is no longer waiting
        for a scheduled retry.
        """
        lease_until = _add_seconds_iso(now, lease_seconds)
        stale_cutoff = (
            datetime.fromisoformat(now) - timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)
        ).isoformat()
        # Use a two-step approach: SELECT candidates, then UPDATE matching.
        # SQLite doesn't support RETURNING with ORIGIN in all configurations,
        # so we select first, then update by outbox_id.
        rows = await self._read_all(
            """SELECT * FROM delivery_outbox
               WHERE (status IN ('pending', 'retry_wait')
                      OR (status = 'in_progress' AND lease_until <= ?)
                      OR (status = 'queued' AND updated_at <= ?))
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 AND (lease_until IS NULL OR lease_until <= ?)
               ORDER BY next_attempt_at ASC, created_at ASC
               LIMIT ?""",
            (now, stale_cutoff, now, now, limit),
        )
        if not rows:
            return []

        outbox_ids = [r["outbox_id"] for r in rows]
        placeholders = ",".join("?" for _ in outbox_ids)
        await self._write(
            f"""UPDATE delivery_outbox
                SET status = 'in_progress',
                    locked_at = ?,
                    lease_until = ?,
                    worker_id = ?,
                    updated_at = ?,
                    next_attempt_at = NULL
                WHERE outbox_id IN ({placeholders})
                  AND (status IN ('pending', 'retry_wait')
                       OR (status = 'in_progress' AND lease_until <= ?)
                       OR (status = 'queued' AND updated_at <= ?))
                  AND (lease_until IS NULL OR lease_until <= ?)""",  # nosec: placeholders are only ? markers, values passed as params
            (now, lease_until, worker_id, now, *outbox_ids, now, stale_cutoff, now),
        )

        # Re-read to get the updated rows (some may have been claimed by
        # another worker if the SELECT/UPDATE window was contested).
        final_rows = await self._read_all(
            f"SELECT * FROM delivery_outbox WHERE outbox_id IN ({','.join('?' for _ in outbox_ids)}) AND worker_id = ? AND status = 'in_progress'",  # nosec: placeholders are only ? markers, values passed as params
            (*outbox_ids, worker_id),
        )
        return [_row_to_outbox_item(r) for r in final_rows]

    async def _update_outbox_status(
        self,
        outbox_id: str,
        new_status: str,
        *,
        allowed_from: tuple[str, ...] | None = None,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        """Shared helper for status transitions.

        Only updates non-terminal items.  The ``WHERE status NOT IN``
        clause prevents regression once a terminal status is set.
        If *allowed_from* is provided, an additional ``AND status IN
        (...)`` guard is added so the transition is only valid from
        the listed source statuses.
        """
        now = _now_iso()
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [new_status, now]

        if receipt_id is not None:
            sets.append("receipt_id = ?")
            params.append(receipt_id)
        if attempt_number is not None:
            sets.append("attempt_number = ?")
            params.append(attempt_number)
        if failure_kind is not None:
            sets.append("failure_kind = ?")
            params.append(failure_kind)
        if failure_kind_detail is not None:
            sets.append("failure_kind_detail = ?")
            params.append(failure_kind_detail)
        if error_summary is not None:
            sets.append("error_summary = ?")
            params.append(error_summary)
        if next_attempt_at is not None:
            sets.append("next_attempt_at = ?")
            params.append(next_attempt_at)
        elif new_status == "retry_wait":
            # retry_wait MUST have next_attempt_at — defensive guard.
            raise ValueError(
                "next_attempt_at is required when transitioning to retry_wait"
            )
        else:
            sets.append("next_attempt_at = NULL")

        if new_status in ("queued", "sent"):
            sets.extend(
                [
                    "failure_kind = NULL",
                    "failure_kind_detail = NULL",
                    "error_summary = NULL",
                ]
            )
        elif new_status in ("dead_lettered", "cancelled", "abandoned"):
            # Clear next_attempt_at is handled above; also clear
            # failure_kind_detail which is not caller-specified for
            # these terminal transitions.  Keep failure_kind and
            # error_summary as callers pass meaningful values.
            if failure_kind_detail is None:
                sets.append("failure_kind_detail = NULL")
        if new_status in ("queued", "sent", "retry_wait"):
            sets.append("last_attempt_at = ?")
            params.append(now)
        if new_status in (
            "sent",
            "dead_lettered",
            "cancelled",
            "abandoned",
            "retry_wait",
            "queued",
        ):
            sets.append("locked_at = NULL")
            sets.append("lease_until = NULL")
            sets.append("worker_id = NULL")

        where_clauses = [
            "outbox_id = ?",
            "status NOT IN ('sent', 'dead_lettered', 'cancelled', 'abandoned')",
        ]
        params.append(outbox_id)
        if allowed_from is not None:
            holders = ",".join("?" for _ in allowed_from)
            where_clauses.append(f"status IN ({holders})")
            params.extend(allowed_from)

        set_clause = ", ".join(sets)
        where_sql = " AND ".join(where_clauses)
        await self._write(
            f"UPDATE delivery_outbox SET {set_clause} WHERE {where_sql}",  # nosec: set_clause contains only hardcoded column names, values via ? params
            tuple(params),
        )

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``sent`` (terminal).

        Only transitions from ``in_progress`` or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "sent",
            allowed_from=("in_progress", "queued"),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
        )

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``queued`` (adapter-local queue acceptance).

        Only transitions from ``in_progress``.
        """
        await self._update_outbox_status(
            outbox_id,
            "queued",
            allowed_from=("in_progress",),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
        )

    async def mark_outbox_retry_wait(
        self,
        outbox_id: str,
        next_attempt_at: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``retry_wait`` (transient failure).

        Sets ``next_attempt_at`` for the next scheduled attempt.
        Only transitions from ``in_progress``.
        """
        await self._update_outbox_status(
            outbox_id,
            "retry_wait",
            allowed_from=("in_progress",),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
            failure_kind=failure_kind,
            failure_kind_detail=failure_kind_detail,
            error_summary=error_summary,
            next_attempt_at=next_attempt_at,
        )

    async def mark_outbox_dead_lettered(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``dead_lettered`` (terminal failure).

        Only transitions from ``in_progress`` or ``retry_wait``.
        """
        await self._update_outbox_status(
            outbox_id,
            "dead_lettered",
            allowed_from=("in_progress", "retry_wait"),
            receipt_id=receipt_id,
            failure_kind=failure_kind,
            failure_kind_detail=failure_kind_detail,
            error_summary=error_summary,
        )

    async def mark_outbox_cancelled(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``cancelled`` (terminal).

        May be called from ``pending``, ``in_progress``, ``retry_wait``,
        or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "cancelled",
            allowed_from=("pending", "in_progress", "retry_wait", "queued"),
            error_summary=error_summary,
        )

    async def mark_outbox_abandoned(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``abandoned`` (terminal).

        May be called from ``pending``, ``in_progress``, ``retry_wait``,
        or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "abandoned",
            allowed_from=("pending", "in_progress", "retry_wait", "queued"),
            error_summary=error_summary,
        )

    async def renew_outbox_lease(
        self,
        outbox_id: str,
        worker_id: str,
        lease_until: str,
    ) -> bool:
        """Renew the lease on an in_progress outbox item.

        Returns True if the lease was renewed, False if the item is no
        longer owned by this worker or is not in_progress.
        """
        now = _now_iso()
        await self._write(
            """UPDATE delivery_outbox
               SET lease_until = ?, updated_at = ?
               WHERE outbox_id = ?
                 AND worker_id = ?
                 AND status = 'in_progress'""",
            (lease_until, now, outbox_id, worker_id),
        )
        # Verify the update matched a row.
        row = await self._read_one(
            "SELECT outbox_id FROM delivery_outbox WHERE outbox_id = ? AND worker_id = ? AND status = 'in_progress'",
            (outbox_id, worker_id),
        )
        return row is not None

    async def release_outbox_claim(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        release_status: str = "pending",
    ) -> None:
        """Release a claim on an outbox item, restoring the caller-specified status.

        Clears locked_at, lease_until, worker_id and sets status to
        *release_status*.  Only succeeds when the current worker_id matches.
        """
        _allowed_release_statuses = {
            "pending",
            "retry_wait",
        }
        if release_status not in _allowed_release_statuses:
            raise ValueError(f"Invalid release_status: {release_status!r}")

        await self._write(
            """UPDATE delivery_outbox
               SET locked_at = NULL, lease_until = NULL, worker_id = NULL,
                   status = ?, updated_at = ?
               WHERE outbox_id = ? AND worker_id = ?
                 AND status NOT IN ('sent', 'dead_lettered', 'cancelled', 'abandoned')""",
            (release_status, _now_iso(), outbox_id, worker_id),
        )

    async def count_outbox_by_status(self) -> dict[str, int]:
        """Return counts of outbox items grouped by status."""
        rows = await self._read_all(
            "SELECT status, COUNT(*) AS cnt FROM delivery_outbox GROUP BY status"
        )
        return {r["status"]: r["cnt"] for r in rows}
