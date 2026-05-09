"""Async publish/subscribe event bus with middleware chain.

This module provides the central event distribution mechanism for the
medre:

* :class:`EventMiddleware` – protocol for intercepting events before
  subscribers receive them.
* :class:`Subscription` – handle returned by :meth:`EventBus.subscribe`
  that supports explicit unsubscription.
* :class:`EventBus` – async pub/sub bus with type-prefix matching and
  an ordered middleware chain.

The bus supports **prefix-based type matching**: subscribing to
``"message"`` matches events of kind ``"message.created"``,
``"message.text"``, ``"message.edited"``, and so on.  The wildcard
``"*"`` matches every event kind.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import insort
from typing import Protocol, runtime_checkable

from medre.core.events.canonical import CanonicalEvent


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Middleware protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventMiddleware(Protocol):
    """Protocol for event-processing middleware.

    Middleware instances are executed in priority order (lowest first)
    during :meth:`EventBus.publish`.  Each middleware receives the event
    and may:

    * Return the event unchanged (pass-through).
    * Return a **modified** copy of the event.
    * Return ``None`` to **drop** the event (subscribers will not be
      notified).
    """

    async def process(self, event: CanonicalEvent) -> CanonicalEvent | None:
        """Process *event* and return it, a modified copy, or ``None``.

        Parameters
        ----------
        event:
            The canonical event being published.

        Returns
        -------
        CanonicalEvent | None
            The (possibly modified) event to continue propagation, or
            ``None`` to silently drop the event.
        """
        ...


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


class Subscription:
    """Opaque handle returned by :meth:`EventBus.subscribe`.

    Stores the subscription metadata and supports explicit removal via
    :meth:`unsubscribe`.

    Attributes
    ----------
    event_type:
        The event type prefix this subscription matches against.
    handler:
        The async callable registered for this subscription.
    """

    __slots__ = ("event_type", "handler", "_bus", "_active")

    def __init__(
        self,
        event_type: str,
        handler: object,
        bus: EventBus,
    ) -> None:
        self.event_type: str = event_type
        self.handler: object = handler
        self._bus: EventBus = bus
        self._active: bool = True

    async def unsubscribe(self) -> None:
        """Remove this subscription from the event bus.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._active:
            await self._bus.unsubscribe(self)
            self._active = False


# ---------------------------------------------------------------------------
# Internal: middleware wrapper preserving insertion order
# ---------------------------------------------------------------------------


