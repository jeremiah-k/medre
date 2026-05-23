"""Meshtastic outbound queue with pacing and send support.

:class:`MeshtasticOutboundQueue` manages outbound message pacing for the
Meshtastic adapter.  It owns the delay between messages and provides
enqueue / dequeue / process_one operations.

The queue owns pacing — the pipeline and renderer must NOT perform
Meshtastic-specific sleeping.

``process_one`` supports two modes:

* **No ``send_fn``**: dequeues one item and returns ``None`` (scaffold mode).
* **With ``send_fn``**: dequeues, applies pacing delay, calls the async
  *send_fn*, and returns a :class:`QueueDeliveryResult` with the
  dequeued item and the :class:`AdapterDeliveryResult` containing the
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
The internal deque is intentionally unbounded. ``max_queue_size`` is enforced explicitly at enqueue time so existing accepted items are never silently evicted.
When the queue is full, ``enqueue()`` raises
:class:`~medre.adapters.meshtastic.errors.MeshtasticSendError` with
``transient=True`` instead of accepting the item.  The caller receives
the exception and can classify the failure as transient for pipeline
retry.  Existing queued items are **never evicted** to make room for
new ones — the rejection is explicit.

The ``total_rejected`` counter tracks how many enqueue attempts were
rejected due to a full queue.

.. note::

   Shutdown cancels the drain task without flushing.  In-flight items that have
   not yet been processed lose their native-ref mapping permanently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable

from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.packet_snapshot import json_safe
from medre.core.contracts.adapter import AdapterDeliveryResult

_logger = logging.getLogger(__name__)

# Default maximum queue size.  When full, enqueue raises MeshtasticSendError.
_DEFAULT_MAX_QUEUE_SIZE: int = 1024


@dataclass(frozen=True)
class QueueDeliveryResult:
    """Immutable result of processing one queued item through send.

    Bundles the dequeued item (carrying ``event_id`` and other metadata
    outside the radio payload) with the :class:`AdapterDeliveryResult`
    returned by the send function.  This allows callers (e.g.
    :class:`MeshtasticAdapter._process_queue`) to correlate the canonical
    event ID with the native message ID obtained from the platform.

    Attributes
    ----------
    item:
        The dequeued item dict with ``payload``, ``channel_index``, and
        optionally ``event_id`` keys.
    delivery_result:
        The adapter delivery result populated with the native packet ID
        and metadata.
    """

    item: dict[str, Any]
    delivery_result: AdapterDeliveryResult


class MeshtasticOutboundQueue:
    """Outbound queue with pacing for Meshtastic messages.

    The internal ``deque`` is intentionally created without ``maxlen`` so
    that capacity is enforced explicitly at ``enqueue()`` time, allowing
    the queue to reject with ``transient=True`` rather than silently
    evicting the oldest item.

    Parameters
    ----------
    delay_between_messages:
        Minimum delay in seconds between consecutive outbound messages.
    max_queue_size:
        Maximum number of queued items.

        * ``None`` — unbounded (not recommended for production).
        * Positive ``int`` — bounded; ``enqueue()`` raises
          :class:`~medre.adapters.meshtastic.errors.MeshtasticSendError`
          with ``transient=True`` when full.
        * ``0``, negative, or ``bool`` — **invalid**; raises
          ``ValueError`` at construction time.
    """

    def __init__(
        self,
        delay_between_messages: float = 0.5,
        max_queue_size: int | None = _DEFAULT_MAX_QUEUE_SIZE,
    ) -> None:
        # Validate max_queue_size: None=unbounded, positive int=bounded.
        if max_queue_size is not None:
            if isinstance(max_queue_size, bool):
                raise ValueError("max_queue_size must not be a bool")
            if not isinstance(max_queue_size, int):
                raise ValueError("max_queue_size must be int or None")
            if max_queue_size <= 0:
                raise ValueError("max_queue_size must be > 0 or None")
        self._delay = delay_between_messages
        self._max_queue_size = max_queue_size
        self._queue: deque[dict[str, Any]] = deque()
        self._last_send_time: float = 0.0
        self._total_sent: int = 0
        self._total_failed: int = 0
        self._total_enqueued: int = 0
        self._total_dequeued: int = 0
        self._total_rejected: int = 0

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

    async def enqueue(
        self,
        payload: dict[str, Any],
        channel_index: int,
        *,
        event_id: str | None = None,
    ) -> None:
        """Enqueue a payload for delivery.

        When the queue is at capacity, raises
        :class:`~medre.adapters.meshtastic.errors.MeshtasticSendError`
        with ``transient=True`` instead of accepting the item.  The
        caller (typically :class:`MeshtasticAdapter.deliver`) catches
        this and maps it to an :class:`AdapterSendError(transient=True)`
        for pipeline retry classification.

        Parameters
        ----------
        payload:
            The rendered payload dict to deliver.  ``event_id`` is NOT
            included in this dict — it is stored separately alongside
            the payload so that the radio-facing data never contains
            framework-internal identifiers.
        channel_index:
            The target radio channel index.
        event_id:
            Optional canonical event ID that originated this send.
            Stored outside the payload so it is never sent to the radio.

        Raises
        ------
        MeshtasticSendError
            When the queue is at capacity (``transient=True``).
        """
        if (
            self._max_queue_size is not None
            and len(self._queue) >= self._max_queue_size
        ):
            self._total_rejected += 1
            _logger.warning(
                "MeshtasticOutboundQueue full (%d/%d); rejecting enqueue",
                len(self._queue),
                self._max_queue_size,
            )
            raise MeshtasticSendError(
                "Meshtastic outbound queue is full; "
                f"enqueue rejected ({len(self._queue)}/{self._max_queue_size})",
                transient=True,
            )
        self._queue.append(
            {
                "payload": dict(payload),
                "channel_index": channel_index,
                "event_id": event_id,
            }
        )
        self._total_enqueued += 1

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
        self._total_dequeued += 1
        return self._queue.popleft()

    async def process_one(
        self,
        send_fn: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> QueueDeliveryResult | None:
        """Process one queued item.

        When *send_fn* is ``None`` (scaffold mode), this method dequeues
        one item but does not perform any real send or pacing sleep.
        It returns ``None`` to indicate no delivery was performed.

        When *send_fn* is provided, the method dequeues one item, applies
        the configured pacing delay, calls *send_fn* with the dequeued
        item, and returns a :class:`QueueDeliveryResult` containing both
        the dequeued item and an :class:`AdapterDeliveryResult`
        populated with the native packet ID (if the send result exposes
        one).

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
        QueueDeliveryResult | None
            ``None`` in scaffold mode or when the queue is empty.
            A :class:`QueueDeliveryResult` when a send was attempted.
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

        delivery_result = AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(channel_index),
            metadata=metadata,
        )

        return QueueDeliveryResult(item=item, delivery_result=delivery_result)

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
    def total_enqueued(self) -> int:
        """Total number of items successfully enqueued (not rejected)."""
        return self._total_enqueued

    @property
    def total_dequeued(self) -> int:
        """Total number of items dequeued for processing."""
        return self._total_dequeued

    @property
    def total_rejected(self) -> int:
        """Total number of enqueue attempts rejected due to full queue."""
        return self._total_rejected

    @property
    def queue_health(self) -> dict[str, Any]:
        """Snapshot of the queue's operational state.

        Returns
        -------
        dict
            Keys: ``pending_count``, ``total_sent``, ``total_failed``,
            ``total_enqueued``, ``total_dequeued``, ``total_rejected``,
            ``max_queue_size``, ``utilization_pct``,
            ``delay_between_messages``, ``last_send_time``.
        """
        max_sz = self._max_queue_size
        util = (
            round(len(self._queue) / max_sz * 100, 1) if max_sz and max_sz > 0 else 0.0
        )
        return {
            "pending_count": self.pending_count,
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "total_enqueued": self._total_enqueued,
            "total_dequeued": self._total_dequeued,
            "total_rejected": self._total_rejected,
            "max_queue_size": max_sz,
            "utilization_pct": util,
            "delay_between_messages": self._delay,
            "last_send_time": self._last_send_time,
        }


