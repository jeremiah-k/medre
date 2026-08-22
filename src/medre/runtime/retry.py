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
4. Asks ``DeliveryLifecycleService`` to reconcile any receipt evidence from a
   previously interrupted attempt before transport is invoked.
5. Calls ``PipelineRunner.deliver_to_target(... previous_receipt=...)`` only
   when no persisted next-attempt evidence already determines the outcome.
6. Delegates durable outbox transitions to ``DeliveryLifecycleService``;
   the worker retains polling, capacity, counters, and event orchestration.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.engine.pipeline.delivery_lifecycle import (
        DeliveryLifecycleService,
        DeliveryLifecycleStorage,
    )
    from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
    from medre.core.supervision.capacity import CapacityController
    from medre.runtime.events import EventBuffer

from medre.config.model import RetryConfig
from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptFinalization
from medre.core.engine.pipeline.retry_plan import (
    reconstruct_retry_delivery_plan,
)
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.events import RuntimeEventType

__all__ = ["RetryWorker", "RetryWorkerState", "RetryWorkerStorage"]

_logger = logging.getLogger(__name__)


class RetryWorkerStorage(Protocol):
    """Read/claim storage surface used directly by :class:`RetryWorker`.

    Durable lifecycle mutations are deliberately absent.  The worker passes
    the concrete backend to :class:`DeliveryLifecycleService`, which owns all
    retry outbox state transitions.
    """

    async def claim_due_outbox_items(
        self,
        now: str,
        worker_id: str,
        lease_seconds: int = 30,
        limit: int = 20,
    ) -> list[DeliveryOutboxItem]: ...

    async def count_outbox_by_status(self) -> dict[str, int]: ...

    async def get(self, event_id: str) -> CanonicalEvent | None: ...

    async def delivery_status(
        self,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> DeliveryReceipt | None: ...


if TYPE_CHECKING:

    class RetryWorkerBackend(RetryWorkerStorage, DeliveryLifecycleStorage, Protocol):
        """Storage backend required at the RetryWorker composition boundary.

        ``RetryWorker`` itself retains only the narrow ``RetryWorkerStorage``
        read/claim view.  The same backend is exposed separately to
        ``DeliveryLifecycleService`` for lifecycle-owned evidence reads and
        durable transitions.
        """


@dataclass
class RetryWorkerState:
    """Snapshot-visible state for the retry worker.

    .. note:: **Durability boundary.** All fields in this dataclass are
       **runtime-local, in-memory state**.  They are *not* persisted to
       storage and do not survive process restart.  In particular,
       ``abandoned`` is a per-process flag that protects against
       double-launching a retry task while a previous one is still alive.
       After a process restart the flag resets to ``False`` because the
       old task no longer exists.  Consumers must not treat these fields
       as durable lifecycle state — they are ephemeral operational
       visibility for the current process only.

    Attributes
    ----------
    abandoned:
        ``True`` if the previous :meth:`RetryWorker.stop` exited while the
        background task was still running and could not be cancelled
        within the grace period.  In this state :meth:`RetryWorker.start`
        refuses to launch a second worker, because doing so would
        silently launch a duplicate task over the same outbox while the
        abandoned one is still alive.  The caller must inspect this flag
        and either reset the worker or shut the entire runtime down.

        **This flag is runtime-local and non-durable.**  It exists only
        in the current process's memory.  It is *not* written to storage
        and will be ``False`` after a process restart, since the abandoned
        task from the previous process no longer exists.
    previous_run_in_progress:
        Count of durable outbox rows observed in ``in_progress`` immediately
        before this worker generation starts.  ``None`` means the startup
        storage snapshot could not be read; ``0`` means it was read and no
        unfinished rows were present.  A positive value is evidence of work
        that predates this worker generation, but is **not** proof that the
        prior worker was abandoned and does not imply that every row is already
        reclaimable (an unexpired lease may still defer a claim).
    """

    enabled: bool = False
    running: bool = False
    abandoned: bool = False
    previous_run_in_progress: int | None = None
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
        storage: RetryWorkerBackend,
        pipeline: PipelineRunner,
        capacity_controller: CapacityController | None,
        *,
        retry_config: RetryConfig | None = None,
        enabled: bool | None = None,
        interval_seconds: float | None = None,
        batch_size: int | None = None,
        max_attempts: int | None = None,
        event_buffer: EventBuffer | None = None,
        lifecycle: DeliveryLifecycleService | None = None,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        config = retry_config if retry_config is not None else RetryConfig()
        self._storage: RetryWorkerStorage = storage
        # The constructor requires the shared backend to satisfy both storage
        # protocols.  Internally the worker keeps the read/claim view narrow,
        # while lifecycle-owned mutations receive the lifecycle view only.
        self._lifecycle_storage: DeliveryLifecycleStorage = storage
        self._pipeline = pipeline
        self._capacity = capacity_controller
        if lifecycle is None:
            from medre.core.engine.pipeline.delivery_lifecycle import (
                DeliveryLifecycleService,
            )

            lifecycle = DeliveryLifecycleService(logger=_logger)
        self._lifecycle = lifecycle
        self._enabled = enabled if enabled is not None else config.enabled
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else config.interval_seconds
        )
        self._batch_size = batch_size if batch_size is not None else config.batch_size
        self._max_attempts = (
            max_attempts if max_attempts is not None else config.max_attempts
        )
        self._event_buffer = event_buffer
        if stop_timeout_seconds <= 0:
            raise ValueError(
                f"stop_timeout_seconds must be > 0, got {stop_timeout_seconds!r}"
            )
        self._stop_timeout = stop_timeout_seconds
        self._shutdown_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Retained reference to a still-running task that was abandoned
        # by :meth:`stop` because it survived the cancel grace period.
        # Prevents the event loop from garbage-collecting the task while
        # it is still alive; removed via a done callback when it finishes.
        self._abandoned_task: asyncio.Task[None] | None = None
        # Serializes concurrent :meth:`stop` calls so the worker
        # emits ``retry_stopped`` / ``retry_abandoned`` exactly once.
        self._stop_lock = asyncio.Lock()
        self._outbox_counts: dict[str, int] = {}
        self._cycle_completed: bool = False
        self.state = RetryWorkerState(enabled=self._enabled)

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

    async def _capture_startup_outbox_evidence(self) -> None:
        """Capture pre-existing ``in_progress`` work before this worker runs.

        The read is deliberately observational.  It does not claim, release,
        or otherwise transition any durable outbox row.  Because the retry
        worker starts before adapters begin accepting new ingress, any
        ``in_progress`` rows seen here predate this worker generation and are
        useful evidence after a prior process stopped unexpectedly.

        Storage-read failure is non-fatal: the normal worker loop remains the
        recovery authority and can still claim due work later.
        """

        try:
            counts = await self._storage.count_outbox_by_status()
        except Exception:
            _logger.debug(
                "RetryWorker could not read startup outbox counts",
                exc_info=True,
            )
            self.state.previous_run_in_progress = None
            return

        raw_count = counts.get("in_progress", 0)
        in_progress = raw_count if isinstance(raw_count, int) else 0
        self.state.previous_run_in_progress = max(in_progress, 0)
        if self.state.previous_run_in_progress <= 0:
            return

        _logger.warning(
            "RetryWorker startup observed %d pre-existing in_progress "
            "outbox item(s); these rows predate this worker generation and "
            "will be reclaimed only when storage claim rules allow",
            self.state.previous_run_in_progress,
        )
        self._emit(
            "retry_unfinished_work_detected",
            {
                "in_progress_count": self.state.previous_run_in_progress,
                "source": "durable_outbox_startup_snapshot",
            },
        )

    def _record_lifecycle_persistence_error(
        self,
        item: DeliveryOutboxItem,
        error: Exception,
        *,
        attempt_number: int,
    ) -> None:
        """Report a lifecycle write/read failure without inventing durable state.

        The outbox lease remains the recovery mechanism.  This helper only
        updates process-local observability and therefore never claims that a
        retry/dead-letter/success transition committed when lifecycle storage
        raised.
        """
        self.state.failed += 1
        self._emit(
            "retry_failed",
            {
                "receipt_id": item.receipt_id or item.outbox_id,
                "parent_receipt_id": item.parent_receipt_id,
                "retry_receipt_id": None,
                "event_id": item.event_id,
                "target_adapter": item.target_adapter,
                "attempt_number": attempt_number,
                "status": "lifecycle_persistence_error",
                "failure_kind": "lifecycle_persistence_error",
                "error": f"{type(error).__name__}: {error}",
                "next_retry_at": None,
            },
        )

    def _record_retry_finalization(
        self,
        item: DeliveryOutboxItem,
        finalization: RetryAttemptFinalization,
        *,
        error_summary: str | None = None,
        reconciled: bool = False,
    ) -> None:
        """Project one committed lifecycle outcome onto runtime observability.

        Durable state is already authoritative when this method runs.  This
        helper keeps counters and runtime events exhaustive and consistent for
        live retry finalization and claim-time reconciliation alike.
        """
        detail = {
            "event_id": item.event_id,
            "target_adapter": item.target_adapter,
            "attempt_number": finalization.attempt_number,
        }
        if reconciled:
            detail["reconciled"] = True

        if finalization.outcome == "accepted":
            self.state.succeeded += 1
            self._emit(
                "retry_succeeded",
                {
                    **detail,
                    "receipt_id": finalization.receipt_id
                    or item.receipt_id
                    or item.outbox_id,
                    "parent_receipt_id": item.receipt_id or item.outbox_id,
                    "retry_receipt_id": finalization.receipt_id,
                },
            )
            return

        if finalization.outcome == "dead_lettered":
            self.state.failed += 1
            self.state.dead_lettered += 1
            self._emit(
                "retry_dead_lettered",
                {
                    **detail,
                    "receipt_id": item.receipt_id or item.outbox_id,
                    "parent_receipt_id": item.parent_receipt_id,
                    "retry_receipt_id": finalization.receipt_id,
                    "failure_kind": finalization.failure_kind,
                },
            )
            return

        if finalization.outcome in {"retry_wait", "suppressed"}:
            self.state.failed += 1
            self._emit(
                "retry_failed",
                {
                    **detail,
                    "receipt_id": item.receipt_id or item.outbox_id,
                    "parent_receipt_id": item.parent_receipt_id,
                    "retry_receipt_id": finalization.receipt_id,
                    "status": finalization.outcome,
                    "failure_kind": finalization.failure_kind,
                    "error": error_summary
                    or finalization.failure_kind
                    or finalization.outcome,
                    "next_retry_at": (
                        finalization.next_retry_at.isoformat()
                        if finalization.next_retry_at is not None
                        else None
                    ),
                },
            )
            return

        _logger.warning(
            "RetryWorker: unhandled retry finalization outcome %r for outbox %s; "
            "no counter or event recorded",
            finalization.outcome,
            item.outbox_id,
        )

    def _finalize_task_outcome(
        self, task: asyncio.Task[None]
    ) -> tuple[bool, BaseException | None]:
        """Inspect a finished worker task, clear worker state, emit the
        correct terminal event.

        Called from every clean-stop path in :meth:`stop` and
        :meth:`_force_cancel_with_poll` once ``task.done()`` is true.
        Retrieves ``task.exception()`` so Python does not log
        ``Task exception was never retrieved`` for crashes, and chooses
        between ``retry_stopped`` and ``retry_failed`` based on whether
        the task exited cleanly or raised.

        Always clears :attr:`_task` and flips :attr:`state.running` to
        ``False`` — the worker is done regardless of exit reason, and
        leaving ``running=True`` after the task finished would prevent
        a future :meth:`start` from launching a replacement.

        A cancelled task is treated as a **clean** stop (not a crash):
        the cancellation is the expected outcome of the stop sequence,
        and emitting ``retry_failed`` for it would be a false alarm.
        The caller's ``stop_timeout_seconds`` + polling logic decided
        to cancel, so the cancellation is by design.

        Returns
        -------
        ``(clean, exc)`` where ``clean`` is ``True`` if the task exited
        without an exception and ``exc`` is the exception instance if
        it raised (``None`` otherwise).  Callers use ``clean`` to
        decide between ``_logger.info("RetryWorker stopped")`` and
        logging the failure.
        """
        assert task.done(), "task must be done before finalization"
        counts = {
            "processed": self.state.processed,
            "succeeded": self.state.succeeded,
            "failed": self.state.failed,
            "dead_lettered": self.state.dead_lettered,
        }
        # Worker is done in both branches; clear the reference and
        # flip ``running`` so a future ``start()`` is allowed.
        self._task = None
        self.state.running = False
        if task.cancelled():
            # Task was cancelled (expected outcome of stop).  Emit
            # ``retry_stopped`` — the cancellation is by design, not
            # a crash.  Note: ``task.exception()`` would raise
            # ``CancelledError`` for a cancelled task, so we must
            # check ``cancelled()`` first.
            self._emit("retry_stopped", counts)
            return (True, None)
        # ``task.exception()`` returns ``None`` for cleanly-finished
        # tasks and the exception instance for crashed ones.  Calling
        # it also marks the exception as retrieved, suppressing the
        # ``Task exception was never retrieved`` warning.
        exc = task.exception()
        if exc is None:
            self._emit("retry_stopped", counts)
            return (True, None)
        # Task crashed with a non-cancellation exception.  Log with
        # the full traceback for operators and emit ``retry_failed``
        # so downstream observers see the failure honestly instead of
        # a misleading clean-stop.
        _logger.error(
            "RetryWorker task exited with exception: %s",
            exc,
            exc_info=exc,
        )
        self._emit(
            "retry_failed",
            {
                **counts,
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": type(exc).__name__,
            },
        )
        return (False, exc)

    def _retain_abandoned_task(self, task: asyncio.Task[None]) -> None:
        """Retain *task* in :attr:`_abandoned_task` with a done callback.

        Called when :meth:`stop` or :meth:`_force_cancel_with_poll` abandons
        a still-running background task (the worker survived the cancel grace
        period).  Without this retention, the ``self._task`` reference would
        already have been cleared, allowing the event loop to garbage-collect
        the task while it is still running.

        The done callback consumes the task's exception (if any) so Python
        does not emit ``Task exception was never retrieved``, and clears the
        retained reference.
        """
        self._abandoned_task = task

        def _on_done(task: asyncio.Task[None]) -> None:
            try:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        _logger.warning(
                            "Abandoned RetryWorker task raised: %s",
                            exc,
                        )
            except Exception:
                # cleanup-silent: inner guard around the warning call
                # above; never mask the original task outcome.
                pass
            finally:
                if self._abandoned_task is task:
                    self._abandoned_task = None

        task.add_done_callback(_on_done)

    async def start(self) -> None:
        """Start the retry worker background task.

        Refuses to launch when the worker is already running, or when a
        previous :meth:`stop` abandoned the background task while it was
        still alive.  In the abandoned case launching again would
        silently double-process the outbox, so :attr:`state.abandoned`
        must be cleared by the caller first.
        """
        if not self._enabled:
            return
        if self.state.abandoned:
            _logger.error(
                "RetryWorker.start refused: previous stop abandoned the "
                "background task; worker must not be restarted without "
                "operator intervention"
            )
            # NOTE: retry_start_refused is a runtime event for operational
            # visibility into the current process's worker health.  It is
            # an in-memory event buffer emission — not persisted state, not
            # a lifecycle transition, and not durable across process restart.
            # It exists solely so operators and diagnostic bundles can see
            # that start was refused during this process's lifetime.
            self._emit(
                "retry_start_refused",
                {
                    "reason": "abandoned",
                    "stop_timeout_seconds": self._stop_timeout,
                    "processed": self.state.processed,
                    "succeeded": self.state.succeeded,
                    "failed": self.state.failed,
                    "dead_lettered": self.state.dead_lettered,
                },
            )
            return
        if self._task is not None:
            return
        await self._capture_startup_outbox_evidence()
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
                "previous_run_in_progress": self.state.previous_run_in_progress,
            },
        )

    async def stop(self) -> None:
        """Signal shutdown and wait for the worker to finish.

        Stop is **bounded** in two stages, both implemented as a
        short-cadence poll of :meth:`asyncio.Task.done` rather than
        :func:`asyncio.wait_for` — the latter cannot terminate a
        coroutine that catches and suppresses :class:`asyncio.CancelledError`
        (the cancel is consumed by an inner ``except`` block and the
        await never raises, leaving ``wait_for`` to wait forever).
        Polling ``task.done()`` is the only reliable hard bound for
        cancellation-resistant coroutines.

        1. **Cooperative stop.**  Set the shutdown event and poll
           ``task.done()`` at 10 ms intervals until either the task
           finishes or ``stop_timeout_seconds`` elapses.
        2. **Forced cancellation.**  If the cooperative stage times out,
           cancel the task once and poll again for a second
           ``stop_timeout_seconds`` grace period.

        ``stop()`` is serialised by an internal :class:`asyncio.Lock`
        so concurrent callers cannot emit duplicate ``retry_stopped``
        or ``retry_abandoned`` events.

        Outcomes:

        * **Cancellation-responsive task** -- the background task
          finishes within the grace period.  :attr:`_task` is cleared,
          :attr:`state.running` is set to ``False``, and a
          ``retry_stopped`` event is emitted.  ``stop()`` returns
          promptly.
        * **Cancellation-resistant task** -- the task survives both
          grace periods.  ``stop()`` installs a done callback on the
          task to consume any exception (preventing ``Task exception
          was never retrieved`` warnings), clears :attr:`_task`,
          flips :attr:`state.running` to ``False``, sets
          :attr:`state.abandoned = True``, and emits a
          ``retry_abandoned`` event so downstream observers see the
          failure honestly.  :meth:`start` will refuse subsequent
          launches while ``abandoned`` is set.
        * **External cancellation of ``stop()`` itself** -- if the
          caller cancels the ``await stop()`` (e.g. ``MedreApp.stop()``
          hits a shutdown timeout), the worker's :attr:`state.abandoned`
          is set and a ``retry_abandoned`` event is emitted before the
          :class:`asyncio.CancelledError` is re-raised.  This makes the
          "stop was cancelled" state distinguishable from
          "stop succeeded" and from "stop never called".
        """
        async with self._stop_lock:
            if self._task is None:
                return
            if self.state.abandoned:
                # A previous stop() already abandoned the worker; do not
                # emit duplicate retry_stopped / retry_failed events.
                return
            _logger.info(
                "RetryWorker stop requested: two-stage bounded shutdown "
                "(stage 1: cooperative poll, stage 2: forced cancel), "
                "effective wall-time up to ~%.1fs "
                "(stop_timeout_seconds=%.1f x 2 stages)",
                self._stop_timeout * 2,
                self._stop_timeout,
            )
            self._shutdown_event.set()
            loop = asyncio.get_running_loop()
            # Stage 1: cooperative stop.  Poll ``task.done()`` on a
            # short cadence until either the task finishes (clean
            # stop) or the grace period expires.
            deadline = loop.time() + self._stop_timeout
            task = self._task
            try:
                while not task.done():
                    if loop.time() >= deadline:
                        await self._force_cancel_with_poll(task=task)
                        return
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                # ``stop()`` itself was cancelled by the caller.  Two
                # sub-cases:
                #
                # 1. The task had already finished by the time the
                #    cancellation arrived (race between the polling
                #    loop's cooperative check and the external cancel).
                #    Do clean-stop cleanup so ``_task`` and
                #    ``state.running`` do not leak, then re-raise.
                # 2. The task is still alive.  Install a done callback
                #    to consume its exception, clear ``_task``, flip
                #    ``state.running`` to ``False``, mark the worker
                #    abandoned so the next start() call refuses a
                #    relaunch, emit the event for downstream
                #    observability, and re-raise.
                if task.done():
                    clean, exc = self._finalize_task_outcome(task)
                    if not clean:
                        # Re-raise the crash so the caller sees the
                        # real failure rather than a swallowed one.
                        raise exc from None  # type: ignore[misc]
                else:
                    self._retain_abandoned_task(task)
                    self._task = None
                    self.state.running = False
                    self.state.abandoned = True
                    self._emit(
                        "retry_abandoned",
                        {
                            "stop_timeout_seconds": self._stop_timeout,
                            "reason": "stop_cancelled",
                            "processed": self.state.processed,
                            "succeeded": self.state.succeeded,
                            "failed": self.state.failed,
                            "dead_lettered": self.state.dead_lettered,
                        },
                    )
                raise
            clean, exc = self._finalize_task_outcome(task)
            if clean:
                _logger.info("RetryWorker stopped")
            else:
                raise exc  # type: ignore[misc]

    async def _force_cancel_with_poll(self, task: asyncio.Task[None]) -> None:
        """Cancel *task* and wait for it with a hard time bound that does
        **not** rely on ``asyncio.wait_for``'s cancel mechanism.

        ``asyncio.wait_for(coro, timeout)`` cannot terminate a coroutine
        that swallows ``CancelledError`` indefinitely — the cancel is
        consumed by the inner ``except`` block and the await never
        raises.  This helper instead polls ``task.done()`` at short
        intervals, calling ``task.cancel()`` once on entry to give a
        cooperative task a chance to clean up, and giving up hard
        after ``self._stop_timeout`` seconds.

        Outcomes:

        * **Task finishes within the cancel grace** (the common case):
          :attr:`_task` is cleared, :attr:`state.running` is set to
          ``False``, and a ``retry_stopped`` event is emitted — the
          same observable result as the cooperative-stop path.
        * **Task does not finish** (cancellation-resistant): the task
          is recorded as abandoned.  A done callback is installed to
          consume any exception (preventing ``Task exception was never
          retrieved`` warnings).  :attr:`_task` is cleared,
          :attr:`state.running` is set to ``False``,
          :attr:`state.abandoned` is set to ``True``, and a
          ``retry_abandoned`` event is emitted.  The worker reports its
          failure honestly instead of pretending to have stopped cleanly.
        * **Race after the deadline**: if the task happens to finish
          between the deadline check and the loop exit, the worker
          takes the clean-stop path so it does not report a false
          abandonment.

        External cancellation of this helper is handled by
        :meth:`stop` (its only caller), which marks the worker
        abandoned and re-raises.
        """
        task.cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_timeout

        # Poll on a 10 ms cadence.  If the task is cancellation-
        # responsive it will finish within a few cycles; if it is
        # cancellation-resistant we will hit the deadline hard.
        while not task.done():
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.01)

        # Check AFTER the loop to correctly handle the race condition
        # where the task finishes between the deadline check and the
        # loop exit.
        if task.done():
            clean, exc = self._finalize_task_outcome(task)
            if not clean:
                raise exc  # type: ignore[misc]
        else:
            _logger.warning(
                "RetryWorker task did not cancel within %.1fs; "
                "abandoning (done callback installed, "
                "state.abandoned=True)",
                self._stop_timeout,
            )
            self._retain_abandoned_task(task)
            self._task = None
            self.state.running = False
            self.state.abandoned = True
            self._emit(
                "retry_abandoned",
                {
                    "stop_timeout_seconds": self._stop_timeout,
                    "processed": self.state.processed,
                    "succeeded": self.state.succeeded,
                    "failed": self.state.failed,
                    "dead_lettered": self.state.dead_lettered,
                },
            )

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
            await self._lifecycle.abandon_retry_outbox(
                self._lifecycle_storage,
                item,
                error_summary="Event not found in storage",
            )
            return

        previous_receipt = await self._storage.delivery_status(
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel,
        )

        # Reconstruct the delivery context (route + plan + retry policy)
        # from the persisted outbox item and previous receipt.  The helper
        # centralises all reconstruction semantics so the worker does not
        # duplicate planning logic.
        try:
            retry_context = reconstruct_retry_delivery_plan(
                item=item,
                previous_receipt=previous_receipt,
                default_max_attempts=self._max_attempts,
            )
        except Exception:
            _logger.exception(
                "RetryWorker: failed to reconstruct delivery context for outbox %s",
                item.outbox_id,
            )
            self.state.processed += 1
            self.state.failed += 1
            await self._lifecycle.abandon_retry_outbox(
                self._lifecycle_storage,
                item,
                error_summary="Reconstruction failure",
            )
            return

        # A worker can reclaim an expired ``in_progress`` row after a prior
        # process persisted receipt evidence but failed before committing the
        # matching outbox transition.  Reconcile that evidence first: invoking
        # the transport again could duplicate an already accepted delivery or
        # repeat a failed attempt whose schedule/dead-letter decision is durable.
        try:
            reconciled = await self._lifecycle.reconcile_retry_claim(
                self._lifecycle_storage,
                item,
                retry_context.retry_policy,
            )
        except Exception as lifecycle_exc:
            self.state.processed += 1
            _logger.exception(
                "RetryWorker: failed to reconcile claimed retry evidence for outbox %s",
                item.outbox_id,
            )
            self._record_lifecycle_persistence_error(
                item,
                lifecycle_exc,
                attempt_number=item.attempt_number + 1,
            )
            return

        if reconciled is not None:
            self.state.processed += 1
            self._record_retry_finalization(item, reconciled, reconciled=True)
            _logger.info(
                "RetryWorker: reconciled persisted attempt %d for outbox %s as %s; "
                "skipping transport",
                reconciled.attempt_number,
                item.outbox_id,
                reconciled.outcome,
            )
            return

        capacity_acquired = False

        if self._capacity is not None:
            try:
                acquired = await self._capacity.acquire_delivery()
                if not acquired:
                    self.state.failed += 1
                    _logger.warning(
                        "RetryWorker: capacity rejected for outbox %s",
                        item.outbox_id,
                    )
                    # Backoff is a lifecycle decision; the worker only
                    # reports the resulting schedule when it was persisted.
                    _cap_next: datetime | None = None
                    try:
                        _cap_next = await self._lifecycle.defer_retry_outbox(
                            self._lifecycle_storage,
                            item,
                            retry_context.retry_policy,
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
                            "next_retry_at": (
                                _cap_next.isoformat() if _cap_next else None
                            ),
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
                    # release_outbox_claim would restore to "pending" and
                    # cause immediate re-claim.  Delegate a durable backoff
                    # transition to the lifecycle authority instead.
                    await self._lifecycle.defer_retry_outbox(
                        self._lifecycle_storage,
                        item,
                        retry_context.retry_policy,
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
            route = retry_context.route
            plan = retry_context.plan

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
                outbox_id=item.outbox_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.processed += 1
            try:
                finalization = await self._lifecycle.finalize_retry_attempt_error(
                    self._lifecycle_storage,
                    item,
                    retry_context.retry_policy,
                    error=exc,
                )
            except Exception as lifecycle_exc:
                _logger.exception(
                    "RetryWorker: failed to reconcile retry lifecycle for outbox %s",
                    item.outbox_id,
                )
                self._record_lifecycle_persistence_error(
                    item,
                    lifecycle_exc,
                    attempt_number=item.attempt_number + 1,
                )
            else:
                self._record_retry_finalization(
                    item,
                    finalization,
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
            _logger.debug(
                "RetryWorker: delivery raised for outbox %s",
                item.outbox_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            self.state.processed += 1
            try:
                retry_succeeded = await self._lifecycle.finalize_retry_success(
                    self._lifecycle_storage,
                    item,
                    result_receipt,
                )
                finalization = RetryAttemptFinalization(
                    outcome="accepted" if retry_succeeded else "suppressed",
                    receipt_id=result_receipt.receipt_id,
                    failure_kind=result_receipt.failure_kind,
                    attempt_number=result_receipt.attempt_number,
                )
                self._record_retry_finalization(
                    item,
                    finalization,
                    error_summary=result_receipt.error,
                )
            except Exception as lifecycle_exc:
                _logger.exception(
                    "RetryWorker: failed to update outbox %s after successful delivery",
                    item.outbox_id,
                )
                self._record_lifecycle_persistence_error(
                    item,
                    lifecycle_exc,
                    attempt_number=result_receipt.attempt_number,
                )
        finally:
            if capacity_acquired and self._capacity is not None:
                await self._capacity.release_delivery()
