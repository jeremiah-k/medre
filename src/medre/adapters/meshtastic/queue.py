"""Meshtastic outbound queue with pacing scaffolding.

:class:`MeshtasticOutboundQueue` manages outbound message pacing for the
Meshtastic adapter.  It owns the delay between messages and provides
enqueue / dequeue / process_one operations.

**Tranche 1 scope**: the queue is scaffolding.  ``process_one`` is a
no-op that returns ``None`` (the actual production send loop is deferred).
The pipeline must NOT perform Meshtastic-specific sleeping; the queue
owns pacing.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from medre.adapters.base import AdapterDeliveryResult
from medre.core.rendering.renderer import RenderingResult


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
        self, client: Any = None
    ) -> AdapterDeliveryResult | None:
        """Process one queued item (tranche 1 scaffolding: no-op).

        In tranche 1 this method dequeues one item but does not perform
        any real send.  It returns ``None`` to indicate no delivery was
        performed.

        Parameters
        ----------
        client:
            Optional Meshtastic client interface (unused in tranche 1).

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in tranche 1 (scaffolded).
        """
        item = await self.dequeue()
        if item is None:
            return None
        # TODO(tranche-N): scaffolded — no real send, no pacing sleep.
        # Future tranches will:
        #   1. Apply pacing delay (asyncio.sleep(self._delay))
        #   2. Call client.sendText(...)
        #   3. Return AdapterDeliveryResult with packet ID
        return None

    @property
    def pending_count(self) -> int:
        """Number of items currently in the queue."""
        return len(self._queue)
