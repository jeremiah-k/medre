"""Durable ingress and application-owned checkpoint storage."""

from __future__ import annotations

import sqlite3
from typing import Any

from medre.core.events import CanonicalEvent, NativeMessageRef
from medre.core.ingress import AdapterCheckpoint, AdmissionResult, IngressProvenance
from medre.core.storage.backend import DuplicateEventError, StorageError
from medre.core.storage.sqlite.connection import sync_admit_ingress, sync_upsert_checkpoint
from medre.core.storage.sqlite.serde import _encode_json, _now_iso, _serialize_metadata
from medre.core.storage.sqlite.statements import _INSERT_EVENT


class _IngressMixin:
    """Atomic durable ingress and checkpoint methods for ``SQLiteStorage``."""

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
        for rel in event.relations:
            ops.append(self._relation_op(event.event_id, rel))
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
        if provenance not in {"live", "recovered", "history"}:
            raise ValueError(f"unsupported ingress provenance: {provenance!r}")
        if inbound_ref is not None and inbound_ref.event_id != event.event_id:
            raise ValueError("inbound_ref.event_id must match event.event_id")

        now = _now_iso()
        work_status = "suppressed_history" if suppress_routing else "pending"
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
        work_insert = (
            """
            INSERT INTO durable_ingress_work
                (event_id, provenance, status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (event.event_id, provenance, work_status, now, now),
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
                                """
                                SELECT event_id FROM native_message_refs
                                WHERE adapter = ? AND native_channel_id IS ?
                                  AND native_message_id = ?
                                """,
                                native_identity,
                            ) as cur:
                                row = await cur.fetchone()
                            if row is not None:
                                existing_event_id = str(row[0])
                        else:
                            async with db.execute(
                                "SELECT event_id FROM canonical_events WHERE event_id = ?",
                                (event.event_id,),
                            ) as cur:
                                row = await cur.fetchone()
                            if row is not None:
                                existing_event_id = event.event_id
                        if existing_event_id is not None:
                            await db.rollback()
                            return await self._admission_result_for_existing(
                                existing_event_id, provenance
                            )

                        for sql, params in event_ops:
                            await db.execute(sql, params)
                        if native_insert is not None:
                            await db.execute(*native_insert)
                        await db.execute(*work_insert)
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
                work_insert=work_insert,
                event_id=event.event_id,
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
            "SELECT provenance, status FROM durable_ingress_work WHERE event_id = ?",
            (event_id,),
        )
        provenance = requested_provenance
        status = "completed"
        if row is not None:
            stored_provenance = row.get("provenance")
            if stored_provenance in {"live", "recovered", "history"}:
                provenance = stored_provenance
            stored_status = row.get("status")
            if stored_status in {
                "pending",
                "processing",
                "suppressed_history",
                "completed",
            }:
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
