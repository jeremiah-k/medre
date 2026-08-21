"""Atomic cross-table delivery finalization for SQLiteStorage."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from medre.core.events import DeliveryReceipt, NativeMessageRef
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._native_ref import (
    _native_ref_identity,
    _native_ref_insert_params,
)
from medre.core.storage.sqlite._receipt import _receipt_insert_params
from medre.core.storage.sqlite.connection import sync_finalize_queued_delivery
from medre.core.storage.sqlite.statements import (
    _FINALIZE_QUEUED_OUTBOX_SENT,
    _INSERT_NATIVE_REF_STRICT,
    _INSERT_RECEIPT,
    _RESOLVE_NATIVE_REF,
)


class _DeliveryFinalizationMixin:
    """Cross-table finalization methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:
        _lock: threading.Lock
        _async_write_lock: asyncio.Lock
        _use_aiosqlite: bool

        def _require_db(self) -> Any: ...

        async def _run_in_thread(self, func: Any, *args: Any, **kwargs: Any) -> Any: ...

    @staticmethod
    def _validate_queued_delivery_finalization(
        native_ref: NativeMessageRef,
        receipt: DeliveryReceipt,
        outbox_id: str,
        attempt_number: int,
    ) -> None:
        if native_ref.direction != "outbound":
            raise ValueError(
                "queued delivery finalization requires an outbound native ref"
            )
        if receipt.status != "sent":
            raise ValueError("queued delivery finalization requires a sent receipt")
        if native_ref.event_id != receipt.event_id:
            raise ValueError("native_ref.event_id must match receipt.event_id")
        if native_ref.adapter != receipt.target_adapter:
            raise ValueError("native_ref.adapter must match receipt.target_adapter")
        if native_ref.native_message_id != receipt.adapter_message_id:
            raise ValueError(
                "native_ref.native_message_id must match receipt.adapter_message_id"
            )
        if receipt.outbox_id != outbox_id:
            raise ValueError("receipt.outbox_id must match outbox_id")
        if receipt.attempt_number != attempt_number:
            raise ValueError("receipt.attempt_number must match attempt_number")
        if attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")

    async def finalize_queued_delivery(
        self,
        native_ref: NativeMessageRef,
        receipt: DeliveryReceipt,
        *,
        outbox_id: str,
        attempt_number: int,
    ) -> bool:
        """Atomically persist queue-send evidence and mark its outbox sent.

        The transaction commits the outbound native reference, immutable sent
        receipt, and guarded outbox transition together.  It returns ``False``
        when the exact outbox attempt is no longer in ``queued`` or
        ``in_progress`` state.  A native identity already mapped to another
        canonical event is a storage-integrity error.
        """
        self._validate_queued_delivery_finalization(
            native_ref, receipt, outbox_id, attempt_number
        )
        receipt_params = _receipt_insert_params(receipt)
        native_identity = _native_ref_identity(native_ref)
        native_params = _native_ref_insert_params(native_ref)
        transition_time = receipt.created_at.isoformat()
        outbox_params: tuple[object, ...] = (
            transition_time,
            transition_time,
            receipt.receipt_id,
            outbox_id,
            attempt_number,
        )

        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with self._async_write_lock:
                    try:
                        await db.execute("BEGIN IMMEDIATE")
                        async with db.execute(
                            _RESOLVE_NATIVE_REF, native_identity
                        ) as cursor:
                            row = await cursor.fetchone()
                        existing_event_id = str(row[0]) if row is not None else None
                        if (
                            existing_event_id is not None
                            and existing_event_id != native_ref.event_id
                        ):
                            await db.rollback()
                            raise StorageError(
                                "Native identity already maps to a different canonical "
                                f"event: {existing_event_id}"
                            )

                        async with db.execute(
                            _FINALIZE_QUEUED_OUTBOX_SENT, outbox_params
                        ) as cursor:
                            changed = int(cursor.rowcount)
                        if changed != 1:
                            await db.rollback()
                            return False

                        if existing_event_id is None:
                            await db.execute(_INSERT_NATIVE_REF_STRICT, native_params)
                        await db.execute(_INSERT_RECEIPT, receipt_params)
                        await db.commit()
                        return True
                    except BaseException:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise

            committed, conflict_event_id = await self._run_in_thread(
                sync_finalize_queued_delivery,
                db,
                self._lock,
                native_identity=native_identity,
                native_event_id=native_ref.event_id,
                native_insert_params=native_params,
                receipt_insert_params=receipt_params,
                outbox_update_params=outbox_params,
            )
            if conflict_event_id is not None:
                raise StorageError(
                    "Native identity already maps to a different canonical event: "
                    f"{conflict_event_id}"
                )
            return committed
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(f"Queued delivery finalization failed: {exc}") from exc
