"""Durable ingress and application-owned checkpoint storage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from medre.core.events import CanonicalEvent, EventRelation, NativeMessageRef
from medre.core.ingress import (
    INGRESS_PROVENANCE_VALUES,
    INGRESS_WORK_STATUS_VALUES,
    AdapterCheckpoint,
    AdmissionResult,
    IngressProvenance,
    IngressWorkItem,
)
from medre.core.storage.backend import DuplicateEventError, StorageError
from medre.core.storage.sqlite.connection import (
    sync_admit_ingress,
    sync_claim_ingress_work,
    sync_upsert_checkpoint,
)
from medre.core.storage.sqlite.ingress_sql import (
    CLAIM_INGRESS_SELECT,
    CLAIM_INGRESS_UPDATE,
    INSERT_INGRESS_WORK,
    SELECT_CANONICAL_EVENT_ID,
    SELECT_INGRESS_WORK_STATE,
    SELECT_NATIVE_EVENT_ID,
    claimed_ingress_row,
)
from medre.core.storage.sqlite.serde import _encode_json, _now_iso, _serialize_metadata
from medre.core.storage.sqlite.statements import _INSERT_EVENT


class _IngressMixin:
    """Atomic durable ingress and checkpoint methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:
        _db_path: str
        _lock: threading.Lock
        _async_write_lock: asyncio.Lock
        _use_aiosqlite: bool

        async def _run_in_thread(
            self, func: Any, *args: Any, **kwargs: Any
        ) -> Any: ...

        @staticmethod
        def _relation_op(
            event_id: str, relation: EventRelation
        ) -> tuple[str, tuple[Any, ...]]: ...

        def _require_db(self) -> Any: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

        async def _write_rowcount(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> int: ...

    def _event_admission_ops(
        self, event: CanonicalEvent
    ) -> list[tuple[str, tuple[Any, ...]]]:
        snr = event.source_native_ref
        ops: list[tuple[str, tuple[Any, ...]]] = [
            (
                _INSERT_EVENT,
                (
                    event.event_id,
                    event.event_kind,
                    event.schema_version,
                    event.timestamp.isoformat(),
                    event.source_adapter,
                    event.source_transport_id,
                    event.source_channel_id,
                    event.parent_event_id,
                    _encode_json(event.lineage),
                    _encode_json(event.payload),
                    _serialize_metadata(event.metadata),
                    event.depth,
                    event.trace_id,
                    event.root_event_id,
                    event.conversation_id,
                    snr.adapter if snr else None,
                    snr.native_channel_id if snr else None,
                    snr.native_message_id if snr else None,
                    snr.native_thread_id if snr else None,
                    _now_iso(),
                ),
            )
        ]
        ops.extend(
            self._relation_op(event.event_id, relation)
            for relation in event.relations
        )
        return ops

    async def admit_ingress(
        self,
        event: CanonicalEvent,
        inbound_ref: NativeMessageRef | None,
        provenance: IngressProvenance,
        *,
        suppress_routing: bool = False,
    ) -> AdmissionResult:
        """Atomically persist event, inbound native ref, and durable work.

        The native identity is the idempotency key when one is available.
        Duplicate native admission returns the original canonical event ID
        without creating a second event or work row.
        """
        if provenance not in INGRESS_PROVENANCE_VALUES:
            raise ValueError(f"unsupported ingress provenance: {provenance!r}")
        if inbound_ref is not None and inbound_ref.event_id != event.event_id:
            raise ValueError("inbound_ref.event_id must match event.event_id")

        now = _now_iso()
        suppress = suppress_routing or provenance == "history"
        work_status = "suppressed_history" if suppress else "pending"
        event_ops = self._event_admission_ops(event)
        native_identity = None
        native_insert = None
        if inbound_ref is not None:
            native_identity = (
                inbound_ref.adapter,
                inbound_ref.native_channel_id,
                inbound_ref.native_message_id,
            )
            native_insert = (
                """
                INSERT INTO native_message_refs
                    (id, event_id, adapter, native_channel_id, native_message_id,
                     native_thread_id, native_relation_id, direction, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inbound_ref.id,
                    inbound_ref.event_id,
                    inbound_ref.adapter,
                    inbound_ref.native_channel_id,
                    inbound_ref.native_message_id,
                    inbound_ref.native_thread_id,
                    inbound_ref.native_relation_id,
                    inbound_ref.direction,
                    _encode_json(inbound_ref.metadata),
                    inbound_ref.created_at.isoformat(),
                ),
            )
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    try:
                        await db.execute("BEGIN IMMEDIATE")
                        existing_event_id: str | None = None
                        if native_identity is not None:
                            async with db.execute(
                                SELECT_NATIVE_EVENT_ID, native_identity,
                            ) as cur:
                                row = await cur.fetchone()
                            if row is not None:
                                existing_event_id = str(row[0])
                        else:
                            async with db.execute(
                                SELECT_CANONICAL_EVENT_ID, (event.event_id,),
                            ) as cur:
                                row = await cur.fetchone()
                            if row is not None:
                                existing_event_id = event.event_id
                        if existing_event_id is not None:
                            async with db.execute(
                                "SELECT 1 FROM durable_ingress_work WHERE event_id = ?",
                                (existing_event_id,),
                            ) as cur:
                                work_row = await cur.fetchone()
                            if work_row is None:
                                await db.execute(
                                    INSERT_INGRESS_WORK,
                                    (
                                        existing_event_id,
                                        provenance,
                                        work_status,
                                        now,
                                        now,
                                    ),
                                )
                            await db.commit()
                            return await self._admission_result_for_existing(
                                existing_event_id, provenance
                            )

                        for sql, params in event_ops:
                            await db.execute(sql, params)
                        if native_insert is not None:
                            await db.execute(*native_insert)
                        await db.execute(
                            INSERT_INGRESS_WORK,
                            (event.event_id, provenance, work_status, now, now),
                        )
                        await db.commit()
                        return AdmissionResult(
                            event_id=event.event_id,
                            created=True,
                            provenance=provenance,
                            work_status=work_status,
                        )
                    except BaseException:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise
            event_id, created = await self._run_in_thread(
                sync_admit_ingress,
                db,
                self._lock,
                event_ops=event_ops,
                native_identity=native_identity,
                native_insert=native_insert,
                event_id=event.event_id,
                provenance=provenance,
                work_status=work_status,
                now_iso=now,
            )
            if not created:
                return await self._admission_result_for_existing(event_id, provenance)
            return AdmissionResult(
                event_id=event_id,
                created=True,
                provenance=provenance,
                work_status=work_status,
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "canonical_events" in msg and "UNIQUE constraint failed" in msg:
                raise DuplicateEventError(f"Duplicate event: {exc}") from exc
            raise StorageError(f"Durable ingress admission failed: {exc}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"Durable ingress admission failed: {exc}") from exc

    async def _admission_result_for_existing(
        self, event_id: str, requested_provenance: IngressProvenance
    ) -> AdmissionResult:
        row = await self._read_one(
            SELECT_INGRESS_WORK_STATE,
            (event_id,),
        )
        if row is None:
            raise StorageError(
                f"durable ingress work missing for admitted event {event_id}"
            )
        provenance = requested_provenance
        status: str = "pending"
        stored_provenance = row.get("provenance")
        if stored_provenance in INGRESS_PROVENANCE_VALUES:
            provenance = stored_provenance
        stored_status = row.get("status")
        if stored_status in INGRESS_WORK_STATUS_VALUES:
            status = stored_status
        return AdmissionResult(
            event_id=event_id,
            created=False,
            provenance=provenance,
            work_status=status,
        )

    async def put_adapter_checkpoint(
        self,
        adapter_id: str,
        stream: str,
        cursor: str,
        *,
        metadata_json: str = "{}",
    ) -> None:
        """Persist an application-owned adapter cursor."""
        try:
            decoded_metadata = json.loads(metadata_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata_json must contain valid JSON") from exc
        if not isinstance(decoded_metadata, dict):
            raise ValueError("metadata_json must encode a JSON object")
        now = _now_iso()
        params = (adapter_id, stream, cursor, metadata_json, now)
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    await db.execute(
                        """
                        INSERT INTO adapter_checkpoints
                            (adapter_id, stream, cursor, metadata, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(adapter_id, stream) DO UPDATE SET
                            cursor=excluded.cursor,
                            metadata=excluded.metadata,
                            updated_at=excluded.updated_at
                        """,
                        params,
                    )
                    await db.commit()
            else:
                await self._run_in_thread(sync_upsert_checkpoint, db, self._lock, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Checkpoint write failed: {exc}") from exc

    async def get_adapter_checkpoint(
        self, adapter_id: str, stream: str
    ) -> AdapterCheckpoint | None:
        """Return the last committed application-owned adapter cursor."""
        row = await self._read_one(
            """
            SELECT adapter_id, stream, cursor, metadata, updated_at
            FROM adapter_checkpoints WHERE adapter_id = ? AND stream = ?
            """,
            (adapter_id, stream),
        )
        if row is None:
            return None
        return AdapterCheckpoint(
            adapter_id=row["adapter_id"],
            stream=row["stream"],
            cursor=row["cursor"],
            metadata_json=row["metadata"],
            updated_at=row["updated_at"],
        )
    async def claim_ingress_work(
        self,
        *,
        worker_id: str,
        limit: int = 25,
        lease_seconds: float = 30.0,
    ) -> list[IngressWorkItem]:
        """Claim pending or lease-expired ingress work atomically."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    try:
                        await db.execute("BEGIN IMMEDIATE")
                        async with db.execute(
                            CLAIM_INGRESS_SELECT, (now_iso, limit)
                        ) as cur:
                            rows = await cur.fetchall()
                        claimed: list[dict[str, Any]] = []
                        for row in rows:
                            event_id = str(row[0])
                            await db.execute(
                                CLAIM_INGRESS_UPDATE,
                                (now_iso, lease_until, worker_id, now_iso, event_id),
                            )
                            claimed.append(
                                claimed_ingress_row(
                                    row,
                                    now_iso=now_iso,
                                    lease_until=lease_until,
                                    worker_id=worker_id,
                                )
                            )
                        await db.commit()
                    except BaseException:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise
            else:
                claimed = await self._run_in_thread(
                    sync_claim_ingress_work,
                    db,
                    self._lock,
                    now_iso=now_iso,
                    lease_until=lease_until,
                    worker_id=worker_id,
                    limit=limit,
                )
            return [
                IngressWorkItem(
                    event_id=row["event_id"],
                    provenance=row["provenance"],
                    status=row["status"],
                    attempts=row["attempts"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    locked_at=row["locked_at"],
                    lease_until=row["lease_until"],
                    worker_id=row["worker_id"],
                )
                for row in claimed
            ]
        except sqlite3.Error as exc:
            raise StorageError(f"Ingress work claim failed: {exc}") from exc

    async def complete_ingress_work(
        self, event_id: str, *, worker_id: str
    ) -> bool:
        """Mark owned ingress work complete after durable delivery planning."""
        changed = await self._write_rowcount(
            """
            UPDATE durable_ingress_work
            SET status='completed', updated_at=?, locked_at=NULL,
                lease_until=NULL, worker_id=NULL, last_error=NULL
            WHERE event_id=? AND status='processing' AND worker_id=?
            """,
            (_now_iso(), event_id, worker_id),
        )
        return changed == 1

    async def release_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool:
        """Return owned ingress work to pending after a processing failure."""
        changed = await self._write_rowcount(
            """
            UPDATE durable_ingress_work
            SET status='pending', updated_at=?, locked_at=NULL, lease_until=NULL,
                worker_id=NULL, last_error=?
            WHERE event_id=? AND status='processing' AND worker_id=?
            """,
            (_now_iso(), error[:1000], event_id, worker_id),
        )
        return changed == 1

    async def fail_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool:
        """Move owned ingress work to terminal ``failed`` state."""
        changed = await self._write_rowcount(
            """
            UPDATE durable_ingress_work
            SET status='failed', updated_at=?, locked_at=NULL, lease_until=NULL,
                worker_id=NULL, last_error=?
            WHERE event_id=? AND status='processing' AND worker_id=?
            """,
            (_now_iso(), error[:1000], event_id, worker_id),
        )
        return changed == 1

    async def renew_ingress_work_lease(
        self,
        event_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        """Renew the lease for processing work still owned by ``worker_id``."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        changed = await self._write_rowcount(
            """
            UPDATE durable_ingress_work
            SET lease_until=?, updated_at=?
            WHERE event_id=? AND status='processing' AND worker_id=?
            """,
            (lease_until, now.isoformat(), event_id, worker_id),
        )
        return changed == 1

    async def count_ingress_work_by_status(self) -> dict[str, int]:
        """Return durable ingress work counts grouped by status."""
        rows = await self._read_all(
            "SELECT status, COUNT(*) AS count FROM durable_ingress_work GROUP BY status"
        )
        return {str(row["status"]): int(row["count"]) for row in rows}
