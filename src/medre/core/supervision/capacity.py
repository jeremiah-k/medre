"""Semaphore-based capacity controller for in-flight delivery and replay limits.

Controls concurrency for two independent work streams:

* **Delivery** — limits the number of concurrent in-flight adapter
  deliveries via :meth:`acquire_delivery` / :meth:`release_delivery`.
* **Replay** — limits the number of concurrent in-flight replay events
  via :meth:`acquire_replay` / :meth:`release_replay`.

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
from typing import Protocol

__all__ = ["CapacityController"]

_logger = logging.getLogger(__name__)


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
        self._delivery_limit = limits.max_inflight_deliveries
        self._replay_limit = limits.max_inflight_replay_events
        self._delivery_timeout = limits.delivery_acquire_timeout_seconds

        # Counters — protected by ``_lock`` for consistent reads.
        self._delivery_current: int = 0
        self._replay_current: int = 0
        self._delivery_rejections: int = 0
        self._replay_rejections: int = 0
        self._delivery_timeouts: int = 0
        self._replay_timeouts: int = 0

        self._accepting_work: bool = True
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
    def accepting_work(self) -> bool:
        """Whether the controller is still accepting new work."""
        return self._accepting_work

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

    # -- Lifecycle ------------------------------------------------------------

    def stop_accepting(self) -> None:
        """Signal that no new work should be accepted.

        Any subsequent call to :meth:`acquire_delivery` or
        :meth:`acquire_replay` will return ``False`` immediately.
        """
        self._accepting_work = False
        _logger.info("CapacityController: stopped accepting new work")

    # -- Diagnostics ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a deterministic, JSON-safe snapshot of capacity counters.

        Keys are alphabetically sorted and contain no secrets or raw
        SDK objects.
        """
        return {
            "accepting_work": self._accepting_work,
            "delivery_current": self._delivery_current,
            "delivery_limit": self._delivery_limit,
            "delivery_rejections": self._delivery_rejections,
            "delivery_timeouts": self._delivery_timeouts,
            "replay_current": self._replay_current,
            "replay_limit": self._replay_limit,
            "replay_rejections": self._replay_rejections,
            "replay_timeouts": self._replay_timeouts,
        }