def _extract_packet_id(result: Any) -> str | None:
    """Extract a native packet ID from a send result.

    The Meshtastic ``sendText`` API returns the sent packet object with
    a populated ``id`` field.  The fake client returns a dict with
    ``"packet_id"``.  Object results may carry ``.packet_id`` instead
    of ``.id``.  When no top-level ID is found, the ``decoded``
    sub-object (dict or attribute) is inspected for ``packet_id``
    then ``id``.

    Fields such as ``channel``, ``reply_id``, ``emoji``,
    ``reaction_id``, and ``reaction_key`` are never used as IDs.

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
    # --- Top-level ID ---------------------------------------------------
    if isinstance(result, dict):
        pid = result.get("packet_id") or result.get("id")
        if pid is not None:
            return str(pid)
    else:
        pid = getattr(result, "id", None)
        if pid is not None:
            return str(pid)
        pid = getattr(result, "packet_id", None)
        if pid is not None:
            return str(pid)
    # --- Decoded sub-object fallback ------------------------------------
    decoded: Any = None
    if isinstance(result, dict):
        decoded = result.get("decoded")
    else:
        decoded = getattr(result, "decoded", None)
    if decoded is None:
        return None
    if isinstance(decoded, dict):
        pid = decoded.get("packet_id")
        if pid is not None:
            return str(pid)
        pid = decoded.get("id")
        if pid is not None:
            return str(pid)
    else:
        pid = getattr(decoded, "packet_id", None)
        if pid is not None:
            return str(pid)
        pid = getattr(decoded, "id", None)
        if pid is not None:
            return str(pid)
    return None


def _packet_snapshot(result: Any) -> dict[str, object]:
    """Extract a safe metadata snapshot from a packet-like send result.

    Returns a dict with available keys (``id``, ``packet_id``,
    ``channel``, ``reply_id``, ``to``, ``emoji``, ``reaction_id``,
    ``reaction_key``) when the result looks like a packet.  Returns an
    empty dict for ``None`` or non-packet results.

    Top-level fields (dict keys or object attributes) are captured
    first.  Then, if the result has a ``decoded`` sub-object (dict or
    object), additional fields are extracted to fill gaps — top-level
    values are never overwritten.

    For dict ``decoded``, ``packet_id`` and ``reaction_id`` are
    captured in addition to ``reply_id``, ``replyId``, ``emoji``,
    ``to``, ``channel``, and ``reaction_key``.  ``decoded["id"]``
    maps to ``snapshot["packet_id"]`` when ``snapshot["packet_id"]``
    is absent; top-level ``snapshot["id"]`` is never overwritten.

    For object ``decoded``, attributes ``to``, ``packet_id``, ``id``
    (mapped to ``"packet_id"``), and ``reaction_id`` are captured
    alongside the existing ``reply_id``, ``replyId``, ``emoji``,
    ``channel``, and ``reaction_key``.

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
                snapshot[key] = json_safe(val)
        decoded = result.get("decoded")
    else:
        for attr in ("id", "packet_id", "channel", "reply_id", "to"):
            val = getattr(result, attr, None)
            if val is not None:
                snapshot[attr] = json_safe(val)
        decoded = getattr(result, "decoded", None)

    # Capture fields from the decoded protobuf sub-object when they fill
    # gaps in the top-level snapshot.  Never overwrite a top-level value.
    if decoded is not None:
        if isinstance(decoded, dict):
            for src_key, dst_key in (
                ("reply_id", "reply_id"),
                ("replyId", "reply_id"),
                ("emoji", "emoji"),
                ("to", "to"),
                ("channel", "channel"),
                ("reaction_key", "reaction_key"),
                ("packet_id", "packet_id"),
                ("reaction_id", "reaction_id"),
            ):
                if src_key not in decoded:
                    continue
                val = decoded[src_key]
                if val is not None and dst_key not in snapshot:
                    snapshot[dst_key] = json_safe(val)
            # decoded.id maps to snapshot["packet_id"] when snapshot["packet_id"]
            # is absent; top-level snapshot["id"] is never overwritten.
            if "packet_id" not in snapshot:
                id_val = decoded.get("id")
                if id_val is not None:
                    snapshot["packet_id"] = json_safe(id_val)
        else:
            for src_attr, dst_key in (
                ("reply_id", "reply_id"),
                ("replyId", "reply_id"),
                ("emoji", "emoji"),
                ("packet_id", "packet_id"),
                ("id", "packet_id"),
                ("to", "to"),
                ("channel", "channel"),
                ("reaction_id", "reaction_id"),
                ("reaction_key", "reaction_key"),
            ):
                if dst_key in snapshot:
                    continue
                val = getattr(decoded, src_attr, None)
                if val is not None:
                    snapshot[dst_key] = json_safe(val)

    return snapshot
