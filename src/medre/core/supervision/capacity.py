"""Semaphore-based capacity controller for in-flight delivery and replay limits.

Controls concurrency for three independent work streams:

* **Delivery** — limits the number of concurrent in-flight adapter
  deliveries via :meth:`acquire_delivery` / :meth:`release_delivery`.
* **Replay** — limits the number of concurrent in-flight replay events
  via :meth:`acquire_replay` / :meth:`release_replay`.
* **Inbound admission** — limits the number of concurrent inbound-event
  admission crossings via :meth:`acquire_inbound` / :meth:`release_inbound`.
  Radio transports schedule one coroutine per SDK callback with no
  transport-level bound; the admission gate bounds how many of those
  coroutines may commit ingress concurrently, and rejects arrivals that
  wait past the admission timeout instead of growing without limit.

The controller is **not** a rate limiter — it bounds the number of
concurrently executing operations, not the rate at which new ones are
admitted.  When a slot cannot be acquired within the configured timeout
the caller receives ``False`` and should treat the operation as rejected.

Public symbols
--------------
* :class:`CapacityController` — semaphore-based capacity manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

__all__ = ["CapacityController", "InboundAdmissionRejected"]

_logger = logging.getLogger(__name__)


class InboundAdmissionRejected(RuntimeError):
    """An inbound event was rejected at the admission gate.

    Raised by the runtime's inbound publish seam when an arrival waited
    past the admission timeout or arrived after the controller stopped
    accepting work.  The controller counts every rejection; adapters
    catch and log it as counted ingress loss — never a silent drop.
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
    """Semaphore-based capacity controller bounding in-flight work.

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
        self._inbound_sem = asyncio.Semaphore(limits.max_inflight_inbound_admissions)
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

        # Wait diagnostics: per-arrival wait-start timestamps. The oldest
        # pending timestamp is the reported oldest wait; entries leave the
        # list when their arrival acquires, times out, or is rejected, so
        # the reported age never references a departed arrival.
        self._inbound_wait_starts: list[float] = []

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

    async def acquire_inbound(self) -> bool:
        """Acquire an inbound admission slot, returning ``True`` on success.

        Returns ``False`` when inbound acceptance has closed, when the
        arrival waited past the inbound admission timeout, or when the
        wait queue is already full.  The wait queue is bounded at the
        admission limit itself: at most ``limit`` admissions may execute
        concurrently and at most ``limit`` further arrivals may queue, so
        a burst cannot accumulate unbounded pending coroutines.  Each
        queued arrival records a wait-start timestamp; the oldest pending
        timestamp is the reported oldest wait.
        """
        started = self._loop_time()
        async with self._lock:
            if not self._inbound_accepting:
                self._inbound_rejections += 1
                return False
            if len(self._inbound_wait_starts) >= self._inbound_limit:
                # Wait-queue overflow: reject immediately rather than
                # schedule another coroutine that would only wait.
                self._inbound_rejections += 1
                return False
            self._inbound_wait_starts.append(started)
        try:
            try:
                await asyncio.wait_for(
                    self._inbound_sem.acquire(),
                    timeout=self._inbound_timeout,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    self._inbound_timeouts += 1
                return False
            async with self._lock:
                if not self._inbound_accepting:
                    self._inbound_sem.release()
                    self._inbound_rejections += 1
                    return False
                self._inbound_current += 1
            return True
        finally:
            async with self._lock:
                self._inbound_wait_starts.remove(started)

    async def release_inbound(self) -> None:
        """Release a previously acquired inbound admission slot."""
        self._inbound_sem.release()
        async with self._lock:
            self._inbound_current = max(0, self._inbound_current - 1)

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
        if self._inbound_wait_starts:
            oldest_wait = round(
                max(0.0, self._loop_time() - min(self._inbound_wait_starts)), 6
            )
        return {
            "accepting_work": self._accepting_work,
            "delivery_current": self._delivery_current,
            "delivery_limit": self._delivery_limit,
            "delivery_rejections": self._delivery_rejections,
            "delivery_timeouts": self._delivery_timeouts,
            "inbound_accepting": self._inbound_accepting,
            "inbound_admission_oldest_wait_seconds": oldest_wait,
            "inbound_admission_waiting": len(self._inbound_wait_starts),
            "inbound_current": self._inbound_current,
            "inbound_limit": self._inbound_limit,
            "inbound_rejections": self._inbound_rejections,
            "inbound_timeouts": self._inbound_timeouts,
            "replay_current": self._replay_current,
            "replay_limit": self._replay_limit,
            "replay_rejections": self._replay_rejections,
            "replay_timeouts": self._replay_timeouts,
        }
