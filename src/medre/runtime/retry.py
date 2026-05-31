"""Bounded delivery retry worker for transient adapter failures.

The RetryWorker polls for due outbox items and re-attempts delivery
through the pipeline.  It is *not* a scheduling framework:
- single-process, in-process
- polling interval is configurable
- batch size is bounded
- stops cleanly on shutdown
- emits runtime events
- visible in snapshot

The RetryWorker consumes **outbox items** (``delivery_outbox``)
exclusively.  Receipts are the evidence/audit log; the outbox is
operational work state.

For each due outbox item claimed, the RetryWorker:
1. Loads the canonical event from storage.
2. Finds the most recent receipt for this delivery plan / target.
3. Reconstructs minimal Route + DeliveryPlan from outbox/receipt metadata.
4. Calls ``PipelineRunner.deliver_to_target(... previous_receipt=...)``.
5. Marks terminal on success or updates the existing item to retry_wait
   for the next attempt (or marks dead-lettered on exhaustion).
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
    from medre.core.storage.sqlite.storage import SQLiteStorage
    from medre.core.supervision.capacity import CapacityController
    from medre.runtime.events import EventBuffer

from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
    delivery_target_identity,
)
from medre.core.routing.models import Route, RouteDestination, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
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
        self._cycle_completed: bool = False
        self.state = RetryWorkerState(enabled=enabled)

    @property
    def outbox_counts(self) -> dict[str, int] | None:
        """Return a copy of the last-known outbox status counts.

        Returns ``None`` if the worker has not yet completed its first
        cycle, so callers can distinguish "no data yet" from "zero items".
        """
        if not self._cycle_completed:
            return None
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
        """Signal shutdown and wait for worker to finish.

        If the background task does not complete within the grace
        period it is cancelled and awaited so that no orphan task
        remains after this method returns.
        """
        if self._task is None:
            return
        self._shutdown_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            _logger.warning("RetryWorker did not stop within 5s, cancelling")
            task = self._task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
        """Find and process due outbox items."""
        now_iso = now.isoformat()
        worker_id = f"retry-worker-{uuid.uuid4().hex[:8]}"

        items = await self._storage.claim_due_outbox_items(
            now=now_iso,
            worker_id=worker_id,
            lease_seconds=int(self._interval * 1.5) or 30,
            limit=self._batch_size,
        )

        if not items:
            # Refresh counts on idle cycles too.
            try:
                self._outbox_counts = await self._storage.count_outbox_by_status()
                self._cycle_completed = True
            except Exception:
                _logger.debug("RetryWorker: failed to refresh outbox counts")
            return

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
            self._cycle_completed = True
        except Exception:
            _logger.debug("RetryWorker: failed to refresh outbox counts")

    async def _retry_outbox_item(self, item: DeliveryOutboxItem) -> None:
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
        # Filter to the same target channel for correct lineage.
        receipts = [r for r in receipts if r.target_channel == item.target_channel]
        previous_receipt = receipts[-1] if receipts else None

        # Initialise retry-policy defaults before the capacity check so
        # the backoff is available for capacity-rejection scheduling.
        _max_attempts = self._max_attempts
        _backoff_base = 2.0
        _max_delay = 60.0
        _jitter = False
        if previous_receipt is not None:
            _max_attempts = previous_receipt.retry_max_attempts or self._max_attempts
            _backoff_base = previous_receipt.retry_backoff_base or 2.0
            _max_delay = previous_receipt.retry_max_delay or 60.0
            _jitter = previous_receipt.retry_jitter or False

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
                    # Compute backoff so the item isn't immediately retried.
                    _cap_policy = RetryPolicy(
                        max_attempts=_max_attempts,
                        backoff_base=_backoff_base,
                        max_delay_seconds=_max_delay,
                        jitter=_jitter,
                    )
                    _cap_backoff = RetryExecutor(_cap_policy).compute_backoff(
                        item.attempt_number,
                    )
                    _cap_next = datetime.now(timezone.utc) + _cap_backoff
                    try:
                        await self._storage.mark_outbox_retry_wait(
                            item.outbox_id,
                            next_attempt_at=_cap_next.isoformat(),
                            failure_kind="capacity_rejection",
                            attempt_number=item.attempt_number,
                        )
                    except Exception:
                        _logger.exception(
                            "RetryWorker: failed to backoff outbox %s on capacity rejection",
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
                            "status": "capacity_rejection",
                            "failure_kind": "capacity_rejection",
                            "error": "delivery capacity not available",
                            "next_retry_at": _cap_next.isoformat(),
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
                    # claim_due_outbox_items clears next_attempt_at, so
                    # release_outbox_claim would always restore to "pending"
                    # and cause immediate re-claim.  Use mark_outbox_retry_wait
                    # with a proper backoff instead.
                    _err_policy = RetryPolicy(
                        max_attempts=_max_attempts,
                        backoff_base=_backoff_base,
                        max_delay_seconds=_max_delay,
                        jitter=_jitter,
                    )
                    _err_backoff = RetryExecutor(_err_policy).compute_backoff(
                        item.attempt_number,
                    )
                    _err_next = datetime.now(timezone.utc) + _err_backoff
                    await self._storage.mark_outbox_retry_wait(
                        item.outbox_id,
                        next_attempt_at=_err_next.isoformat(),
                        failure_kind="capacity_error",
                        attempt_number=item.attempt_number,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: failed to backoff outbox %s on capacity error",
                        item.outbox_id,
                    )
                return
            capacity_acquired = True

        try:
            # Reconstruct Route and DeliveryPlan from outbox metadata.
            _dest: RouteDestination | None = None
            if item.metadata and "destination_kind" in item.metadata:
                _dest = RouteDestination(
                    kind=item.metadata["destination_kind"],
                    destination_hash=item.metadata.get("destination_hash"),
                    destination_name=item.metadata.get("destination_name"),
                    metadata=item.metadata.get("destination_metadata", {}),
                )
            target = RouteTarget(
                adapter=item.target_adapter,
                channel=item.target_channel,
                destination=_dest,
            )
            route = Route(
                id=item.route_id or "",
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[target],
            )

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
                route_id=item.route_id or None,
                target_identity=delivery_target_identity(target),
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
                        target_channel=item.target_channel,
                    )
                    # Fallback: if parent-specific check missed, check for
                    # any dead-lettered receipt for this event+adapter.
                    # The pipeline may create a dead-lettered receipt whose
                    # parent is the CURRENT attempt's receipt, not the
                    # previous one.
                    if not is_dead_lettered:
                        is_dead_lettered = await self._check_dead_lettered(
                            item.event_id,
                            item.target_adapter,
                            target_channel=item.target_channel,
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
                    _dl_receipts = [
                        r
                        for r in _dl_receipts
                        if r.target_channel == item.target_channel
                        and r.status == "dead_lettered"
                    ]
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
                _exhausted = False
                try:
                    # Try to get the actual failure kind from the latest
                    # receipt (which the pipeline already persisted).
                    _actual_kind = "delivery_failure"
                    try:
                        _latest_receipts = await self._storage.list_receipts_for_plan(
                            item.delivery_plan_id,
                            item.target_adapter,
                        )
                        _latest_receipts = [
                            r
                            for r in _latest_receipts
                            if r.target_channel == item.target_channel
                        ]
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
                    # Guard: if attempt count exceeds policy, mark
                    # dead-lettered instead of scheduling another retry.
                    # This prevents infinite retries when the pipeline
                    # creates a dead-lettered receipt but the parent chain
                    # doesn't align with _check_dead_lettered.
                    if next_attempt > _max_attempts:
                        _logger.warning(
                            "RetryWorker: retries exhausted for outbox %s "
                            "(attempt %d > max %d)",
                            item.outbox_id,
                            next_attempt,
                            _max_attempts,
                        )
                        await self._storage.mark_outbox_dead_lettered(
                            item.outbox_id,
                            failure_kind=_actual_kind,
                        )
                        self.state.dead_lettered += 1
                        self._emit(
                            "retry_dead_lettered",
                            {
                                "receipt_id": item.receipt_id or item.outbox_id,
                                "parent_receipt_id": item.parent_receipt_id,
                                "retry_receipt_id": item.receipt_id,
                                "event_id": item.event_id,
                                "target_adapter": item.target_adapter,
                                "attempt_number": next_attempt,
                            },
                        )
                        _exhausted = True
                    else:
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
                if not _exhausted:
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
        else:
            # Success — delivery returned a receipt.
            self.state.processed += 1
            self.state.succeeded += 1
            try:
                if result_receipt.status == "queued":
                    await self._storage.mark_outbox_queued(
                        item.outbox_id,
                        receipt_id=result_receipt.receipt_id,
                        attempt_number=result_receipt.attempt_number,
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
            except Exception:
                _logger.exception(
                    "RetryWorker: failed to update outbox %s after successful delivery",
                    item.outbox_id,
                )
        finally:
            if capacity_acquired and self._capacity is not None:
                await self._capacity.release_delivery()

    async def _check_dead_lettered(
        self,
        event_id: str,
        target_adapter: str,
        parent_receipt_id: str | None = None,
        target_channel: str | None = None,
    ) -> bool:
        """Check if a dead-lettered receipt exists for this event+adapter.

        When *parent_receipt_id* is provided, matches receipts whose parent
        is that receipt.  When omitted, matches ANY dead-lettered receipt
        for the event+adapter pair (used by the exhaustion guard).

        When *target_channel* is provided, only matches receipts targeting
        that channel, preventing cross-channel false positives.
        """
        receipts = await self._storage.list_receipts_for_event(event_id)
        return any(
            r.status == "dead_lettered"
            and r.target_adapter == target_adapter
            and (parent_receipt_id is None or r.parent_receipt_id == parent_receipt_id)
            and (target_channel is None or r.target_channel == target_channel)
            for r in receipts
        )
