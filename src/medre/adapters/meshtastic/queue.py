"""Meshtastic outbound queue with pacing and send support.

:class:`MeshtasticOutboundQueue` manages outbound message pacing for the
Meshtastic adapter.  It owns the delay between messages and provides
enqueue / dequeue / process_one operations.

The queue owns pacing — the pipeline and renderer must NOT perform
Meshtastic-specific sleeping.

``process_one`` supports two modes:

* **No ``send_fn``**: dequeues one item and returns ``None`` (scaffold mode).
* **With ``send_fn``**: dequeues, applies pacing delay, calls the async
  *send_fn*, and returns an :class:`AdapterDeliveryResult` with the
  native packet ID if available.

Failure semantics
-----------------
When *send_fn* raises an exception during ``process_one``, the dequeued
item is **permanently dropped** — it is NOT requeued or retried.  The
queue increments ``total_failed`` and re-raises the exception to the
caller.  Production-grade retry / requeue logic is explicitly deferred
to future work.  This is a scaffold design choice, not a bug.

Queue bounds
------------
The internal deque is bounded by ``max_queue_size`` (default 1024).
When the queue is full, the **oldest** item is silently dropped to make
room for the new enqueue.  This prevents unbounded memory growth in
long-duration runs where outbound throughput exceeds send capacity.
The ``total_dropped`` counter tracks how many items were shed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from types import MappingProxyType
from typing import Any, Awaitable, Callable

from medre.core.contracts.adapter import AdapterDeliveryResult

_logger = logging.getLogger(__name__)

# Default maximum queue size.  When full, oldest items are dropped.
_DEFAULT_MAX_QUEUE_SIZE: int = 1024


class MeshtasticOutboundQueue:
    """Outbound queue with pacing for Meshtastic messages.

    Parameters
    ----------
    delay_between_messages:
        Minimum delay in seconds between consecutive outbound messages.
    max_queue_size:
        Maximum number of queued items.  When exceeded, the oldest item
        is silently dropped.  ``None`` means unbounded (not recommended
        for production).
    """

    def __init__(
        self,
        delay_between_messages: float = 0.5,
        max_queue_size: int | None = _DEFAULT_MAX_QUEUE_SIZE,
    ) -> None:
        self._delay = delay_between_messages
        self._max_queue_size = max_queue_size
        self._queue: deque[dict[str, Any]] = deque(
            maxlen=max_queue_size,
        )
        self._last_send_time: float = 0.0
        self._total_sent: int = 0
        self._total_failed: int = 0
        self._total_dropped: int = 0

    @property
    def delay_between_messages(self) -> float:
        """Minimum seconds between consecutive outbound messages."""
        return self._delay

    @property
    def max_queue_size(self) -> int | None:
        """Maximum queue capacity, or ``None`` if unbounded."""
        return self._max_queue_size

    @property
    def queue_depth(self) -> int:
        """Current number of items waiting in the queue."""
        return len(self._queue)

    @property
    def total_dropped(self) -> int:
        """Number of items dropped due to queue overflow."""
        return self._total_dropped

    async def enqueue(self, payload: dict[str, Any], channel_index: int) -> None:
        """Enqueue a payload for delivery.

        When the queue is at capacity, the oldest item is silently
        dropped and ``total_dropped`` is incremented.

        Parameters
        ----------
        payload:
            The rendered payload dict to deliver.
        channel_index:
            The target radio channel index.
        """
        # Detect overflow: deque with maxlen silently drops from the
        # left (oldest) when append would exceed capacity.
        if (
            self._max_queue_size is not None
            and len(self._queue) >= self._max_queue_size
        ):
            self._total_dropped += 1
            _logger.warning(
                "MeshtasticOutboundQueue full (%d items); dropping oldest",
                self._max_queue_size,
            )
        self._queue.append(
            {
                "payload": payload,
                "channel_index": channel_index,
            }
        )

    async def dequeue(self) -> dict[str, Any] | None:
        """Dequeue the next payload, or ``None`` if the queue is empty.

        Returns
        -------
        dict | None
            The next queued item with ``payload`` and ``channel_index``,
            or ``None`` when empty.
        """
        if not self._queue:
            return None
        return self._queue.popleft()

    async def process_one(
        self,
        send_fn: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> AdapterDeliveryResult | None:
        """Process one queued item.

        When *send_fn* is ``None`` (scaffold mode), this method dequeues
        one item but does not perform any real send or pacing sleep.
        It returns ``None`` to indicate no delivery was performed.

        When *send_fn* is provided, the method dequeues one item, applies
        the configured pacing delay, calls *send_fn* with the dequeued
        item, and returns an :class:`AdapterDeliveryResult` populated
        with the native packet ID (if the send result exposes one).

        Parameters
        ----------
        send_fn:
            Optional async callable that accepts the dequeued item dict
            (with ``payload`` and ``channel_index`` keys) and returns a
            send result.  The result may be ``None``, a dict with an
            ``"id"`` or ``"packet_id"`` key, or an object with an ``id``
            attribute.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in scaffold mode or when the queue is empty.
            An :class:`AdapterDeliveryResult` when a send was attempted.
        """
        item = await self.dequeue()
        if item is None:
            return None

        if send_fn is None:
            # Scaffold mode: no send, no pacing sleep.
            return None

        # Apply pacing delay based on time since last send.
        now = time.monotonic()
        elapsed_since_last = now - self._last_send_time
        remaining = self._delay - elapsed_since_last
        if remaining > 0:
            await asyncio.sleep(remaining)

        try:
            send_result = await send_fn(item)
        except Exception:
            self._total_failed += 1
            raise

        self._last_send_time = time.monotonic()
        self._total_sent += 1

        # Extract native packet ID from the send result.
        native_id = _extract_packet_id(send_result)
        channel_index = item.get("channel_index", 0)

        # Build packet snapshot metadata when result is packet-like.
        snapshot = _packet_snapshot(send_result)
        metadata = MappingProxyType(snapshot) if snapshot else MappingProxyType({})

        return AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(channel_index),
            metadata=metadata,
        )

    @property
    def pending_count(self) -> int:
        """Number of items currently in the queue."""
        return len(self._queue)

    @property
    def total_sent(self) -> int:
        """Total number of items successfully sent."""
        return self._total_sent

    @property
    def total_failed(self) -> int:
        """Total number of send failures."""
        return self._total_failed

    @property
    def queue_health(self) -> dict[str, Any]:
        """Snapshot of the queue's operational state.

        Returns
        -------
        dict
            Keys: ``pending_count``, ``total_sent``, ``total_failed``,
            ``delay_between_messages``, ``last_send_time``.
        """
        return {
            "pending_count": self.pending_count,
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "delay_between_messages": self._delay,
            "last_send_time": self._last_send_time,
        }


def _extract_packet_id(result: Any) -> str | None:
    """Extract a native packet ID from a send result.

    The Meshtastic ``sendText`` API returns the sent packet object with
    a populated ``id`` field.  The fake client returns a dict with
    ``"packet_id"``.  This helper handles both cases.

    Parameters
    ----------
    result:
        The value returned by the send function.

    Returns
    -------
    str | None
        The extracted ID as a string, or ``None``.
    """
    if result is None:
        return None
    # Dict with "packet_id" key (fake client pattern).
    if isinstance(result, dict):
        pid = result.get("packet_id") or result.get("id")
        return str(pid) if pid is not None else None
    # Object with .id attribute (real meshtastic client pattern).
    pid = getattr(result, "id", None)
    return str(pid) if pid is not None else None


def _packet_snapshot(result: Any) -> dict[str, object]:
    """Extract a safe metadata snapshot from a packet-like send result.

    Returns a dict with available keys (``id``, ``packet_id``,
    ``channel``, ``reply_id``, ``to``) when the result looks like a
    packet.  Returns an empty dict for ``None`` or non-packet results.

    Parameters
    ----------
    result:
        The value returned by the send function.

    Returns
    -------
    dict
        Safe key-value pairs suitable for
        :class:`AdapterDeliveryResult` metadata.
    """
    if result is None:
        return {}
    snapshot: dict[str, object] = {}
    if isinstance(result, dict):
        for key in ("id", "packet_id", "channel", "reply_id", "to"):
            val = result.get(key)
            if val is not None:
                snapshot[key] = val
    else:
        for attr in ("id", "channel", "reply_id", "to"):
            val = getattr(result, attr, None)
            if val is not None:
                snapshot[attr] = val
    return snapshot
