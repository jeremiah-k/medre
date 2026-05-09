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
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Awaitable

from medre.adapters.base import AdapterDeliveryResult


class MeshtasticOutboundQueue:
    """Outbound queue with pacing for Meshtastic messages.

    Parameters
    ----------
    delay_between_messages:
        Minimum delay in seconds between consecutive outbound messages.
    """

    def __init__(self, delay_between_messages: float = 0.5) -> None:
        self._delay = delay_between_messages
        self._queue: deque[dict[str, Any]] = deque()
        self._last_send_time: float = 0.0
        self._total_sent: int = 0
        self._total_failed: int = 0

    @property
    def delay_between_messages(self) -> float:
        """Minimum seconds between consecutive outbound messages."""
        return self._delay

    async def enqueue(self, payload: dict[str, Any], channel_index: int) -> None:
        """Enqueue a payload for delivery.

        Parameters
        ----------
        payload:
            The rendered payload dict to deliver.
        channel_index:
            The target radio channel index.
        """
        self._queue.append({
            "payload": payload,
            "channel_index": channel_index,
        })

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

        return AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(channel_index),
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
