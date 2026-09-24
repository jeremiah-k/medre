"""Delivery receipt mixins for SQLiteStorage.

Authority surface:
  - append_receipt: **append** (append-only).  Delivery receipts are
    historical delivery evidence — once appended they must never be updated
    or deleted by runtime code.  The ``sequence`` column (INTEGER PRIMARY KEY
    AUTOINCREMENT) provides strict chronological ordering.  No update-receipt
    or delete-receipt method exists by design.
  - delivery_status:              **list/get** (read-only).
  - list_receipts_for_delivery:   **list/get** (read-only).
  - list_receipts_by_replay_run:  **list/get** (read-only).
  - list_receipts_for_event:      **list/get** (read-only).
  - list_all_receipts:            **list/get** (read-only).
  - list_due_retry_receipts:      **list/get** (read-only).
  - count_pending_retry:          **list/get** (read-only).
"""

from __future__ import annotations

from datetime import datetime

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline.delivery_state import RECEIPT_STATUSES
from medre.core.events import (
    DELIVERY_CONFIRMATION_LEVEL_VALUES,
    DeliveryReceipt,
    normalize_delivery_provenance,
    normalize_replay_run_id,
)
from medre.core.storage.sqlite.serde import _row_to_receipt
from medre.core.storage.sqlite.statements import (
    _DELIVERY_RECEIPT_LATEST_BY_EVENT_CHANNEL,
    _INSERT_RECEIPT,
    _SELECT_ALL_RECEIPTS,
    _SELECT_RECEIPTS_BY_REPLAY_RUN,
    _SELECT_RECEIPTS_FOR_DELIVERY,
    _SELECT_RECEIPTS_FOR_EVENT,
)


def _receipt_insert_params(receipt: DeliveryReceipt) -> tuple[object, ...]:
    """Validate and serialize one immutable receipt row."""
    if receipt.status not in RECEIPT_STATUSES:
        raise ValueError(
            f"Unknown receipt status {receipt.status!r}; "
            f"expected one of {sorted(RECEIPT_STATUSES)}"
        )
    if receipt.receipt_kind not in {"attempt", "lifecycle"}:
        raise ValueError(
            f"Unknown receipt kind {receipt.receipt_kind!r}; "
            "expected 'attempt' or 'lifecycle'"
        )
    if not isinstance(receipt.confirmation_level, str) or (
        receipt.confirmation_level not in DELIVERY_CONFIRMATION_LEVEL_VALUES
    ):
        raise ValueError(
            f"Unknown confirmation level {receipt.confirmation_level!r}; "
            f"expected one of {sorted(DELIVERY_CONFIRMATION_LEVEL_VALUES)}"
        )

    source, replay_run_id = normalize_delivery_provenance(
        receipt.source, receipt.replay_run_id
    )
    channel = receipt.target_channel or None
    return (
        receipt.receipt_id,
        receipt.event_id,
        receipt.delivery_plan_id,
        receipt.target_adapter,
        channel,
        receipt.route_id,
        receipt.status,
        receipt.receipt_kind,
        receipt.error,
        receipt.failure_kind,
        receipt.adapter_message_id,
        receipt.next_retry_at.isoformat() if receipt.next_retry_at else None,
        receipt.attempt_number,
        receipt.parent_receipt_id,
        source,
        replay_run_id,
        receipt.retry_max_attempts,
        receipt.retry_backoff_base,
        receipt.retry_max_delay,
        (
            1
            if receipt.retry_jitter is True
            else (0 if receipt.retry_jitter is False else None)
        ),
        receipt.rendering_evidence,
        receipt.outbox_id,
        receipt.confirmation_level,
        receipt.created_at.isoformat(),
    )


