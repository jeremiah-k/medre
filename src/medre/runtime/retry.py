"""Bounded delivery retry worker for transient adapter failures.

The RetryWorker polls for due transient-failure receipts and re-attempts
delivery through the pipeline.  It is *not* a scheduling framework:
- single-process, in-process
- polling interval is configurable
- batch size is bounded
- stops cleanly on shutdown
- emits runtime events
- visible in snapshot
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.storage.sqlite import SQLiteStorage
    from medre.runtime.capacity import CapacityController
    from medre.runtime.events import EventBuffer

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

    Polls storage for due retry receipts and re-attempts delivery
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
        self.state = RetryWorkerState(enabled=enabled)

    def _emit(self, event_type: str, detail: dict[str, Any]) -> None:
        """Emit a runtime event if an event buffer is configured."""
        if self._event_buffer is None:
            return
        from medre.runtime.events import RuntimeEventType
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
        self._emit("retry_started", {
            "interval": self._interval,
            "batch_size": self._batch_size,
            "max_attempts": self._max_attempts,
        })

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
        self._emit("retry_stopped", {
            "processed": self.state.processed,
            "succeeded": self.state.succeeded,
            "failed": self.state.failed,
            "dead_lettered": self.state.dead_lettered,
        })
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
        """Find and process due retry receipts."""
        receipts = await self._storage.list_due_retry_receipts(
            now, self._batch_size, max_attempts=self._max_attempts,
        )
        if not receipts:
            return
        self.state.last_run_at = now.isoformat()
        for receipt in receipts:
            if self._shutdown_event.is_set():
                break
            try:
                await self._retry_one(receipt)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "RetryWorker: unexpected error for receipt %s",
                    receipt.receipt_id,
                )

    async def _retry_one(self, receipt: Any) -> None:
        """Retry delivery for a single failed receipt."""
        from medre.core.planning.delivery_plan import (
            DeliveryPlan,
            DeliveryStrategy,
            RetryPolicy,
        )
        from medre.core.routing.models import Route, RouteSource, RouteTarget

        event = await self._storage.get(receipt.event_id)
        if event is None:
            _logger.warning(
                "RetryWorker: event %s not found for receipt %s",
                receipt.event_id,
                receipt.receipt_id,
            )
            return

        parent_receipt_id = receipt.receipt_id

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
                    # Back off: update next_retry_at to prevent hot-looping.
                    try:
                        from medre.core.planning.delivery_plan import (
                            RetryPolicy,
                            RetryExecutor,
                        )
                        # Reconstruct policy from receipt metadata.
                        policy = RetryPolicy(
                            max_attempts=receipt.retry_max_attempts if receipt.retry_max_attempts is not None else self._max_attempts,
                            backoff_base=receipt.retry_backoff_base if receipt.retry_backoff_base is not None else 2.0,
                            max_delay_seconds=receipt.retry_max_delay if receipt.retry_max_delay is not None else 60.0,
                            jitter=receipt.retry_jitter if receipt.retry_jitter is not None else False,
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
                    self._emit("retry_failed", {
                        "receipt_id": parent_receipt_id,
                        "parent_receipt_id": parent_receipt_id,
                        "retry_receipt_id": None,
                        "event_id": receipt.event_id,
                        "target_adapter": receipt.target_adapter,
                        "attempt_number": receipt.attempt_number,
                        "status": "capacity_rejection",
                        "failure_kind": "capacity_rejection",
                        "error": "delivery capacity not available",
                        "next_retry_at": None,
                    })
                    return
            except Exception:
                self.state.failed += 1
                _logger.warning(
                    "RetryWorker: capacity error for receipt %s",
                    receipt.receipt_id,
                )
                return

        try:
            # Reconstruct minimal Route and DeliveryPlan from receipt metadata.
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
                    max_attempts=receipt.retry_max_attempts if receipt.retry_max_attempts is not None else self._max_attempts,
                    backoff_base=receipt.retry_backoff_base if receipt.retry_backoff_base is not None else 2.0,
                    max_delay_seconds=receipt.retry_max_delay if receipt.retry_max_delay is not None else 60.0,
                    jitter=receipt.retry_jitter if receipt.retry_jitter is not None else False,
                ),
            )

            self._emit("retry_attempted", {
                "receipt_id": parent_receipt_id,
                "parent_receipt_id": parent_receipt_id,
                "retry_receipt_id": None,
                "event_id": receipt.event_id,
                "target_adapter": receipt.target_adapter,
                "attempt_number": receipt.attempt_number,
            })

            result_receipt = await self._pipeline.deliver_to_target(
                event=event,
                route=route,
                plan=plan,
                previous_receipt=receipt,
                source="retry",
                replay_run_id=None,
            )

            # Success path — deliver_to_target returns a receipt.
            self.state.processed += 1
            self.state.succeeded += 1
            self._emit("retry_succeeded", {
                "receipt_id": result_receipt.receipt_id,
                "parent_receipt_id": parent_receipt_id,
                "retry_receipt_id": result_receipt.receipt_id,
                "event_id": receipt.event_id,
                "target_adapter": receipt.target_adapter,
                "attempt_number": result_receipt.attempt_number,
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.processed += 1
            self.state.failed += 1
            # Check if a dead-lettered receipt was appended by the pipeline.
            # FIX 2: guard storage calls so errors don't swallow the original.
            try:
                is_dead_lettered = await self._check_dead_lettered(
                    receipt.event_id, receipt.target_adapter,
                    parent_receipt_id,
                )
            except Exception:
                _logger.exception(
                    "RetryWorker: error checking dead-lettered for %s/%s",
                    receipt.event_id, receipt.target_adapter,
                )
                is_dead_lettered = False

            if is_dead_lettered:
                self.state.dead_lettered += 1
                try:
                    dl_receipt_id = await self._find_dead_letter_receipt_id(
                        receipt.event_id, receipt.target_adapter,
                        parent_receipt_id,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: error finding dead-letter receipt for %s/%s",
                        receipt.event_id, receipt.target_adapter,
                    )
                    dl_receipt_id = None
                self._emit("retry_dead_lettered", {
                    "receipt_id": dl_receipt_id or parent_receipt_id,
                    "parent_receipt_id": parent_receipt_id,
                    "retry_receipt_id": dl_receipt_id,
                    "event_id": receipt.event_id,
                    "target_adapter": receipt.target_adapter,
                    "attempt_number": receipt.attempt_number + 1,
                })
            else:
                # FIX 1: look up the failed receipt for retry_receipt_id.
                try:
                    new_receipt = await self._find_failed_receipt(
                        receipt.event_id, receipt.target_adapter,
                        parent_receipt_id,
                    )
                except Exception:
                    _logger.exception(
                        "RetryWorker: error finding failed receipt for %s/%s",
                        receipt.event_id, receipt.target_adapter,
                    )
                    new_receipt = None
                self._emit("retry_failed", {
                    "receipt_id": parent_receipt_id,
                    "parent_receipt_id": parent_receipt_id,
                    "retry_receipt_id": new_receipt.receipt_id if new_receipt else None,
                    "event_id": receipt.event_id,
                    "target_adapter": receipt.target_adapter,
                    "attempt_number": receipt.attempt_number,
                })
            _logger.debug(
                "RetryWorker: delivery failed for receipt %s",
                receipt.receipt_id,
                exc_info=True,
            )
        finally:
            if self._capacity is not None:
                await self._capacity.release_delivery()

    async def _check_dead_lettered(
        self, event_id: str, target_adapter: str, parent_receipt_id: str,
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
        self, event_id: str, target_adapter: str, parent_receipt_id: str,
    ) -> str | None:
        """Return the receipt_id of the dead-lettered receipt for this lineage, if any."""
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
        self, event_id: str, target_adapter: str, parent_receipt_id: str,
    ) -> Any | None:
        """Find the latest failed receipt for this retry lineage."""
        receipts = await self._storage.list_receipts_for_event(event_id)
        for r in reversed(receipts):
            if (r.status == "failed"
                and r.target_adapter == target_adapter
                and r.parent_receipt_id == parent_receipt_id):
                return r
        return None
