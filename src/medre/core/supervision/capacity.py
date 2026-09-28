"""Capacity controller for delivery, replay, and inbound-admission limits.

Controls concurrency for three independent work streams:

* **Delivery** — limits the number of concurrent in-flight adapter
  deliveries via :meth:`acquire_delivery` / :meth:`release_delivery`.
* **Replay** — limits the number of concurrent in-flight replay events
  via :meth:`acquire_replay` / :meth:`release_replay`.
* **Inbound admission** — limits the number of concurrent inbound-event
  admission crossings via :meth:`acquire_inbound` / :meth:`release_inbound`.
  Active slots are global and work-conserving; saturated arrivals queue in
  source-aware fair shares and are granted round-robin so one callback-heavy
  adapter cannot monopolise the bounded overload cushion.

The controller is **not** a rate limiter — it bounds the number of
concurrently executing operations, not the rate at which new ones are
admitted.  When a slot cannot be acquired within the configured timeout
the caller receives ``False`` and should treat the operation as rejected.

Public symbols
--------------
* :class:`CapacityController` — bounded capacity and admission manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Protocol

__all__ = ["CapacityController", "InboundAdmissionRejected"]

_logger = logging.getLogger(__name__)

_ANONYMOUS_INBOUND_SOURCE = "<anonymous>"


@dataclass
class _InboundWaiter:
    """One queued inbound-admission request."""

    source_id: str
    started: float
    future: asyncio.Future[bool]
    granted: bool = False


class InboundAdmissionRejected(RuntimeError):
    """An inbound event was rejected at the admission gate.

    Raised by the runtime's inbound publish seam when an arrival waited
    past the admission timeout, overflowed its global/source fair-share
    queue, or arrived after the controller stopped accepting work.  The
    controller attributes the pressure to the adapter source.  Ordinary
    publish callbacks treat it as counted ingress loss; cursor-aware
    durable admission maps it to :class:`DurableIngressDeferredError` so
    the native event remains retryable.
    """


class _Limits(Protocol):
    """Structural interface for capacity configuration.

    Satisfied by :class:`medre.config.model.RuntimeLimits` without
    importing it — keeps core free of config dependencies.

    Attributes are declared read-only (via ``@property``) so that
    frozen dataclasses like ``RuntimeLimits`` satisfy the protocol.
    """

    @property
    def max_inflight_deliveries(self) -> int: ...

    @property
    def max_inflight_replay_events(self) -> int: ...

    @property
    def delivery_acquire_timeout_seconds(self) -> float: ...

    @property
    def max_inflight_inbound_admissions(self) -> int: ...

    @property
    def inbound_admission_timeout_seconds(self) -> float: ...


class CapacityController:
    """Capacity controller bounding in-flight work and queued admission.

    Parameters
    ----------
    limits:
        Object with ``max_inflight_deliveries``,
        ``max_inflight_replay_events``, and
        ``delivery_acquire_timeout_seconds`` attributes.
        :class:`~medre.config.model.RuntimeLimits` satisfies this
        protocol without a direct import dependency.
    """

    def __init__(self, limits: _Limits) -> None:
        self._delivery_sem = asyncio.Semaphore(limits.max_inflight_deliveries)
        self._replay_sem = asyncio.Semaphore(limits.max_inflight_replay_events)
        self._delivery_limit = limits.max_inflight_deliveries
        self._replay_limit = limits.max_inflight_replay_events
        self._inbound_limit = limits.max_inflight_inbound_admissions
        self._delivery_timeout = limits.delivery_acquire_timeout_seconds
        self._inbound_timeout = limits.inbound_admission_timeout_seconds

        # Counters — protected by ``_lock`` for consistent reads.
        self._delivery_current: int = 0
        self._replay_current: int = 0
        self._inbound_current: int = 0
        self._delivery_rejections: int = 0
        self._replay_rejections: int = 0
        self._inbound_rejections: int = 0
        self._delivery_timeouts: int = 0
        self._replay_timeouts: int = 0
        self._inbound_timeouts: int = 0

        # Inbound admission is work-conserving for active slots but fair for
        # queued overload.  A single source may use every execution slot when
        # uncontended; once saturated, pending arrivals are partitioned by
        # configured adapter source and granted round-robin.  This prevents a
        # burst from one callback-heavy adapter from consuming the entire
        # bounded wait queue before another adapter can enqueue.
        self._inbound_sources: tuple[str, ...] = ()
        self._inbound_sources_configured: bool = False
        self._inbound_waiters: dict[str, deque[_InboundWaiter]] = {}
        self._inbound_rr_sources: deque[str] = deque()
        self._inbound_waiting_total: int = 0
        self._inbound_current_by_source: dict[str, int] = {}
        self._inbound_rejections_by_source: dict[str, int] = {}
        self._inbound_timeouts_by_source: dict[str, int] = {}

        # Delivery/replay acceptance and inbound acceptance close at
        # different points of shutdown: delivery stops taking work before
        # adapters stop draining, while inbound stays admissible through
        # adapter teardown so late callbacks can persist rows for the next
        # runtime generation.
        self._accepting_work: bool = True
        self._inbound_accepting: bool = True
        self._lock = asyncio.Lock()

    # -- Properties -----------------------------------------------------------

    @property
    def delivery_current(self) -> int:
        """Number of currently in-flight deliveries."""
        return self._delivery_current

    @property
    def delivery_limit(self) -> int:
        """Maximum concurrent in-flight deliveries."""
        return self._delivery_limit

    @property
    def replay_current(self) -> int:
        """Number of currently in-flight replay events."""
        return self._replay_current

    @property
    def replay_limit(self) -> int:
        """Maximum concurrent in-flight replay events."""
        return self._replay_limit

    @property
    def inbound_current(self) -> int:
        """Number of inbound events currently crossing admission."""
        return self._inbound_current

    @property
    def inbound_limit(self) -> int:
        """Maximum concurrent inbound admissions."""
        return self._inbound_limit

    @property
    def accepting_work(self) -> bool:
        """Whether the controller is still accepting delivery/replay work."""
        return self._accepting_work

    @property
    def inbound_accepting(self) -> bool:
        """Whether the controller is still accepting inbound admissions."""
        return self._inbound_accepting

    # -- Delivery acquire / release -------------------------------------------

    async def acquire_delivery(self) -> bool:
        """Acquire a delivery slot, returning ``True`` on success.

        Returns ``False`` when the controller has stopped accepting work
        or the acquire times out.
        """
        if not self._accepting_work:
            async with self._lock:
                self._delivery_rejections += 1
            return False

        try:
            await asyncio.wait_for(
                self._delivery_sem.acquire(),
                timeout=self._delivery_timeout,
            )
            # Re-check accepting_work after semaphore wait — it may have
            # changed to False while we were blocked on the semaphore.
            async with self._lock:
                if not self._accepting_work:
                    self._delivery_sem.release()
                    self._delivery_rejections += 1
                    return False
                self._delivery_current += 1
            return True
        except asyncio.TimeoutError:
            async with self._lock:
                self._delivery_timeouts += 1
            return False

    async def release_delivery(self) -> None:
        """Release a previously acquired delivery slot."""
        self._delivery_sem.release()
        async with self._lock:
            self._delivery_current = max(0, self._delivery_current - 1)

    # -- Replay acquire / release ---------------------------------------------

    async def acquire_replay(self) -> bool:
        """Acquire a replay slot, returning ``True`` on success.

        Returns ``False`` when the controller has stopped accepting work
        or the acquire times out.
        """
        if not self._accepting_work:
            async with self._lock:
                self._replay_rejections += 1
            return False

        try:
            await asyncio.wait_for(
                self._replay_sem.acquire(),
                timeout=self._delivery_timeout,
            )
            # Re-check accepting_work after semaphore wait — it may have
            # changed to False while we were blocked on the semaphore.
            async with self._lock:
                if not self._accepting_work:
                    self._replay_sem.release()
                    self._replay_rejections += 1
                    return False
                self._replay_current += 1
            return True
        except asyncio.TimeoutError:
            async with self._lock:
                self._replay_timeouts += 1
            return False

    async def release_replay(self) -> None:
        """Release a previously acquired replay slot."""
        self._replay_sem.release()
        async with self._lock:
            self._replay_current = max(0, self._replay_current - 1)

    # -- Inbound admission acquire / release ----------------------------------

    def configure_inbound_sources(self, source_ids: Iterable[str]) -> None:
        """Declare the adapter sources that share inbound admission capacity.

        The runtime calls this once after adapter construction and before any
        adapter starts.  The configured source count determines each adapter's
        fair share of the *pending* wait queue.  Active execution slots remain
        global and work-conserving: an uncontended adapter may still use the
        entire admission concurrency limit.

        Reconfiguration after admission work has started is rejected because
        changing queue shares underneath live waiters would make overload
        semantics non-deterministic.
        """
        normalized = tuple(
            sorted(
                {
                    source_id.strip()
                    for source_id in source_ids
                    if isinstance(source_id, str) and source_id.strip()
                }
            )
        )
        if self._inbound_sources_configured:
            if normalized == self._inbound_sources:
                return
            raise RuntimeError(
                "inbound admission sources are fixed for the runtime generation"
            )
        if self._inbound_current or self._inbound_waiting_total:
            raise RuntimeError(
                "cannot configure inbound admission sources after work has started"
            )
        self._inbound_sources = normalized
        self._inbound_sources_configured = True
        for source_id in normalized:
            self._ensure_inbound_source(source_id)

    async def acquire_inbound(self, source_id: str | None = None) -> bool:
        """Acquire an inbound admission slot, returning ``True`` on success.

        Active slots are runtime-global and work-conserving.  Under saturation,
        pending arrivals are bounded twice: the total wait queue may contain at
        most ``inbound_limit`` arrivals, and each configured source may consume
        only its equal fair share of that queue.  Queued arrivals are granted
        round-robin by source, preventing one callback-heavy adapter from
        monopolising the overload cushion.

        Returns ``False`` when inbound acceptance has closed, the global or
        source-local wait queue is full, or the arrival waits past the inbound
        admission timeout.
        """
        source = self._normalize_inbound_source(source_id)
        started = self._loop_time()
        loop = asyncio.get_running_loop()
        waiter = _InboundWaiter(
            source_id=source,
            started=started,
            future=loop.create_future(),
        )

        async with self._lock:
            self._ensure_inbound_source(source)
            if not self._inbound_accepting:
                self._record_inbound_rejection_locked(source)
                return False

            # Work-conserving fast path.  Do not bypass an existing queue:
            # once contention exists, queued source ordering is authoritative.
            if (
                self._inbound_current < self._inbound_limit
                and self._inbound_waiting_total == 0
            ):
                self._record_inbound_grant_locked(source)
                return True

            source_queue = self._inbound_waiters.get(source)
            source_waiting = len(source_queue) if source_queue is not None else 0
            if self._inbound_waiting_total >= self._inbound_limit or (
                source_waiting >= self._inbound_source_wait_limit(source)
            ):
                self._record_inbound_rejection_locked(source)
                return False

            if source_queue is None:
                source_queue = deque()
                self._inbound_waiters[source] = source_queue
            was_empty = not source_queue
            source_queue.append(waiter)
            self._inbound_waiting_total += 1
            if was_empty:
                self._inbound_rr_sources.append(source)
            self._grant_inbound_waiters_locked()
            if waiter.granted:
                return True

        try:
            return await asyncio.wait_for(
                asyncio.shield(waiter.future),
                timeout=self._inbound_timeout,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                # A grant may have raced the timeout while this task waited
                # for the lock.  In that case the caller owns the slot and
                # must observe success so it can release it normally.
                if waiter.granted:
                    return True
                if self._remove_inbound_waiter_locked(waiter):
                    self._inbound_timeouts += 1
                    self._inbound_timeouts_by_source[source] += 1
            return False
        except asyncio.CancelledError:
            async with self._lock:
                if waiter.granted:
                    # Cancellation after grant but before the caller can
                    # return would otherwise leak a slot forever.
                    waiter.granted = False
                    self._release_inbound_slot_locked(source)
                    self._grant_inbound_waiters_locked()
                else:
                    self._remove_inbound_waiter_locked(waiter)
            raise

    async def release_inbound(self, source_id: str | None = None) -> None:
        """Release a previously acquired inbound admission slot."""
        source = self._normalize_inbound_source(source_id)
        async with self._lock:
            self._ensure_inbound_source(source)
            if self._inbound_current_by_source[source] <= 0:
                _logger.warning(
                    "CapacityController: unmatched inbound release for source %s",
                    source,
                )
                return
            self._release_inbound_slot_locked(source)
            self._grant_inbound_waiters_locked()

    @staticmethod
    def _normalize_inbound_source(source_id: str | None) -> str:
        if isinstance(source_id, str) and source_id.strip():
            return source_id.strip()
        return _ANONYMOUS_INBOUND_SOURCE

    def _ensure_inbound_source(self, source_id: str) -> None:
        self._inbound_current_by_source.setdefault(source_id, 0)
        self._inbound_rejections_by_source.setdefault(source_id, 0)
        self._inbound_timeouts_by_source.setdefault(source_id, 0)

    def _inbound_source_wait_limit(self, source_id: str) -> int:
        configured = len(self._inbound_sources)
        if configured:
            source_count = configured + (0 if source_id in self._inbound_sources else 1)
        else:
            source_count = max(1, len(self._inbound_current_by_source))
        return max(1, (self._inbound_limit + source_count - 1) // source_count)

    def _record_inbound_grant_locked(self, source_id: str) -> None:
        self._inbound_current += 1
        self._inbound_current_by_source[source_id] += 1

    def _record_inbound_rejection_locked(self, source_id: str) -> None:
        self._inbound_rejections += 1
        self._inbound_rejections_by_source[source_id] += 1

    def _release_inbound_slot_locked(self, source_id: str) -> None:
        self._inbound_current = max(0, self._inbound_current - 1)
        self._inbound_current_by_source[source_id] = max(
            0, self._inbound_current_by_source[source_id] - 1
        )

    def _remove_inbound_waiter_locked(self, waiter: _InboundWaiter) -> bool:
        queue = self._inbound_waiters.get(waiter.source_id)
        if queue is None:
            return False
        try:
            queue.remove(waiter)
        except ValueError:
            return False
        self._inbound_waiting_total = max(0, self._inbound_waiting_total - 1)
        if not queue:
            self._inbound_waiters.pop(waiter.source_id, None)
            self._inbound_rr_sources = deque(
                source
                for source in self._inbound_rr_sources
                if source != waiter.source_id
            )
        return True

    def _grant_inbound_waiters_locked(self) -> None:
        """Grant queued arrivals round-robin while capacity is available."""
        while (
            self._inbound_current < self._inbound_limit
            and self._inbound_waiting_total > 0
            and self._inbound_rr_sources
        ):
            source = self._inbound_rr_sources.popleft()
            queue = self._inbound_waiters.get(source)
            if not queue:
                continue
            waiter = queue.popleft()
            self._inbound_waiting_total = max(0, self._inbound_waiting_total - 1)
            if queue:
                self._inbound_rr_sources.append(source)
            else:
                self._inbound_waiters.pop(source, None)

            if not self._inbound_accepting:
                self._record_inbound_rejection_locked(source)
                if not waiter.future.done():
                    waiter.future.set_result(False)
                continue

            waiter.granted = True
            self._record_inbound_grant_locked(source)
            if not waiter.future.done():
                waiter.future.set_result(True)

    @staticmethod
    def _loop_time() -> float:
        """Return a monotonic clock readable in any thread or context."""
        return time.monotonic()

    # -- Lifecycle ------------------------------------------------------------

    def stop_accepting(self) -> None:
        """Signal that no new delivery/replay work should be accepted.

        Any subsequent call to :meth:`acquire_delivery` or
        :meth:`acquire_replay` will return ``False`` immediately.
        Inbound acceptance is unaffected; it closes separately via
        :meth:`stop_accepting_inbound` so late adapter callbacks can keep
        crossing durable admission while adapters shut down.
        """
        self._accepting_work = False
        _logger.info("CapacityController: stopped accepting delivery work")

    def stop_accepting_inbound(self) -> None:
        """Signal that no further inbound admissions are accepted.

        Called only after every adapter has stopped, closing the inbound
        seam for the runtime generation. Queued arrivals observe the
        closed acceptance when they wake and are rejected with counted
        evidence.
        """
        self._inbound_accepting = False
        _logger.info("CapacityController: stopped accepting inbound work")

    # -- Diagnostics ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a deterministic, JSON-safe snapshot of capacity counters.

        Keys are alphabetically sorted and contain no secrets or raw
        SDK objects.
        """
        oldest_wait = None
        waiters = [
            waiter for queue in self._inbound_waiters.values() for waiter in queue
        ]
        if waiters:
            oldest_wait = round(
                max(0.0, self._loop_time() - min(waiter.started for waiter in waiters)),
                6,
            )
        sources: dict[str, dict[str, int | float | None]] = {}
        source_ids = sorted(
            set(self._inbound_current_by_source)
            | set(self._inbound_rejections_by_source)
            | set(self._inbound_timeouts_by_source)
            | set(self._inbound_waiters)
            | set(self._inbound_sources)
        )
        for source_id in source_ids:
            queue = self._inbound_waiters.get(source_id, ())
            source_oldest_wait = None
            if queue:
                source_oldest_wait = round(
                    max(
                        0.0,
                        self._loop_time() - min(waiter.started for waiter in queue),
                    ),
                    6,
                )
            sources[source_id] = {
                "current": self._inbound_current_by_source.get(source_id, 0),
                "oldest_wait_seconds": source_oldest_wait,
                "rejections": self._inbound_rejections_by_source.get(source_id, 0),
                "timeouts": self._inbound_timeouts_by_source.get(source_id, 0),
                "wait_limit": self._inbound_source_wait_limit(source_id),
                "waiting": len(queue),
            }
        return {
            "accepting_work": self._accepting_work,
            "delivery_current": self._delivery_current,
            "delivery_limit": self._delivery_limit,
            "delivery_rejections": self._delivery_rejections,
            "delivery_timeouts": self._delivery_timeouts,
            "inbound_accepting": self._inbound_accepting,
            "inbound_admission_oldest_wait_seconds": oldest_wait,
            "inbound_admission_waiting": self._inbound_waiting_total,
            "inbound_current": self._inbound_current,
            "inbound_limit": self._inbound_limit,
            "inbound_rejections": self._inbound_rejections,
            "inbound_sources": sources,
            "inbound_timeouts": self._inbound_timeouts,
            "replay_current": self._replay_current,
            "replay_limit": self._replay_limit,
            "replay_rejections": self._replay_rejections,
            "replay_timeouts": self._replay_timeouts,
        }
