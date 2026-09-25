"""Atomic cross-table delivery finalization for SQLiteStorage."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import (
    DeferredHandoffFinalization,
    StorageError,
    TerminalOutboxFinalization,
)
from medre.core.storage.sqlite._native_ref import (
    _native_ref_identity,
    _native_ref_insert_params,
)
from medre.core.storage.sqlite._receipt import _receipt_insert_params
from medre.core.storage.sqlite.connection import (
    sync_finalize_deferred_handoff,
    sync_finalize_outbox_terminal,
)


class _DeliveryFinalizationMixin:
    """Cross-table finalization methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> Any: ...

        async def _run_in_thread(self, func: Any, *args: Any, **kwargs: Any) -> Any: ...

    async def finalize_deferred_handoff(
        self,
        command: DeferredHandoffFinalization,
    ) -> bool:
        """Atomically persist queue-send evidence and mark its outbox sent.

        The transaction commits the outbound native reference, immutable sent
        receipt, and full-identity guarded outbox transition together. It
        returns ``False`` if the outbox identity, attempt number, or eligible
        status no longer matches. A native identity already mapped to another
        canonical event raises ``StorageError``; SQLite errors are also raised
        as ``StorageError`` after rollback. Invalid receipt fields raise
        ``ValueError`` before the transaction starts.
        """
        native_ref = command.native_ref
        receipt = command.receipt
        identity = command.identity
        receipt_params = _receipt_insert_params(receipt)
        native_identity = (
            _native_ref_identity(native_ref) if native_ref is not None else None
        )
        native_params = (
            _native_ref_insert_params(native_ref) if native_ref is not None else None
        )
        transition_time = receipt.created_at.isoformat()
        outbox_params: tuple[object, ...] = (
            transition_time,
            transition_time,
            receipt.receipt_id,
            command.outbox_id,
            identity.event_id,
            identity.delivery_plan_id,
            identity.target_adapter,
            identity.target_channel,
            command.attempt_number,
        )

        db = self._require_db()
        try:
            committed, conflict_event_id = await self._run_in_thread(
                sync_finalize_deferred_handoff,
                db,
                self._lock,
                native_identity=native_identity,
                native_event_id=(
                    native_ref.event_id if native_ref is not None else None
                ),
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
            raise StorageError(f"Deferred hand-off finalization failed: {exc}") from exc

    async def finalize_outbox_terminal(
        self,
        command: TerminalOutboxFinalization,
    ) -> bool:
        """Atomically persist one terminal delivery outcome.

        The transaction re-checks the exact outbox attempt at write time —
        full delivery identity (outbox/event/plan/adapter/channel),
        ``attempt_number``, and eligibility (status still ``queued`` or
        ``in_progress``) — then inserts the immutable lifecycle receipt and
        transitions the row to the command's terminal status together. It
        returns ``False`` when the guarded attempt no longer qualifies (stale
        callback, duplicate notification, or a competing attempt/state
        change won); in that case no receipt commits. A supplied failed-attempt
        receipt commits in the same transaction. SQLite errors roll back and
        reach callers as ``StorageError``. Invalid receipt fields raise
        ``ValueError`` before the transaction starts.

        The outbox row is linked back to its evidence receipt via
        ``receipt_id``; stale retry metadata (``failure_kind``,
        ``failure_kind_detail``, ``next_attempt_at``, lease columns) is
        cleared in the same transition.
        """
        receipt = command.lifecycle_receipt
        attempt_receipt = command.attempt_receipt
        identity = command.identity
        receipt_params = _receipt_insert_params(receipt)
        attempt_receipt_params = (
            _receipt_insert_params(attempt_receipt)
            if attempt_receipt is not None
            else None
        )
        transition_time = receipt.created_at.isoformat()
        outbox_params: tuple[object, ...] = (
            command.terminal_status,
            command.failure_kind,
            transition_time,
            receipt.receipt_id,
            command.error_summary,
            command.outbox_id,
            identity.event_id,
            identity.delivery_plan_id,
            identity.target_adapter,
            identity.target_channel,
            command.attempt_number,
            command.expected_worker_id,
            command.expected_worker_id,
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
