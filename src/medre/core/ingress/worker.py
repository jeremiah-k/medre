"""Recoverable worker for routing durably admitted inbound events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import Protocol

from medre.core.ingress.types import (
    DurableIngressDeferredError,
    IngressWorkerStopResult,
    IngressWorkItem,
)

_logger = logging.getLogger(__name__)


class _IngressStorage(Protocol):
    async def claim_ingress_work(
        self, *, worker_id: str, limit: int, lease_seconds: float
    ) -> list[IngressWorkItem]: ...

    async def complete_ingress_work(self, event_id: str, *, worker_id: str) -> bool: ...

    async def release_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool: ...

    async def defer_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool: ...

    async def fail_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool: ...

    async def renew_ingress_work_lease(
        self, event_id: str, *, worker_id: str, lease_seconds: float
    ) -> bool: ...


class _IngressPipeline(Protocol):
    async def process_admitted_event(self, event_id: str) -> object: ...


class _IngressLeaseLostError(RuntimeError):
    """Raised when processing continues after durable work ownership is lost."""


class DurableIngressWorker:
    """Claim and process persisted ingress work until stopped.

    Processing failures are retried only up to ``max_attempts`` and then moved
    to terminal ``failed`` state.  Operational deferrals caused by capacity or
    shutdown do not consume that terminal failure budget.  A heartbeat renews
    ownership while routing is active,
    preventing a long-running event from being reclaimed by another worker
    generation merely because the original lease interval elapsed.
    """

    def __init__(
        self,
        *,
        storage: _IngressStorage,
        pipeline: _IngressPipeline,
        interval_seconds: float = 0.5,
        batch_size: int = 25,
        lease_seconds: float = 30.0,
        max_attempts: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._storage = storage
        self._pipeline = pipeline
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._logger = logger or _logger
        self._worker_id = f"ingress-{uuid.uuid4().hex}"
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._processed = 0
        self._failures = 0
        self._lost_leases = 0
        self._terminal_failures = 0
        self._deferrals = 0
        self._deferred_this_cycle = False
        self._active_event_id: str | None = None
        self._forced_cancellations = 0

    @property
    def running(self) -> bool:
        """Return whether the worker task is still alive."""
        return self._task is not None and not self._task.done()

    @property
    def processed(self) -> int:
        """Return successfully completed work count for this generation."""
        return self._processed

    @property
    def failures(self) -> int:
        """Return processing or claim failures observed by this generation."""
        return self._failures

    @property
    def lost_leases(self) -> int:
        """Return ownership losses detected before a state transition."""
        return self._lost_leases

    @property
    def terminal_failures(self) -> int:
        """Return rows moved to terminal ``failed`` state."""
        return self._terminal_failures

    @property
    def deferrals(self) -> int:
        """Return capacity/shutdown deferrals retained for later processing."""
        return self._deferrals

    @property
    def active_event_id(self) -> str | None:
        """Return the event currently being processed, if any."""
        return self._active_event_id

    @property
    def forced_cancellations(self) -> int:
        """Return shutdowns that exceeded the worker grace period."""
        return self._forced_cancellations

    async def start(self) -> None:
        """Start the background claim loop idempotently."""
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="medre-durable-ingress")

    def _consume_worker_result(self, task: asyncio.Task[None]) -> None:
        """Clear a retained worker task and consume its terminal exception."""
        if self._task is task:
            self._task = None
        with suppress(asyncio.CancelledError):
            task.exception()

    async def _request_worker_cancellation(self, task: asyncio.Task[None]) -> None:
        """Request cancellation without waiting indefinitely for cooperation."""
        if task.done():
            self._consume_worker_result(task)
            return
        task.cancel()
        # Give cooperative coroutines a bounded window to observe cancellation.
        # One asyncio.sleep(0) is often not enough because nested awaits
        # like ``asyncio.wait`` need multiple event-loop passes for full
        # cleanup. A cancellation-resistant pipeline that intentionally
        # suppresses cancellation remains retained in ``self._task`` and its
        # eventual result is consumed by the callback below.
        for _ in range(5):
            if task.done():
                break
            await asyncio.sleep(0)
        if task.done():
            self._consume_worker_result(task)
        else:
            task.add_done_callback(self._consume_worker_result)

    async def stop(self, *, grace_seconds: float = 0.0) -> IngressWorkerStopResult:
        """Stop claiming new work and report whether the task terminated.

        ``grace_seconds`` bounds how long an already-claimed event may finish.
        No new row is claimed after the stop signal. When the grace period
        expires, cancellation is requested without waiting indefinitely afterward.
        If processing suppresses cancellation, the worker task remains retained and
        the returned ``stopped`` flag is false. Shared runtime dependencies must not
        be torn down while that retained task can still perform side effects.
        """
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        self._stop.set()
        task = self._task
        if task is None:
            return IngressWorkerStopResult(
                stopped=True, cancellation_requested=False, active_event_id=None
            )
        cancellation_requested = False
        try:
            if grace_seconds > 0 and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
                except TimeoutError:
                    pass
            if not task.done():
                if self._active_event_id is not None:
                    self._forced_cancellations += 1
                cancellation_requested = True
                await self._request_worker_cancellation(task)
        except asyncio.CancelledError:
            if not task.done():
                await self._request_worker_cancellation(task)
            raise
        finally:
            if task.done():
                self._consume_worker_result(task)
        stopped = task.done()
        return IngressWorkerStopResult(
            stopped=stopped,
            cancellation_requested=cancellation_requested,
            active_event_id=None if stopped else self._active_event_id,
        )

    async def _renew_lease(self, event_id: str) -> None:
        interval = max(0.01, self._lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            owned = await self._storage.renew_ingress_work_lease(
                event_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
            if not owned:
                raise _IngressLeaseLostError(
                    f"durable ingress lease lost for event {event_id}"
                )

    async def _process_with_lease(self, item: IngressWorkItem) -> None:
        processing = asyncio.create_task(
            self._pipeline.process_admitted_event(item.event_id)
        )
        renewal = asyncio.create_task(self._renew_lease(item.event_id))
        try:
            done, _ = await asyncio.wait(
                {processing, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                exc = renewal.exception()
                processing.cancel()
                with suppress(asyncio.CancelledError):
                    await processing
                if exc is not None:
                    raise exc
                raise _IngressLeaseLostError(
                    f"durable ingress lease renewal ended for {item.event_id}"
                )
            await processing
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            if not processing.done():
                processing.cancel()
                with suppress(asyncio.CancelledError):
                    await processing

    async def run_once(self) -> int:
        """Claim and process up to one batch, returning completed count.

        Work is claimed one item at a time so no unstarted item waits behind
        another event while its lease is already counting down.
        """
        completed = 0
        self._deferred_this_cycle = False
        for _ in range(self._batch_size):
            if self._stop.is_set():
                break
            work = await self._storage.claim_ingress_work(
                worker_id=self._worker_id,
                limit=1,
                lease_seconds=self._lease_seconds,
            )
            if not work:
                break
            item = work[0]
            self._active_event_id = item.event_id
            deferred = False
            try:
                await self._process_with_lease(item)
            except asyncio.CancelledError:
                raise
            except _IngressLeaseLostError:
                self._lost_leases += 1
                self._failures += 1
                self._logger.error(
                    "Durable ingress lease lost while processing: event_id=%s attempt=%d",
                    item.event_id,
                    item.attempts,
                )
            except DurableIngressDeferredError as exc:
                deferred = True
                self._deferrals += 1
                error = f"{type(exc).__name__}: {exc}"
                changed = await self._storage.defer_ingress_work(
                    item.event_id, worker_id=self._worker_id, error=error
                )
                if not changed:
                    self._lost_leases += 1
                else:
                    self._logger.info(
                        "Durable ingress deferred without consuming retry budget: "
                        "event_id=%s attempt=%d reasons=%s",
                        item.event_id,
                        item.attempts,
                        ",".join(exc.reasons),
                    )
            except Exception as exc:
                self._failures += 1
                error = f"{type(exc).__name__}: {exc}"
                self._logger.exception(
                    "Durable ingress processing failed: event_id=%s attempt=%d",
                    item.event_id,
                    item.attempts,
                )
                if item.attempts >= self._max_attempts:
                    changed = await self._storage.fail_ingress_work(
                        item.event_id, worker_id=self._worker_id, error=error
                    )
                    if changed:
                        self._terminal_failures += 1
                    else:
                        self._lost_leases += 1
                else:
                    changed = await self._storage.release_ingress_work(
                        item.event_id, worker_id=self._worker_id, error=error
                    )
                    if not changed:
                        self._lost_leases += 1
            else:
                changed = await self._storage.complete_ingress_work(
                    item.event_id, worker_id=self._worker_id
                )
                if changed:
                    completed += 1
                    self._processed += 1
                else:
                    self._lost_leases += 1
                    self._logger.error(
                        "Durable ingress completion lost ownership: event_id=%s",
                        item.event_id,
                    )
            finally:
                self._active_event_id = None
            if deferred:
                self._deferred_this_cycle = True
            if deferred or self._stop.is_set():
                break
        return completed

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                processed = 0
                self._logger.exception("Durable ingress claim cycle failed; retrying")
            if processed == 0 or self._deferred_this_cycle:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._interval_seconds
                    )
                except TimeoutError:
                    pass