class _ReceiptMixin:
    """Delivery receipt methods for SQLiteStorage.

    Accesses ``self._read_one``, ``self._read_all``, and ``self._write``
    from the base class via MRO.
    """

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        """Append a delivery receipt record.

        Authority: **append** (append-only).  Receipts are append-only:
        every call creates a new row.  Existing receipt rows are never
        updated or deleted.  Current-status projections distinguish append
        history from lifecycle authority: an outbox-backed receipt is current
        only when the outbox row points at its ``receipt_id``.  A receipt whose
        guarded outbox transition lost a race remains durable historical
        evidence.

        Empty-string ``target_channel`` values are normalised to ``None``
        (SQL NULL) before insertion.  The ``delivery_status`` view uses
        ``COALESCE(target_channel, '')`` so that NULL and empty-string
        channels are treated identically in grouping.

        Raises :class:`ValueError` if ``receipt.status`` is not a known
        receipt status (not in ``RECEIPT_STATUSES``).
        """
        await self._write(_INSERT_RECEIPT, _receipt_insert_params(receipt))

    async def delivery_status(
        self,
        identity: DeliveryIdentity,
    ) -> DeliveryReceipt | None:
        """Return current authority for one complete delivery identity.

        Authority: **list/get** (read-only). The query uses the same typed
        event/plan/adapter/channel identity as historical reads. Outbox-backed
        receipts are eligible only when the matching outbox generation commits
        their receipt ID; outbox-less history retains append-order authority.
        Return ``None`` when no receipt qualifies. Raise ``ValueError`` for an
        incomplete identity.
        """
        if not identity.complete:
            raise ValueError("delivery status requires a complete DeliveryIdentity")
        sql = _DELIVERY_RECEIPT_LATEST_BY_EVENT_CHANNEL
        params = (
            identity.event_id,
            identity.delivery_plan_id,
            identity.target_adapter,
            identity.target_channel or None,
            identity.event_id,
            identity.delivery_plan_id,
            identity.target_adapter,
            identity.target_channel or None,
        )
        row = await self._read_one(sql, params)
        return _row_to_receipt(row) if row else None

    async def list_receipts_for_delivery(
        self,
        identity: DeliveryIdentity,
    ) -> list[DeliveryReceipt]:
        """Return immutable history for one complete delivery identity.

        The channel component is part of lifecycle identity. Empty and absent
        channels normalize to SQL ``NULL`` at persistence boundaries. Rows are
        ordered by attempt number and append sequence; an unmatched identity
        returns an empty list. Raise ``ValueError`` for an incomplete identity.
        """
        if not identity.complete:
            raise ValueError("receipt history requires a complete DeliveryIdentity")
        rows = await self._read_all(
            _SELECT_RECEIPTS_FOR_DELIVERY,
            (
                identity.event_id,
                identity.delivery_plan_id,
                identity.target_adapter,
                identity.target_channel or None,
            ),
        )
        return [_row_to_receipt(row) for row in rows]

    async def list_receipts_by_replay_run(
        self,
        run_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all receipts produced by a specific replay run.

        Authority: **list/get** (read-only).  Receipts are ordered by ``sequence`` ascending.  Only receipts
        with the given ``replay_run_id`` are returned.  Returns an
        empty list when no receipts match.
        """
        normalized_run_id = normalize_replay_run_id(run_id)
        if normalized_run_id is None:
            raise ValueError("replay-run receipt lookup requires a non-empty run ID")
        rows = await self._read_all(
            _SELECT_RECEIPTS_BY_REPLAY_RUN,
            (normalized_run_id,),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_receipts_for_event(
        self,
        event_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts for a specific event.

        Authority: **list/get** (read-only).  Receipts are ordered by ``sequence`` ascending, which reflects
        the chronological append order across all delivery plans and
        adapters for this event.
        """
        rows = await self._read_all(
            _SELECT_RECEIPTS_FOR_EVENT,
            (event_id,),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_all_receipts(
        self,
        limit: int = 10_000,
        offset: int = 0,
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts in sequence order.

        Authority: **list/get** (read-only).  Ordered by ``sequence``
        ascending for deterministic output.
        Useful for global convergence analysis across all events.
        """
        rows = await self._read_all(
            f"{_SELECT_ALL_RECEIPTS.strip()} LIMIT ? OFFSET ?",  # nosec B608 - _SELECT_ALL_RECEIPTS is a module-level constant, values parameterized
            (limit, offset),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_due_retry_receipts(
        self, now: datetime, limit: int = 50, max_attempts: int = 3
    ) -> list[DeliveryReceipt]:
        """Return transient-failure receipts whose next_retry_at <= now,
        ordered by next_retry_at ASC, sequence ASC, limited to *limit*.
        Excludes receipts that have reached *max_attempts* or are dead_lettered.

        Authority: **list/get** (read-only).
        """
        rows = await self._read_all(
            """SELECT * FROM delivery_receipts r
             WHERE r.status = 'failed'
               AND r.failure_kind = 'adapter_transient'
               AND r.next_retry_at IS NOT NULL
               AND r.next_retry_at <= ?
               AND r.attempt_number < ?
               AND (
                   r.outbox_id IS NULL
                   OR EXISTS (
                       SELECT 1 FROM delivery_outbox o
                       WHERE o.outbox_id = r.outbox_id
                         AND o.receipt_id = r.receipt_id
                   )
               )
               AND NOT EXISTS (
                   SELECT 1 FROM delivery_receipts child
                   WHERE child.parent_receipt_id = r.receipt_id
                     AND child.source = 'retry'
               )
             ORDER BY r.next_retry_at ASC, r.sequence ASC
             LIMIT ?""",
            (now.isoformat(), max_attempts, limit),
        )
        return [_row_to_receipt(r) for r in rows]

    async def count_pending_retry(self, now: datetime, max_attempts: int = 3) -> int:
        """Count transient-failure receipts due for retry.

        Authority: **list/get** (read-only).
        """
        row = await self._read_one(
            """SELECT COUNT(*) AS cnt FROM delivery_receipts r
             WHERE r.status = 'failed'
               AND r.failure_kind = 'adapter_transient'
               AND r.next_retry_at IS NOT NULL
               AND r.next_retry_at <= ?
               AND r.attempt_number < ?
               AND (
                   r.outbox_id IS NULL
                   OR EXISTS (
                       SELECT 1 FROM delivery_outbox o
                       WHERE o.outbox_id = r.outbox_id
                         AND o.receipt_id = r.receipt_id
                   )
               )
               AND NOT EXISTS (
                   SELECT 1 FROM delivery_receipts child
                   WHERE child.parent_receipt_id = r.receipt_id
                     AND child.source = 'retry'
               )""",
            (now.isoformat(), max_attempts),
        )
        return row["cnt"] if row else 0
