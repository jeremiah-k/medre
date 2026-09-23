"""Atomic cross-table delivery finalization for SQLiteStorage."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from medre.core.engine.pipeline.delivery_state import TERMINAL_OUTBOX_STATUSES
from medre.core.events import DeliveryReceipt, NativeMessageRef
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._native_ref import (
    _native_ref_identity,
    _native_ref_insert_params,
)
from medre.core.storage.sqlite._receipt import _receipt_insert_params
from medre.core.storage.sqlite.connection import (
    sync_finalize_outbox_terminal,
    sync_finalize_queued_delivery,
)

# Error terminals only — "sent" is terminal but happy-path and queue
# terminal outcomes never map to it.
_ERROR_TERMINAL_OUTBOX_STATUSES: frozenset[str] = TERMINAL_OUTBOX_STATUSES - {"sent"}


class _DeliveryFinalizationMixin:
    """Cross-table finalization methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:
        _lock: threading.Lock

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

    @staticmethod
    def _validate_outbox_terminal_finalization(
        receipt: DeliveryReceipt,
        *,
        outbox_id: str,
        attempt_number: int,
        terminal_status: str,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
    ) -> None:
        if terminal_status not in _ERROR_TERMINAL_OUTBOX_STATUSES:
            raise ValueError(
                "terminal outbox finalization requires an error-terminal "
                f"status (dead_lettered/cancelled/abandoned), got {terminal_status!r}"
            )
        if receipt.receipt_kind != "lifecycle":
            raise ValueError("terminal outbox finalization requires lifecycle evidence")
        if receipt.status != terminal_status:
            raise ValueError(
                "terminal lifecycle receipt status must match terminal_status"
            )
        if receipt.outbox_id != outbox_id:
            raise ValueError("receipt.outbox_id must match outbox_id")
        if receipt.event_id != event_id:
            raise ValueError("receipt.event_id must match event_id")
        if receipt.delivery_plan_id != delivery_plan_id:
            raise ValueError("receipt.delivery_plan_id must match delivery_plan_id")
        if receipt.target_adapter != target_adapter:
            raise ValueError("receipt.target_adapter must match target_adapter")
        if (receipt.target_channel or None) != (target_channel or None):
            raise ValueError("receipt.target_channel must match target_channel")
        if receipt.attempt_number != attempt_number:
            raise ValueError("receipt.attempt_number must match attempt_number")
        if attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")

    async def finalize_outbox_terminal(
        self,
        receipt: DeliveryReceipt,
        *,
        attempt_receipt: DeliveryReceipt | None = None,
        outbox_id: str,
        attempt_number: int,
        terminal_status: str,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        failure_kind: str | None = None,
        error_summary: str | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Atomically persist one terminal delivery outcome.

        The transaction re-checks the exact outbox attempt at write time —
        full delivery identity (outbox/event/plan/adapter/channel),
        ``attempt_number``, and eligibility (status still ``queued`` or
        ``in_progress``) — then inserts the immutable lifecycle receipt and
        transitions the row to *terminal_status* together.  It returns
        ``False`` when the guarded attempt no longer qualifies (stale
        callback, duplicate notification, or a competing attempt/state
        change won); in that case neither write commits.  Any error rolls
        the whole operation back.

        The outbox row is linked back to its evidence receipt via
        ``receipt_id``; stale retry metadata (``failure_kind``,
        ``failure_kind_detail``, ``next_attempt_at``, lease columns) is
        cleared in the same transition.
        """
        self._validate_outbox_terminal_finalization(
            receipt,
            outbox_id=outbox_id,
            attempt_number=attempt_number,
            terminal_status=terminal_status,
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
        )
        if attempt_receipt is not None:
            if attempt_receipt.receipt_kind != "attempt":
                raise ValueError("attempt_receipt must be attempt evidence")
            if attempt_receipt.status != "failed":
                raise ValueError("terminal attempt_receipt must have status='failed'")
            if (
                attempt_receipt.event_id != receipt.event_id
                or attempt_receipt.delivery_plan_id != receipt.delivery_plan_id
                or attempt_receipt.target_adapter != receipt.target_adapter
                or (attempt_receipt.target_channel or None)
                != (receipt.target_channel or None)
                or attempt_receipt.outbox_id != receipt.outbox_id
                or attempt_receipt.attempt_number != receipt.attempt_number
                or receipt.parent_receipt_id != attempt_receipt.receipt_id
            ):
                raise ValueError(
                    "terminal lifecycle receipt must be linked to attempt_receipt"
                )
        receipt_params = _receipt_insert_params(receipt)
        attempt_receipt_params = (
            _receipt_insert_params(attempt_receipt)
            if attempt_receipt is not None
            else None
        )
        transition_time = receipt.created_at.isoformat()
        outbox_params: tuple[object, ...] = (
            terminal_status,
            failure_kind,
            transition_time,
            receipt.receipt_id,
            error_summary,
            outbox_id,
            event_id,
            delivery_plan_id,
            target_adapter,
            target_channel or None,
            attempt_number,
            expected_worker_id,
            expected_worker_id,
        )

        db = self._require_db()
        try:
            return await self._run_in_thread(
                sync_finalize_outbox_terminal,
                db,
                self._lock,
                receipt_insert_params=receipt_params,
                attempt_receipt_insert_params=attempt_receipt_params,
                outbox_update_params=outbox_params,
            )

        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(f"Terminal outbox finalization failed: {exc}") from exc
