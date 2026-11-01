"""One live, owner-fenced content collection worker.

This module is *not* a scheduling framework. It mirrors the
:class:`~medre.runtime.retry.RetryWorker` shape, simplified to a single
sequential loop (collection is I/O-serialized on the storage executor's
single worker thread, so concurrent sweeps would only contend):

- single-process, in-process, at most one loop per live storage handle
- sweep cadence and batch size come from the operator's
  :class:`~medre.core.storage.content_retention.ContentRetentionPolicy`
- every mutation goes through ``collect_event_content(event_id,
  dry_run=False)`` — the guarded collector with its ``BEGIN IMMEDIATE``
  revalidation is the only deletion boundary; no preview, count, or
  cursor state ever grants mutation authority
- transient SQLite conditions defer the remaining page under a bounded
  backoff and an observable degraded state; integrity refusals are
  fail-closed and restrict (stop) the worker after repeated refusals
- stops cleanly on shutdown with a bounded two-stage stop, honest
  abandonment reporting, and runtime events
- never touches the durable activation epoch except through the storage
  epoch methods: the first start under an enabled relay-only policy
  opens the activation epoch (``runtime-first-start`` provenance, whose
  admission watermark pins the pre-activation history out of scope), and
  every sweep heartbeats the epoch forward-only before paging

The worker holds the ONE live storage authority handle for collection and
reaches no other storage surface, and no adapter callback path reaches
the worker. A stale activation heartbeat (an epoch whose last activity
observation is too old) suspends collection entirely — no collection and
no heartbeat — until a fresh operator-opened epoch appears.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from medre.core.storage.backend import (
    ContentCollectionEpochChanged,
    ContentCollectionTransientError,
    StorageError,
)
from medre.core.storage.content_retention import (
    CollectionCandidateSource,
    ContentRetentionPolicy,
    RetentionEpoch,
    retention_epoch_grace_seconds,
)
from medre.runtime.events import EventBuffer, RuntimeEventType

__all__ = [
    "CollectionWorkerStorage",
    "ContentCollectionWorker",
    "ContentCollectionWorkerError",
    "ContentCollectionWorkerState",
]

_logger = logging.getLogger(__name__)

#: Bounded in-memory error journal depth. Entries carry a timestamp, a
#: failure kind, and the event identifier only — never a message string
#: and never a content value.
_RECENT_ERROR_LIMIT = 8

#: The worker restricts itself (fail-closed stop) after this many
#: consecutive integrity refusals within one sweep.
_INTEGRITY_REFUSAL_RESTRICTION_THRESHOLD = 3

#: Upper multiplier of the policy interval for the transient backoff:
#: ``min(interval * 2**consecutive, 10 * interval)``, further bounded
#: by the shared epoch grace minus one interval.
_TRANSIENT_BACKOFF_CAP_MULTIPLIER = 10

#: Provenance recorded when the runtime itself opens the first activation
#: epoch; the watermark is the admission high-water at that instant.
_FIRST_ACTIVATION_ACTIVATOR = "runtime-first-start"

#: Poll cadence of the bounded stop stages (mirrors RetryWorker).
_STOP_POLL_SECONDS = 0.01


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_aware_iso(value: str) -> datetime | None:
    """Parse a durable ISO observation timestamp; ``None`` when unusable.

    A missing or unparseable or naive observation fails closed: the caller
    treats ``None`` as a stale epoch rather than guessing it is fresh.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _counted_int(value: Any) -> int:
    """Return *value* as an int when it is one, else ``0`` (fail-closed)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


class CollectionWorkerStorage(Protocol):
    """The one live storage authority surface this worker may touch.

    Exactly the guarded collector and the durable activation-epoch methods.
    The worker creates no second storage handle and reaches no other
    storage API; production wiring passes the runtime's single
    ``SQLiteStorage`` instance, which satisfies this protocol structurally.
    """

    async def collect_event_content(
        self,
        event_id: str,
        *,
        dry_run: bool = True,
        retention_epoch: int | None = None,
    ) -> dict[str, Any]: ...

    async def latest_retention_epoch(self) -> RetentionEpoch | None: ...

    async def open_retention_epoch(
        self, *, activated_by: str, now: datetime
    ) -> RetentionEpoch: ...

    async def observe_retention_heartbeat(
        self, *, epoch: int, now: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ContentCollectionWorkerError:
    """One bounded, content-free worker error journal entry.

    ``kind`` labels the failure family (``transient``, ``integrity_refused``,
    ``unexpected_status``, ``cycle``); ``event_id`` is the candidate
    identifier, or ``None`` for worker-level failures. No exception message
    and no content value is ever recorded — exception text is not proven
    content-free, so it stays in the logs only.
    """

    timestamp: str
    kind: str
    event_id: str | None = None


@dataclass
class ContentCollectionWorkerState:
    """Snapshot-visible state for the content collection worker.

    .. note:: **Durability boundary.** All fields in this dataclass are
       **runtime-local, in-memory state**. They are *not* persisted to
       storage and do not survive process restart. In particular,
       ``abandoned`` and ``restricted`` are per-process flags that guard
       this worker instance against relaunching a loop over a live
       predecessor; after a process restart they reset to ``False``
       because the old task no longer exists. Counters are cumulative for
       this process only. Consumers must not treat these fields as
       durable lifecycle state.

    Attributes
    ----------
    abandoned:
        ``True`` if a previous :meth:`ContentCollectionWorker.stop` exited
        while the background loop was still running and could not be
        cancelled within the grace period. :meth:`start` refuses to launch
        a replacement while this is set, because the abandoned loop can
        still transact (see :attr:`ContentCollectionWorker.has_retained_work`).
    restricted:
        ``True`` after repeated integrity refusals within one sweep — the
        fail-closed stop. Start is refused while this is set.
    awaiting_activation:
        ``True`` while the newest durable epoch's activity observation is
        stale: the worker idles without collecting and without
        heartbeating until a fresh operator-opened epoch appears.
    degraded:
        ``True`` while the most recent sweep attempt ended in a transient
        or cycle failure; a fully completed page clears it.
    last_sweep_at:
        Wall-time ISO string of the last sweep cycle that ran its
        collection phase (heartbeat + paging). Gate-only cycles (stale
        epoch, failed epoch read) do not update it.
    last_outcome:
        Per-outcome counts of the last completed collection phase, or
        ``None`` before the first one.
    recent_errors:
        Bounded journal (oldest dropped first) of content-free error
        entries.
    epoch:
        The epoch number this worker last observed, or ``None``.
    """

    enabled: bool = False
    running: bool = False
    abandoned: bool = False
    restricted: bool = False
    awaiting_activation: bool = False
    degraded: bool = False
    last_sweep_at: str | None = None
    events_examined: int = 0
    collected: int = 0
    already_collected: int = 0
    blocked: int = 0
    empty: int = 0
    failed: int = 0
    logical_bytes_removed: int = 0
    rows_removed: int = 0
    epoch: int | None = None
    last_outcome: dict[str, int] | None = None
    recent_errors: deque[ContentCollectionWorkerError] = field(
        default_factory=lambda: deque(maxlen=_RECENT_ERROR_LIMIT)
    )

    def counter_snapshot(self) -> dict[str, int]:
        """Return the cumulative counters as one plain dict for events."""
        return {
            "events_examined": self.events_examined,
            "collected": self.collected,
            "already_collected": self.already_collected,
            "blocked": self.blocked,
            "empty": self.empty,
            "failed": self.failed,
            "rows_removed": self.rows_removed,
            "logical_bytes_removed": self.logical_bytes_removed,
        }


class ContentCollectionWorker:
    """Bounded policy-governed collector over one live storage authority.

    The loop pages candidate event IDs from the injected
    :class:`~medre.core.storage.content_retention.CollectionCandidateSource`
    and hands each one to ``collect_event_content(event_id, dry_run=False)``
    on the single storage authority. It trusts nothing from any prior
    preview: the collector's own ``BEGIN IMMEDIATE`` revalidation is the
    only mutation authority, and every candidate is naturally revisited
    once per full cursor cycle, which is the bounded retry interval for
    blocked events (no per-event retry ledger exists).
    """

    def __init__(
        self,
        storage: CollectionWorkerStorage,
        candidate_source: CollectionCandidateSource,
        policy: ContentRetentionPolicy,
        *,
        event_buffer: EventBuffer | None = None,
        stop_timeout_seconds: float = 5.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError(
                f"stop_timeout_seconds must be > 0, got {stop_timeout_seconds!r}"
            )
        self._storage = storage
        self._candidate_source = candidate_source
        self._policy = policy
        self._event_buffer = event_buffer
        self._stop_timeout = stop_timeout_seconds
        self._now = now if now is not None else _default_now
        # In-memory (created_at, event_id) keyset cursor. Process-local by
        # design: after a restart the first cycle re-pages from scope head,
        # which is safe because collection is idempotent and an
        # already_collected decision is a cheap read outcome.
        self._cursor: tuple[str, str] | None = None
        # Every new process-local worker must re-establish its own safe
        # activation boundary before collecting. A fresh previous process's
        # heartbeat is not evidence that the policy stayed enabled during
        # the gap; a stale predecessor still requires explicit reactivation.
        self._startup_epoch_checked = False
        self._cursor_epoch: int | None = None
        # Transient-failure backoff: the next sweep may not start before this
        # instant. Enforced by the loop's idle wait, not by sweep() itself.
        self._next_sweep_not_before: datetime | None = None
        self._consecutive_transient = 0
        self._shutdown_event = asyncio.Event()
        self._sweep_wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Retained reference to a still-running loop that stop() abandoned
        # because it survived the cancel grace period; prevents the event
        # loop from garbage-collecting a live task. Cleared by its done
        # callback when the task finishes.
        self._abandoned_task: asyncio.Task[None] | None = None
        # Serializes lifecycle transitions so concurrent start() calls cannot
        # create duplicate loops and stop() cannot observe a half-started
        # one; stop() also emits its terminal event exactly once.
        self._stop_lock = asyncio.Lock()
        self._state = ContentCollectionWorkerState(enabled=not policy.retained)

    @property
    def state(self) -> ContentCollectionWorkerState:
        """Runtime-local observability snapshot (see the dataclass contract)."""
        return self._state

    @property
    def has_retained_work(self) -> bool:
        """Whether an abandoned loop can still perform side effects.

        An abandoned loop may be mid-collection transaction; callers must
        not tear down or close the storage authority while this is true.
        """
        return self._abandoned_task is not None and not self._abandoned_task.done()

    def _emit(self, event_type: str, detail: dict[str, Any]) -> None:
        """Emit a runtime event if an event buffer is configured."""
        if self._event_buffer is None:
            return
        try:
            rt = RuntimeEventType(event_type)
        except ValueError:
            return
        self._event_buffer.emit(rt, detail)

    def _record_worker_error(self, kind: str, event_id: str | None) -> None:
        self._state.recent_errors.append(
            ContentCollectionWorkerError(
                timestamp=self._now().isoformat(), kind=kind, event_id=event_id
            )
        )

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the collection loop.

        Silently does nothing for a declared ``retain`` policy (an
        observing policy never collects). Refuses to launch when a previous
        stop abandoned the loop or a fail-closed restriction is in effect —
        in both cases a duplicate loop could transact over the same storage
        authority — and is idempotent while running. The whole transition
        is serialized by the stop lock so concurrent starts cannot spawn
        duplicate workers.
        """
        async with self._stop_lock:
            if not self._state.enabled:
                return
            if self._state.abandoned:
                _logger.error(
                    "ContentCollectionWorker.start refused: previous stop "
                    "abandoned the background loop; worker must not be "
                    "restarted without operator intervention"
                )
                # NOTE: collection_start_refused is a runtime event for
                # operational visibility into the current process's worker
                # health. It is an in-memory event buffer emission — not
                # persisted state, not a lifecycle transition, and not
                # durable across process restart.
                self._emit(
                    "collection_start_refused",
                    {
                        "reason": "abandoned",
                        "stop_timeout_seconds": self._stop_timeout,
                        **self._state.counter_snapshot(),
                    },
                )
                return
            if self._state.restricted:
                _logger.error(
                    "ContentCollectionWorker.start refused: the worker "
                    "restricted itself after repeated integrity refusals; "
                    "collection stays fail-closed"
                )
                self._emit(
                    "collection_start_refused",
                    {
                        "reason": "restricted",
                        "stop_timeout_seconds": self._stop_timeout,
                        **self._state.counter_snapshot(),
                    },
                )
                return
            if self._task is not None:
                return
            self._shutdown_event.clear()
            self._sweep_wake.clear()
            self._startup_epoch_checked = False
            self._cursor_epoch = None
            self._cursor = None
            self._consecutive_transient = 0
            self._next_sweep_not_before = None
            self._state.degraded = False
            self._state.awaiting_activation = False
            self._state.running = True
            self._task = asyncio.create_task(self._run_loop())
            _logger.info(
                "Content collection worker started (mode=%s, interval=%ss, batch=%d)",
                self._policy.mode,
                self._policy.interval_seconds,
                self._policy.batch_limit,
            )
            self._emit(
                "collection_started",
                {
                    "interval_seconds": self._policy.interval_seconds,
                    "batch_limit": self._policy.batch_limit,
                },
            )

    async def stop(self, *, timeout_seconds: float | None = None) -> None:
        """Signal shutdown and wait for the loop to finish, bounded.

        Two-stage bounded stop mirroring the retry worker: a cooperative
        ``task.done()`` poll (a coroutine that suppresses cancellation
        would make ``wait_for`` wait forever), then a forced cancel with a
        polled grace period that retains a still-alive loop as an abandoned
        task. ``stop()`` is serialized by the stop lock so concurrent
        callers cannot emit duplicate terminal events, and an external
        cancellation of ``stop()`` itself marks the loop abandoned before
        re-raising, making "stop was cancelled" distinguishable from "stop
        succeeded".
        """
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        async with self._stop_lock:
            if self._task is None:
                return
            if self._state.abandoned:
                # A previous stop() already abandoned the loop; do not emit
                # duplicate terminal events.
                return
            budget = (
                self._stop_timeout * 2 if timeout_seconds is None else timeout_seconds
            )
            _logger.info(
                "ContentCollectionWorker stop requested: two-stage bounded "
                "shutdown (stage 1: cooperative poll, stage 2: forced cancel), "
                "effective wall-time up to ~%.1fs (stop_timeout_seconds=%.1f)",
                budget,
                self._stop_timeout,
            )
            self._shutdown_event.set()
            self._sweep_wake.set()
            loop = asyncio.get_running_loop()
            final_deadline = loop.time() + budget
            deadline = loop.time() + min(self._stop_timeout, budget / 2)
            task = self._task
            try:
                while not task.done():
                    if loop.time() >= deadline:
                        await self._force_cancel_with_poll(
                            task=task,
                            timeout_seconds=max(0.0, final_deadline - loop.time()),
                        )
                        return
                    await asyncio.sleep(_STOP_POLL_SECONDS)
            except asyncio.CancelledError:
                # stop() itself was cancelled by the caller. If the loop had
                # already finished, clean up and surface a crash honestly;
                # otherwise mark it abandoned (the loop can still transact)
                # before re-raising.
                if task.done():
                    clean, exc = self._finalize_task_outcome(task)
                    if not clean:
                        raise exc from None  # type: ignore[misc]
                else:
                    self._retain_abandoned_task(task)
                    self._task = None
                    self._state.running = False
                    self._state.abandoned = True
                    self._emit(
                        "collection_abandoned",
                        {
                            "stop_timeout_seconds": self._stop_timeout,
                            "reason": "stop_cancelled",
                            **self._state.counter_snapshot(),
                        },
                    )
                raise
            clean, exc = self._finalize_task_outcome(task)
            if clean:
                _logger.info("Content collection worker stopped")
            else:
                raise exc  # type: ignore[misc]

    def _finalize_task_outcome(
        self, task: asyncio.Task[None]
    ) -> tuple[bool, BaseException | None]:
        """Inspect a finished loop task, clear worker state, emit the
        terminal event.

        Retrieves ``task.exception()`` so Python does not log ``Task
        exception was never retrieved``. A cancelled task is a clean stop
        (the cancellation is the expected outcome of the bounded stop), a
        clean exit emits ``collection_stopped``, and a crash emits
        ``collection_failed`` with the exception type only — the full
        traceback stays in the logs. Always clears the task reference and
        flips ``running`` off.
        """
        assert task.done(), "task must be done before finalization"
        counts = self._state.counter_snapshot()
        self._task = None
        self._state.running = False
        if task.cancelled():
            self._emit("collection_stopped", counts)
            return (True, None)
        exc = task.exception()
        if exc is None:
            self._emit("collection_stopped", counts)
            return (True, None)
        _logger.error(
            "ContentCollectionWorker loop exited with exception: %s",
            exc,
            exc_info=exc,
        )
        self._emit(
            "collection_failed",
            {**counts, "error_type": type(exc).__name__},
        )
        return (False, exc)

    def _retain_abandoned_task(self, task: asyncio.Task[None]) -> None:
        """Retain *task* with a done callback that consumes its exception.

        Called when stop() abandons a still-running loop; without the
        retained reference the event loop could garbage-collect the live
        task, and without the done callback an eventual crash would warn
        about a never-retrieved exception.
        """
        self._abandoned_task = task

        def _on_done(task: asyncio.Task[None]) -> None:
            try:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        _logger.warning("Abandoned collection loop raised: %s", exc)
            except Exception:
                # cleanup-silent: never mask the original task outcome.
                pass
            finally:
                if self._abandoned_task is task:
                    self._abandoned_task = None

        task.add_done_callback(_on_done)

    async def _force_cancel_with_poll(
        self, task: asyncio.Task[None], *, timeout_seconds: float | None = None
    ) -> None:
        """Cancel *task* and wait with a hard bound that does not rely on
        ``asyncio.wait_for``'s cancel mechanism.

        A coroutine that swallows ``CancelledError`` indefinitely would
        hold ``wait_for`` forever; polling ``task.done()`` is the only
        reliable hard bound. A loop that survives the grace period is
        retained as abandoned and reported honestly.
        """
        task.cancel()
        # Bounded event-loop turns let the awaited storage call settle.
        for _ in range(5):
            if task.done():
                break
            await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (
            self._stop_timeout if timeout_seconds is None else timeout_seconds
        )
        while not task.done():
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_STOP_POLL_SECONDS)
        # Check AFTER the loop to handle the race where the task finishes
        # between the deadline check and the loop exit.
        if task.done():
            clean, exc = self._finalize_task_outcome(task)
            if not clean:
                raise exc  # type: ignore[misc]
        else:
            _logger.warning(
                "ContentCollectionWorker loop did not cancel within %.1fs; "
                "abandoning (done callback installed, state.abandoned=True)",
                self._stop_timeout,
            )
            self._retain_abandoned_task(task)
            self._task = None
            self._state.running = False
            self._state.abandoned = True
            self._emit(
                "collection_abandoned",
                {
                    "stop_timeout_seconds": self._stop_timeout,
                    **self._state.counter_snapshot(),
                },
            )

    # -- The loop ------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Sweep, then idle on the wake event until the next sweep.

        Cycle errors are logged and recorded as degraded state; the loop
        survives them. The idle wait uses ``wait_for`` over ``Event.wait``
        — a wait_for-safe awaitable — catching the py3.11 ``TimeoutError``,
        and extends past the policy interval while a transient backoff is
        pending. A restricted worker stops its own loop cleanly: the task
        ends, running clears, and start() refuses until operator
        intervention.
        """
        try:
            while not self._shutdown_event.is_set():
                self._sweep_wake.clear()
                try:
                    await self.sweep()
                except Exception:
                    _logger.exception("Content collection cycle error")
                    self._record_worker_error("cycle", None)
                    self._state.degraded = True
                if self._state.restricted:
                    break
                try:
                    await asyncio.wait_for(
                        self._sweep_wake.wait(), timeout=self._idle_timeout()
                    )
                except TimeoutError:
                    pass
        finally:
            # The loop coroutine is done in every exit path (shutdown,
            # restriction, crash, cancellation). Clearing running here keeps
            # the state honest even before stop() finalizes the task
            # reference; stop()'s finalize remains the single terminal-event
            # emitter for stop-driven exits.
            self._state.running = False

    def _idle_timeout(self) -> float:
        """Seconds to idle before the next sweep.

        The policy interval, extended to honor a pending transient backoff
        so a deferred sweep cannot spin on a persistently busy database.
        """
        timeout = float(self._policy.interval_seconds)
        if self._next_sweep_not_before is not None:
            remaining = (self._next_sweep_not_before - self._now()).total_seconds()
            timeout = max(timeout, remaining)
        return timeout

    # -- The sweep -----------------------------------------------------------

    async def sweep(self) -> None:
        """Run one bounded collection cycle.

        Sequence: gate on a fresh durable activation epoch (auto-opening
        the first one), heartbeat the epoch forward-only, page candidate
        event IDs, hand each candidate to the guarded collector with
        ``dry_run=False``, classify every outcome, and advance the
        in-memory cursor only across work that was actually examined.

        A transient SQLite failure aborts the remaining page with the
        cursor left untouched — the next sweep re-pages from before the
        failed event, so nothing is skipped — under a doubling backoff
        capped at ten policy intervals and at the epoch grace minus one interval.
        An integrity refusal is fail-closed:
        it is counted, journaled, and the cycle continues past it (the
        failed event is naturally re-examined next cycle), but three
        consecutive refusals within one sweep restrict the worker.
        """
        if not self._state.enabled or self._state.restricted or self._state.abandoned:
            # The public sweep method is also an authority boundary: direct
            # calls must never bypass the policy/start/restriction gates.
            return
        now = self._now()
        epoch = await self._ensure_activation_epoch(now)
        if epoch is None:
            # Stale activation epoch: no collection, no heartbeat, no paging.
            return
        if self._cursor_epoch != epoch.epoch:
            self._cursor = None
            self._cursor_epoch = epoch.epoch
        try:
            await self._storage.observe_retention_heartbeat(epoch=epoch.epoch, now=now)
        except ContentCollectionEpochChanged:
            # An operator opened a newer epoch after our read. Never page
            # candidates under the superseded watermark.
            self._cursor = None
            self._cursor_epoch = None
            return
        page = await self._candidate_source.page_collection_candidates(
            after=self._cursor,
            limit=self._policy.batch_limit,
            watermark=epoch.admission_watermark,
        )
        outcome: dict[str, int] = {
            "examined": 0,
            "collected": 0,
            "already_collected": 0,
            "blocked": 0,
            "empty": 0,
            "failed": 0,
        }
        consecutive_refusals = 0
        restricted = False
        for event_id in page.event_ids:
            if self._shutdown_event.is_set():
                # Cooperative shutdown: leave the cursor untouched so the
                # unexamined remainder of this page is re-paged on the next
                # generation; re-examining collected events is a cheap read.
                return
            self._state.events_examined += 1
            outcome["examined"] += 1
            try:
                result = await self._storage.collect_event_content(
                    event_id, dry_run=False, retention_epoch=epoch.epoch
                )
            except ContentCollectionEpochChanged:
                # Activation rotated between paging and the guarded write.
                # Restart the scan at the new watermark; do not count a
                # normal policy transition as an integrity failure.
                self._cursor = None
                self._cursor_epoch = None
                return
            except ContentCollectionTransientError:
                # Transient SQLite condition: abort the remaining page with
                # the cursor NOT advanced past the failed event, back off,
                # and surface the degraded state.
                self._state.degraded = True
                self._consecutive_transient += 1
                backoff_seconds = min(
                    self._policy.interval_seconds * 2**self._consecutive_transient,
                    self._policy.interval_seconds * _TRANSIENT_BACKOFF_CAP_MULTIPLIER,
                    # Leave one policy interval for collection and scheduling:
                    # a deliberate defer must not exhaust the freshness grace.
                    self._epoch_staleness_threshold() - self._policy.interval_seconds,
                )
                self._next_sweep_not_before = self._now() + timedelta(
                    seconds=backoff_seconds
                )
                self._record_worker_error("transient", event_id)
                _logger.warning(
                    "Content collection deferred for %s after a transient "
                    "SQLite condition; backing off %.1fs",
                    event_id,
                    backoff_seconds,
                )
                self._emit(
                    "collection_deferred",
                    {
                        "event_id": event_id,
                        "backoff_seconds": backoff_seconds,
                        "consecutive_transient": self._consecutive_transient,
                    },
                )
                return
            except StorageError as exc:
                # Integrity refusal: fail-closed bookkeeping, advance past
                # the event (it is re-examined next cycle), and restrict the
                # worker after repeated consecutive refusals in one sweep.
                consecutive_refusals += 1
                self._state.failed += 1
                outcome["failed"] += 1
                self._record_worker_error("integrity_refused", event_id)
                _logger.warning("Content collection refused for %s: %s", event_id, exc)
                self._emit(
                    "collection_failed",
                    {"event_id": event_id, "error_type": type(exc).__name__},
                )
                if consecutive_refusals >= _INTEGRITY_REFUSAL_RESTRICTION_THRESHOLD:
                    restricted = True
                    self._state.restricted = True
                    _logger.error(
                        "Content collection restricted after %d consecutive "
                        "integrity refusals in one sweep; failing closed and "
                        "stopping the loop",
                        consecutive_refusals,
                    )
                    self._emit(
                        "collection_restricted",
                        {
                            "epoch": epoch.epoch,
                            "event_id": event_id,
                            "consecutive_refusals": consecutive_refusals,
                        },
                    )
                    break
                continue
            self._classify_outcome(event_id, result, outcome)
            # A decided outcome breaks any run of consecutive integrity
            # refusals within this sweep.
            consecutive_refusals = 0
        self._state.last_sweep_at = now.isoformat()
        self._state.last_outcome = dict(outcome)
        if restricted:
            # The page was abandoned mid-flight; the cursor stays untouched.
            return
        if page.has_more:
            self._cursor = page.next_after
        else:
            # Cycle complete: the next sweep starts a new cycle from scope
            # head, so every blocked event is naturally revisited once per
            # cycle. A malformed page (has_more with a null continuation)
            # degrades to the same head restart, which is safe because
            # collection is idempotent.
            self._cursor = None
        # A page that completed without a transient abort clears the backoff
        # and the degraded flag.
        self._consecutive_transient = 0
        self._next_sweep_not_before = None
        self._state.degraded = False
        self._emit(
            "collection_swept",
            {
                "epoch": epoch.epoch,
                "cycle_complete": not page.has_more,
                **outcome,
            },
        )

    def _classify_outcome(
        self, event_id: str, result: dict[str, Any], outcome: dict[str, int]
    ) -> None:
        """Fold one collector result into the worker counters."""
        status = result.get("status")
        if status == "collected":
            rows = _counted_int(result.get("rows_removed"))
            logical_bytes = _counted_int(result.get("logical_bytes_removed"))
            self._state.collected += 1
            self._state.rows_removed += rows
            self._state.logical_bytes_removed += logical_bytes
            outcome["collected"] += 1
        elif status == "already_collected":
            self._state.already_collected += 1
            outcome["already_collected"] += 1
        elif status == "blocked":
            self._state.blocked += 1
            outcome["blocked"] += 1
        elif status == "empty":
            self._state.empty += 1
            outcome["empty"] += 1
        else:
            # ``dry_run`` can never occur (the worker always passes
            # dry_run=False); any other unexpected status is fail-closed
            # bookkeeping for an event the collector did not decide.
            self._state.failed += 1
            outcome["failed"] += 1
            self._record_worker_error("unexpected_status", event_id)
            _logger.warning(
                "Content collection returned unexpected status %r for %s",
                status,
                event_id,
            )

    # -- Activation epoch gate ----------------------------------------------

    async def _ensure_activation_epoch(self, now: datetime) -> RetentionEpoch | None:
        """Gate every sweep on a fresh durable activation epoch.

        Opens the first epoch with ``runtime-first-start`` provenance when
        none exists; each newly started LIVE worker also opens a new epoch
        when the predecessor is still fresh, so a short disabled period is
        never silently swept. Returns ``None`` — and leaves collection
        and the heartbeat untouched — when the newest epoch's activity
        observation is stale, entering the inspectable
        ``awaiting_activation`` state until a fresh operator-opened epoch
        appears.
        """
        epoch = await self._storage.latest_retention_epoch()
        if not self._startup_epoch_checked and epoch is not None:
            if not self._epoch_is_stale(epoch, now):
                # The last heartbeat may belong to a previous process that
                # briefly ran with retention disabled. Never silently sweep
                # that gap merely because the timestamp is still fresh.
                epoch = await self._storage.open_retention_epoch(
                    activated_by=_FIRST_ACTIVATION_ACTIVATOR, now=now
                )
                _logger.info(
                    "Content collection opened a new startup epoch %d "
                    "to exclude an unverified retention-policy gap",
                    epoch.epoch,
                )
            # A failed epoch write must NOT flip this flag. Otherwise the
            # next cycle could collect under a previous, still-fresh epoch.
            self._startup_epoch_checked = True
        if epoch is None:
            epoch = await self._storage.open_retention_epoch(
                activated_by=_FIRST_ACTIVATION_ACTIVATOR, now=now
            )
            self._startup_epoch_checked = True
            _logger.info(
                "Content collection opened the first retention epoch %d "
                "(activated_by=%s, watermark=%s)",
                epoch.epoch,
                epoch.activated_by,
                epoch.admission_watermark,
            )
            self._state.epoch = epoch.epoch
            self._state.awaiting_activation = False
            return epoch
        self._state.epoch = epoch.epoch
        if self._epoch_is_stale(epoch, now):
            if not self._state.awaiting_activation:
                self._state.awaiting_activation = True
                _logger.warning(
                    "Content collection awaiting a fresh activation epoch: "
                    "epoch %d's last activity observation is older than %.0fs",
                    epoch.epoch,
                    self._epoch_staleness_threshold(),
                )
                self._emit(
                    "collection_epoch_awaited",
                    {
                        "epoch": epoch.epoch,
                        "staleness_threshold_seconds": (
                            self._epoch_staleness_threshold()
                        ),
                    },
                )
            return None
        self._state.awaiting_activation = False
        return epoch

    def _epoch_staleness_threshold(self) -> float:
        return float(retention_epoch_grace_seconds(self._policy.interval_seconds))

    def _epoch_is_stale(self, epoch: RetentionEpoch, now: datetime) -> bool:
        reference = epoch.last_observed_active_at or epoch.activated_at
        observed = _parse_aware_iso(reference)
        if observed is None:
            # Unparseable durable observation: fail closed (idle, collect
            # nothing) rather than guess the boundary is fresh.
            return True
        return (now - observed).total_seconds() > self._epoch_staleness_threshold()