class _MiddlewareEntry:
    """Internal wrapper that pairs a middleware with its priority."""

    __slots__ = ("priority", "counter", "middleware")

    _counter: int = 0

    def __init__(self, middleware: EventMiddleware, priority: int) -> None:
        _MiddlewareEntry._counter += 1
        self.priority: int = priority
        self.counter: int = _MiddlewareEntry._counter
        self.middleware: EventMiddleware = middleware

    def __lt__(self, other: _MiddlewareEntry) -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.counter < other.counter


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Async publish/subscribe bus with middleware chain and prefix matching.

    Subscribers register for an event type string.  At publish time, the
    bus matches the event's ``event_kind`` against every subscription
    using prefix rules:

    * ``"*"`` matches all events.
    * ``"message"`` matches ``"message"`` exactly **and** any kind that
      starts with ``"message."`` (e.g. ``"message.created"``).
    * ``"message.text"`` matches only that exact kind.

    Middleware is executed in **priority order** (lower runs first) before
    any subscriber is notified.  If any middleware returns ``None``, the
    event is dropped and no subscribers are invoked.

    Thread-safety: the bus uses an :class:`asyncio.Lock` to serialise
    concurrent publish calls so that middleware and handler invocations
    are never interleaved.

    Example
    -------
    >>> bus = EventBus()
    >>> sub = bus.subscribe("message", my_handler)
    >>> await bus.publish(event)
    >>> await sub.unsubscribe()
    """

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._middleware: list[_MiddlewareEntry] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    # -- Subscription -------------------------------------------------------

    def subscribe(
        self,
        event_type: str,
        handler: object,
    ) -> Subscription:
        """Subscribe *handler* to events whose kind matches *event_type*.

        Parameters
        ----------
        event_type:
            A prefix string for matching event kinds, or ``"*"`` to match
            all events.
        handler:
            An async callable accepting a single :class:`CanonicalEvent`.

        Returns
        -------
        Subscription
            A handle that can be used to unsubscribe later.
        """
        sub = Subscription(event_type=event_type, handler=handler, bus=self)
        self._subscriptions.append(sub)
        return sub

    async def unsubscribe(self, subscription: Subscription) -> None:
        """Remove *subscription* from the bus.

        Parameters
        ----------
        subscription:
            The subscription handle previously returned by :meth:`subscribe`.
        """
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass  # already removed – no-op

    # -- Middleware ---------------------------------------------------------

    def add_middleware(
        self,
        middleware: EventMiddleware,
        priority: int = 0,
    ) -> None:
        """Register *middleware* at the given *priority*.

        Lower priority values run first.  Middleware registered at the
        same priority are executed in insertion order.

        Parameters
        ----------
        middleware:
            An object satisfying the :class:`EventMiddleware` protocol.
        priority:
            Execution priority (default ``0``).
        """
        entry = _MiddlewareEntry(middleware, priority)
        insort(self._middleware, entry)

    def remove_middleware(self, middleware: EventMiddleware) -> None:
        """Remove *middleware* from the chain.

        No-op if *middleware* is not currently registered.

        Parameters
        ----------
        middleware:
            The middleware instance to remove.
        """
        self._middleware = [
            e for e in self._middleware if e.middleware is not middleware
        ]

    # -- Publish -----------------------------------------------------------

    async def publish(self, event: CanonicalEvent) -> None:
        """Publish *event* through the middleware chain to subscribers.

        The publish flow is:

        1. Acquire an internal lock to serialise concurrent publishes.
        2. Run each middleware in priority order.  If any middleware
           returns ``None``, the event is dropped and no handlers run.
        3. Collect all subscribers whose event type prefix matches the
           event's ``event_kind``.
        4. Invoke matching handlers concurrently via
           :func:`asyncio.gather`.  Handler exceptions are logged but do
           not prevent other handlers from running.

        Parameters
        ----------
        event:
            The canonical event to distribute.
        """
        async with self._lock:
            processed = await self._run_middleware(event)

        if processed is None:
            _logger.debug("Event dropped by middleware")
            return

        await self._dispatch(processed)

    # -- Internals ---------------------------------------------------------

    async def _run_middleware(
        self,
        event: CanonicalEvent,
    ) -> CanonicalEvent | None:
        """Execute the middleware chain on *event*.

        Returns ``None`` if any middleware drops the event.
        """
        current: CanonicalEvent | None = event
        for entry in self._middleware:
            if current is None:
                return None
            try:
                current = await entry.middleware.process(current)
            except Exception:
                _logger.exception(
                    "Middleware %r raised an exception; dropping event",
                    entry.middleware,
                )
                return None
        return current

    async def _dispatch(self, event: CanonicalEvent) -> None:
        """Invoke all matching subscribers for *event*."""
        handlers = [
            sub.handler
            for sub in self._subscriptions
            if self._type_matches(sub.event_type, event.event_kind)
        ]

        if not handlers:
            _logger.debug(
                "No subscribers for event_kind=%r", event.event_kind
            )
            return

        coros = [self._invoke_handler(h, event) for h in handlers]
        await asyncio.gather(*coros)

    @staticmethod
    async def _invoke_handler(
        handler: object,
        event: CanonicalEvent,
    ) -> None:
        """Call a single handler, trapping exceptions."""
        try:
            await handler(event)  # type: ignore[misc]
        except Exception:
            _logger.exception(
                "Subscriber %r raised an exception handling event %s",
                handler,
                event.event_id,
            )

    def status_summary(self) -> dict[str, object]:
        """Return a read-only snapshot of bus state for diagnostics.

        Returns a plain dict safe for JSON serialisation.  Does **not**
        expose handler references or internal lock state.

        Returns
        -------
        dict[str, object]
            Keys: ``subscription_count``, ``middleware_count``.
        """
        return {
            "subscription_count": len(self._subscriptions),
            "middleware_count": len(self._middleware),
        }

    @staticmethod
    def _type_matches(subscription_type: str, event_kind: str) -> bool:
        """Return ``True`` if *subscription_type* matches *event_kind*.

        Matching rules:

        * ``"*"`` matches everything.
        * An exact match (e.g. ``"message.text"`` == ``"message.text"``).
        * A prefix match where the subscription type is the prefix of the
          event kind before a ``"."`` boundary (e.g. ``"message"`` matches
          ``"message.text"``).
        """
        if subscription_type == "*":
            return True
        if subscription_type == event_kind:
            return True
        if event_kind.startswith(subscription_type + "."):
            return True
        return False
