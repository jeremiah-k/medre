"""Bounded delivery retry worker for transient adapter failures.

The RetryWorker polls for due outbox items and re-attempts delivery
through the pipeline.  It is *not* a scheduling framework:
- single-process, in-process
- polling interval is configurable
- batch size is bounded
- stops cleanly on shutdown
- emits runtime events
- visible in snapshot

Outbox integration
------------------
In Tranche 5 the RetryWorker consumes **outbox items** (``delivery_outbox``)
as its primary work queue.  Receipts remain the evidence/audit log; the
outbox is operational work state.

For each due outbox item claimed, the RetryWorker:
1. Loads the canonical event from storage.
2. Finds the most recent receipt for this delivery plan / target.
3. Reconstructs minimal Route + DeliveryPlan from outbox/receipt metadata.
4. Calls ``PipelineRunner.deliver_to_target(... previous_receipt=...)``.
5. Marks terminal on success or updates the existing item to retry_wait
   for the next attempt (or marks dead-lettered on exhaustion).

Legacy receipt-based retry (pre-outbox databases) is supported as a
fallback via ``_process_due_receipts()``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.storage.sqlite import SQLiteStorage
    from medre.core.supervision.capacity import CapacityController
    from medre.runtime.events import EventBuffer

from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.events import RuntimeEventType

__all__ = ["RetryWorker", "RetryWorkerState"]

_logger = logging.getLogger(__name__)


@dataclass
class RetryWorkerState:
    """Snapshot-visible state for the retry worker."""

    enabled: bool = False
    running: bool = False
    last_run_at: str | None = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0


class RetryWorker:
    """In-process retry worker for transient adapter failures.

    Polls storage for due outbox items and re-attempts delivery
    through the pipeline.  Runs as a background asyncio task.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        pipeline: PipelineRunner,
        capacity_controller: CapacityController | None,
        *,
        enabled: bool = True,
        interval_seconds: float = 10.0,
        batch_size: int = 20,
        max_attempts: int = 3,
        event_buffer: EventBuffer | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._capacity = capacity_controller
        self._enabled = enabled
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._event_buffer = event_buffer
        self._shutdown_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._outbox_counts: dict[str, int] = {}
        self.state = RetryWorkerState(enabled=enabled)

    @property
    def outbox_counts(self) -> dict[str, int]:
        """Return a copy of the last-known outbox status counts."""
        return dict(self._outbox_counts)

    def _emit(self, event_type: str, detail: dict[str, Any]) -> None:
        """Emit a runtime event if an event buffer is configured."""
        if self._event_buffer is None:
            return

        try:
            rt = RuntimeEventType(event_type)
        except ValueError:
            return
        self._event_buffer.emit(rt, detail)

    async def start(self) -> None:
        """Start the retry worker background task."""
        if not self._enabled:
            return
        if self._task is not None:
            return
        self._shutdown_event.clear()
        self.state.running = True
        self._task = asyncio.create_task(self._run_loop())
        _logger.info(
            "RetryWorker started (interval=%ss, batch=%d, max_attempts=%d)",
            self._interval,
            self._batch_size,
            self._max_attempts,
        )
        self._emit(
            "retry_started",
            {
                "interval": self._interval,
                "batch_size": self._batch_size,
                "max_attempts": self._max_attempts,
            },
        )

    async def stop(self) -> None:
        """Signal shutdown and wait for worker to finish."""
        if self._task is None:
            return
        self._shutdown_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            _logger.warning("RetryWorker did not stop within 5s")
        self._task = None
        self.state.running = False
        self._emit(
            "retry_stopped",
            {
                "processed": self.state.processed,
                "succeeded": self.state.succeeded,
                "failed": self.state.failed,
                "dead_lettered": self.state.dead_lettered,
            },
        )
        _logger.info("RetryWorker stopped")

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while not self._shutdown_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                await self._process_due(now)
            except Exception:
                _logger.exception("RetryWorker cycle error")
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._interval,
                )
                break  # shutdown was signaled
            except asyncio.TimeoutError:
                pass  # normal interval elapsed

    async def _process_due(self, now: datetime) -> None:
        """Find and process due outbox items.

        Primary path: claim due outbox items from the ``delivery_outbox``
        table.  Fallback path: process legacy retry receipts for databases
        that predate the outbox migration.
        """
        now_iso = now.isoformat()
        worker_id = f"retry-worker-{uuid.uuid4().hex[:8]}"

        # Primary: claim due outbox items.
        items = await self._storage.claim_due_outbox_items(
            now=now_iso,
            worker_id=worker_id,
            lease_seconds=int(self._interval * 1.5) or 30,
            limit=self._batch_size,
        )

        if items:
            self.state.last_run_at = now_iso
            for item in items:
                if self._shutdown_event.is_set():
                    break
                try:
                    await self._retry_outbox_item(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "RetryWorker: unexpected error for outbox %s",
                        item.outbox_id,
                    )
            # Refresh outbox counts after processing a batch.
            try:
                self._outbox_counts = await self._storage.count_outbox_by_status()
            except Exception:
                _logger.debug("RetryWorker: failed to refresh outbox counts")
            return

        # Fallback: legacy receipt-based retry for pre-outbox databases.
        await self._process_due_receipts(now)

    async def _process_due_receipts(self, now: datetime) -> None:
        """Legacy receipt-based retry (pre-outbox databases)."""
        receipts = await self._storage.list_due_retry_receipts(
            now,
            self._batch_size,
            max_attempts=self._max_attempts,
        )
        if not receipts:
            return
        self.state.last_run_at = now.isoformat()
        for receipt in receipts:
            if self._shutdown_event.is_set():
                break
            try:
                await self._retry_one_legacy(receipt)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "RetryWorker: unexpected error for receipt %s",
                    receipt.receipt_id,
                )

    async def _retry_outbox_item(self, item: Any) -> None:
        """Retry delivery for a single due outbox item.

        Uses the outbox item's metadata to reconstruct the delivery
        context, finds the latest receipt for lineage, and re-attempts
        delivery through the pipeline.
        """
        event = await self._storage.get(item.event_id)
        if event is None:
            _logger.warning(
                "RetryWorker: event %s not found for outbox %s",
                item.event_id,
                item.outbox_id,
            )
            await self._storage.mark_outbox_abandoned(
                item.outbox_id,
                error_summary="Event not found in storage",
            )
            return

        # Find the most recent receipt for lineage.
        receipts = await self._storage.list_receipts_for_plan(
            item.delivery_plan_id,
            item.target_adapter,
        )
        previous_receipt = receipts[-1] if receipts else None

        capacity_acquired = False

        # Acquire delivery capacity.
        if self._capacity is not None:
            try:
                acquired = await self._capacity.acquire_delivery()
                if not acquired:
                    self.state.failed += 1
                    _logger.warning(
                        "RetryWorker: capacity rejected for outbox %s",
                        item.outbox_id,
                    )
                    # Release the claim so the item is visible next cycle.
                    _restore_status = "retry_wait" if item.next_attempt_at else "pending"
                    await self._storage.release_outbox_claim(
                        item.outbox_id,
                        item.worker_id or "",
                        release_status=_restore_status,
                    )
                    self._emit(
                        "retry_failed",
                        {
                            "receipt_id": item.receipt_id or item.outbox_id,
                            "parent_receipt_id": item.parent_receipt_id,
                            "retry_receipt_id": None,
                            "event_id": item.event_id,
                            "target_adapter": item.target_adapter,
                            "attempt_number": item.attempt_number,
                            "status": "capacity_rejection",
                            "failure_kind": "capacity_rejection",
                            "error": "delivery capacity not available",
                            "next_retry_at": None,
                        },
                    )
                    return
            except Exception:
                self.state.failed += 1
                _logger.warning(
                    "RetryWorker: capacity error for outbox %s",
                    item.outbox_id,
                )
                try:
                    _restore_status = "retry_wait" if item.next_attempt_at else "pending"
                    await self._storage.release_outbox_claim(
                        item.outbox_id,
                        item.worker_id or "",
                        release_status=_restore_status,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: failed to release claim for outbox %s",
                        item.outbox_id,
                    )
                return
            capacity_acquired = True

        try:
            # Reconstruct Route and DeliveryPlan from outbox metadata.
            target = RouteTarget(
                adapter=item.target_adapter,
                channel=item.target_channel,
            )
            route = Route(
                id=item.route_id or "",
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[target],
            )

            # Use retry policy from the previous receipt, or defaults.
            _max_attempts = self._max_attempts
            _backoff_base = 2.0
            _max_delay = 60.0
            _jitter = False
            if previous_receipt is not None:
                _max_attempts = (
                    previous_receipt.retry_max_attempts or self._max_attempts
                )
                _backoff_base = previous_receipt.retry_backoff_base or 2.0
                _max_delay = previous_receipt.retry_max_delay or 60.0
                _jitter = previous_receipt.retry_jitter or False

            plan = DeliveryPlan(
                plan_id=item.delivery_plan_id or "",
                event_id=item.event_id,
                target=target,
                primary_strategy=DeliveryStrategy(method="direct"),
                retry_policy=RetryPolicy(
                    max_attempts=_max_attempts,
                    backoff_base=_backoff_base,
                    max_delay_seconds=_max_delay,
                    jitter=_jitter,
                ),
            )

            self._emit(
                "retry_attempted",
                {
                    "receipt_id": item.receipt_id or item.outbox_id,
                    "parent_receipt_id": item.parent_receipt_id,
                    "retry_receipt_id": None,
                    "event_id": item.event_id,
                    "target_adapter": item.target_adapter,
                    "attempt_number": item.attempt_number,
                },
            )

            result_receipt = await self._pipeline.deliver_to_target(
                event=event,
                route=route,
                plan=plan,
                previous_receipt=previous_receipt,
                source="retry",
                replay_run_id=None,
            )

            # Success — deliver_to_target returned a receipt.
            self.state.processed += 1
            self.state.succeeded += 1
            if result_receipt.status == "queued":
                await self._storage.mark_outbox_queued(
                    item.outbox_id,
                    receipt_id=result_receipt.receipt_id,
                )
            else:
                await self._storage.mark_outbox_sent(
                    item.outbox_id,
                    receipt_id=result_receipt.receipt_id,
                    attempt_number=result_receipt.attempt_number,
                )
            self._emit(
                "retry_succeeded",
                {
                    "receipt_id": result_receipt.receipt_id,
                    "parent_receipt_id": item.receipt_id or item.outbox_id,
                    "retry_receipt_id": result_receipt.receipt_id,
                    "event_id": item.event_id,
                    "target_adapter": item.target_adapter,
                    "attempt_number": result_receipt.attempt_number,
                },
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.processed += 1
            self.state.failed += 1
            # Check if retry exhausted: pipeline may have appended
            # a dead-lettered receipt.
            is_dead_lettered = False
            if previous_receipt is not None:
                try:
                    is_dead_lettered = await self._check_dead_lettered(
                        item.event_id,
                        item.target_adapter,
                        previous_receipt.receipt_id,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: error checking dead-lettered for %s/%s",
                        item.event_id,
                        item.target_adapter,
                    )

            if is_dead_lettered:
                self.state.dead_lettered += 1
                # Find the dead-lettered receipt to link the outbox item.
                _dl_receipt_id = None
                try:
                    _dl_receipts = await self._storage.list_receipts_for_plan(
                        item.delivery_plan_id,
                        item.target_adapter,
                    )
                    if _dl_receipts:
                        _dl_receipt_id = _dl_receipts[-1].receipt_id
                except Exception:
                    pass
                await self._storage.mark_outbox_dead_lettered(
                    item.outbox_id,
                    receipt_id=_dl_receipt_id,
                    failure_kind="retry_exhausted",
                )
                self._emit(
                    "retry_dead_lettered",
                    {
                        "receipt_id": item.receipt_id or item.outbox_id,
                        "parent_receipt_id": item.parent_receipt_id,
                        "retry_receipt_id": item.receipt_id,
                        "event_id": item.event_id,
                        "target_adapter": item.target_adapter,
                        "attempt_number": item.attempt_number + 1,
                    },
                )
            else:
                # Compute backoff for next retry attempt.
                try:
                    # Try to get the actual failure kind from the latest
                    # receipt (which the pipeline already persisted).
                    _actual_kind = "delivery_failure"
                    try:
                        _latest_receipts = await self._storage.list_receipts_for_plan(
                            item.delivery_plan_id,
                            item.target_adapter,
                        )
                        if _latest_receipts:
                            _latest = _latest_receipts[-1]
                            if _latest.failure_kind:
                                _actual_kind = _latest.failure_kind
                    except Exception:
                        pass  # fall through to default

                    policy = RetryPolicy(
                        max_attempts=_max_attempts,
                        backoff_base=_backoff_base,
                        max_delay_seconds=_max_delay,
                        jitter=_jitter,
                    )
                    next_attempt = item.attempt_number + 1
                    backoff = RetryExecutor(policy).compute_backoff(
                        next_attempt,
                    )
                    next_at = datetime.now(timezone.utc) + backoff
                    await self._storage.mark_outbox_retry_wait(
                        item.outbox_id,
                        next_attempt_at=next_at.isoformat(),
                        failure_kind=_actual_kind,
                        attempt_number=next_attempt,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: failed to backoff outbox %s",
                        item.outbox_id,
                    )
                self._emit(
                    "retry_failed",
                    {
                        "receipt_id": item.receipt_id or item.outbox_id,
                        "parent_receipt_id": item.parent_receipt_id,
                        "retry_receipt_id": None,
                        "event_id": item.event_id,
                        "target_adapter": item.target_adapter,
                        "attempt_number": item.attempt_number,
                    },
                )
            _logger.debug(
                "RetryWorker: delivery failed for outbox %s",
                item.outbox_id,
                exc_info=True,
            )
        finally:
            if capacity_acquired and self._capacity is not None:
                await self._capacity.release_delivery()

    async def _retry_one_legacy(self, receipt: Any) -> None:
        """Legacy retry for a single receipt (pre-outbox databases)."""
        event = await self._storage.get(receipt.event_id)
        if event is None:
            _logger.warning(
                "RetryWorker: event %s not found for receipt %s",
                receipt.event_id,
                receipt.receipt_id,
            )
            return

        parent_receipt_id = receipt.receipt_id
        capacity_acquired = False

        # Acquire delivery capacity.
        if self._capacity is not None:
            try:
                acquired = await self._capacity.acquire_delivery()
                if not acquired:
                    self.state.failed += 1
                    _logger.warning(
                        "RetryWorker: capacity rejected for receipt %s",
                        receipt.receipt_id,
                    )
                    try:
                        policy = RetryPolicy(
                            max_attempts=(
                                receipt.retry_max_attempts
                                if receipt.retry_max_attempts is not None
                                else self._max_attempts
                            ),
                            backoff_base=(
                                receipt.retry_backoff_base
                                if receipt.retry_backoff_base is not None
                                else 2.0
                            ),
                            max_delay_seconds=(
                                receipt.retry_max_delay
                                if receipt.retry_max_delay is not None
                                else 60.0
                            ),
                            jitter=(
                                receipt.retry_jitter
                                if receipt.retry_jitter is not None
                                else False
                            ),
                        )
                        backoff = RetryExecutor(policy).compute_backoff(
                            receipt.attempt_number,
                        )
                        await self._storage.update_retry_due(
                            receipt.receipt_id,
                            datetime.now(timezone.utc) + backoff,
                        )
                    except Exception:
                        _logger.exception(
                            "RetryWorker: failed to backoff receipt %s",
                            receipt.receipt_id,
                        )
                    self._emit(
                        "retry_failed",
                        {
                            "receipt_id": parent_receipt_id,
                            "parent_receipt_id": parent_receipt_id,
                            "retry_receipt_id": None,
                            "event_id": receipt.event_id,
                            "target_adapter": receipt.target_adapter,
                            "attempt_number": receipt.attempt_number,
                            "status": "capacity_rejection",
                            "failure_kind": receipt.failure_kind or "delivery_failure",
                            "error": "delivery capacity not available",
                            "next_retry_at": None,
                        },
                    )
                    return
            except Exception:
                self.state.failed += 1
                _logger.warning(
                    "RetryWorker: capacity error for receipt %s",
                    receipt.receipt_id,
                )
                return
            capacity_acquired = True

        try:
            target = RouteTarget(
                adapter=receipt.target_adapter,
                channel=receipt.target_channel,
            )
            route = Route(
                id=receipt.route_id or "",
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[target],
            )
            plan = DeliveryPlan(
                plan_id=receipt.delivery_plan_id or "",
                event_id=receipt.event_id,
                target=target,
                primary_strategy=DeliveryStrategy(method="direct"),
                retry_policy=RetryPolicy(
                    max_attempts=(
                        receipt.retry_max_attempts
                        if receipt.retry_max_attempts is not None
                        else self._max_attempts
                    ),
                    backoff_base=(
                        receipt.retry_backoff_base
                        if receipt.retry_backoff_base is not None
                        else 2.0
                    ),
                    max_delay_seconds=(
                        receipt.retry_max_delay
                        if receipt.retry_max_delay is not None
                        else 60.0
                    ),
                    jitter=(
                        receipt.retry_jitter
                        if receipt.retry_jitter is not None
                        else False
                    ),
                ),
            )

            self._emit(
                "retry_attempted",
                {
                    "receipt_id": parent_receipt_id,
                    "parent_receipt_id": parent_receipt_id,
                    "retry_receipt_id": None,
                    "event_id": receipt.event_id,
                    "target_adapter": receipt.target_adapter,
                    "attempt_number": receipt.attempt_number,
                },
            )

            result_receipt = await self._pipeline.deliver_to_target(
                event=event,
                route=route,
                plan=plan,
                previous_receipt=receipt,
                source="retry",
                replay_run_id=None,
            )

            self.state.processed += 1
            self.state.succeeded += 1
            self._emit(
                "retry_succeeded",
                {
                    "receipt_id": result_receipt.receipt_id,
                    "parent_receipt_id": parent_receipt_id,
                    "retry_receipt_id": result_receipt.receipt_id,
                    "event_id": receipt.event_id,
                    "target_adapter": receipt.target_adapter,
                    "attempt_number": result_receipt.attempt_number,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.processed += 1
            self.state.failed += 1
            is_dead_lettered = False
            try:
                is_dead_lettered = await self._check_dead_lettered(
                    receipt.event_id,
                    receipt.target_adapter,
                    parent_receipt_id,
                )
            except Exception:
                _logger.exception(
                    "RetryWorker: error checking dead-lettered for %s/%s",
                    receipt.event_id,
                    receipt.target_adapter,
                )

            if is_dead_lettered:
                self.state.dead_lettered += 1
                try:
                    dl_receipt_id = await self._find_dead_letter_receipt_id(
                        receipt.event_id,
                        receipt.target_adapter,
                        parent_receipt_id,
                    )
                except Exception:
                    _logger.debug(
                        "RetryWorker: failed to find dead-letter receipt for %s/%s",
                        receipt.event_id,
                        receipt.target_adapter,
                        exc_info=True,
                    )
                    dl_receipt_id = None
                self._emit(
                    "retry_dead_lettered",
                    {
                        "receipt_id": dl_receipt_id or parent_receipt_id,
                        "parent_receipt_id": parent_receipt_id,
                        "retry_receipt_id": dl_receipt_id,
                        "event_id": receipt.event_id,
                        "target_adapter": receipt.target_adapter,
                        "attempt_number": receipt.attempt_number + 1,
                    },
                )
            else:
                try:
                    new_receipt = await self._find_failed_receipt(
                        receipt.event_id,
                        receipt.target_adapter,
                        parent_receipt_id,
                    )
                except Exception:
                    _logger.debug(
                        "RetryWorker: failed to find failed receipt for %s/%s",
                        receipt.event_id,
                        receipt.target_adapter,
                        exc_info=True,
                    )
                    new_receipt = None
                self._emit(
                    "retry_failed",
                    {
                        "receipt_id": parent_receipt_id,
                        "parent_receipt_id": parent_receipt_id,
                        "retry_receipt_id": (
                            new_receipt.receipt_id if new_receipt else None
                        ),
                        "event_id": receipt.event_id,
                        "target_adapter": receipt.target_adapter,
                        "attempt_number": receipt.attempt_number,
                    },
                )
            _logger.debug(
                "RetryWorker: delivery failed for receipt %s",
                receipt.receipt_id,
                exc_info=True,
            )
        finally:
            if capacity_acquired and self._capacity is not None:
                await self._capacity.release_delivery()

    async def _check_dead_lettered(
        self,
        event_id: str,
        target_adapter: str,
        parent_receipt_id: str,
    ) -> bool:
        """Check if a dead-lettered receipt exists for this specific retry lineage."""
        receipts = await self._storage.list_receipts_for_event(event_id)
        return any(
            r.status == "dead_lettered"
            and r.target_adapter == target_adapter
            and r.parent_receipt_id == parent_receipt_id
            for r in receipts
        )

    async def _find_dead_letter_receipt_id(
        self,
        event_id: str,
        target_adapter: str,
        parent_receipt_id: str,
    ) -> str | None:
        """Return the receipt_id of the dead-lettered receipt for this lineage."""
        receipts = await self._storage.list_receipts_for_event(event_id)
        for r in receipts:
            if (
                r.status == "dead_lettered"
                and r.target_adapter == target_adapter
                and r.parent_receipt_id == parent_receipt_id
            ):
                return r.receipt_id
        return None

    async def _find_failed_receipt(
        self,
        event_id: str,
        target_adapter: str,
        parent_receipt_id: str,
    ) -> Any | None:
        """Find the latest failed receipt for this retry lineage."""
        receipts = await self._storage.list_receipts_for_event(event_id)
        for r in reversed(receipts):
            if (
                r.status == "failed"
                and r.target_adapter == target_adapter
                and r.parent_receipt_id == parent_receipt_id
            ):
                return r
        return None
