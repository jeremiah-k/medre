"""Meshtastic outbound queue with pacing and bounded retry.

:class:`MeshtasticOutboundQueue` manages outbound message pacing for the
Meshtastic adapter.  It owns the delay between messages and provides
enqueue / dequeue / process_one operations.

The queue owns pacing — the pipeline and renderer must NOT perform
Meshtastic-specific sleeping.

``process_one`` supports two modes:

* **No ``send_fn``**: dequeues one item and returns ``None`` (fake mode).
* **With ``send_fn``**: dequeues, applies pacing delay, calls the async
  *send_fn*, and returns a :class:`QueueDeliveryResult` with the
  dequeued item and the :class:`AdapterDeliveryResult` containing the
  native packet ID if available.

Failure semantics
-----------------
When *send_fn* raises an exception during ``process_one``, the behaviour
depends on the exception type and remaining retry budget:

* ``asyncio.CancelledError`` — re-raised immediately and not counted
  as failed/requeued/exhausted.  Because the item has already been
  dequeued, shutdown-time cancellation can abandon the in-flight
  item; this is **not** durable delivery.
* :class:`~medre.adapters.meshtastic.errors.MeshtasticSendError` with
  ``transient=True`` — the item is **front-requeued** (``appendleft``)
  if the attempt count is below ``max_attempts``; otherwise it is
  counted as exhausted (``total_exhausted`` + ``total_failed``) and
  dropped.
* :class:`~medre.adapters.meshtastic.errors.MeshtasticSendError` with
  ``transient=False`` — the item is **not** retried; it is counted as
  a permanent failure (``total_permanent_failed`` + ``total_failed``)
  and dropped.
* Any other ``Exception`` — treated conservatively as transient and
  front-requeued with the same bounded retry logic.

Front requeue (``appendleft``) preserves urgency and FIFO ordering for
the failed item; the bounded ``max_attempts`` prevents starvation.

.. note::

   "Deliver success" at the adapter boundary still means **local queue
   acceptance only** — it does **not** imply RF confirmation, ACK
   receipt, or durable delivery.  The queue owns local-only retry
   semantics.

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
    """Outbound queue with pacing and bounded retry for Meshtastic messages.

    The internal ``deque`` is intentionally created without ``maxlen`` so
    that capacity is enforced explicitly at ``enqueue()`` time, allowing
    the queue to reject with ``transient=True`` rather than silently
    evicting the oldest item.

    When a transient send failure occurs, the item is front-requeued
    (``appendleft``) up to ``max_attempts`` times.  This preserves
    urgency and FIFO ordering for the failed item while bounding
    starvation.

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
    max_attempts:
        Maximum send attempts per item (first attempt + retries).
        Must be a positive ``int``.  Default: ``3``.
    """

    def __init__(
        self,
        delay_between_messages: float = 0.5,
        max_queue_size: int | None = _DEFAULT_MAX_QUEUE_SIZE,
        max_attempts: int = 3,
    ) -> None:
        # Validate max_queue_size: None=unbounded, positive int=bounded.
        if max_queue_size is not None:
            if isinstance(max_queue_size, bool):
                raise ValueError("max_queue_size must not be a bool")
            if not isinstance(max_queue_size, int):
                raise ValueError("max_queue_size must be int or None")
            if max_queue_size <= 0:
                raise ValueError("max_queue_size must be > 0 or None")
        # Validate max_attempts: positive int only.
        if isinstance(max_attempts, bool):
            raise ValueError("max_attempts must not be a bool")
        if not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an int")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        self._delay = delay_between_messages
        self._max_queue_size = max_queue_size
        self._max_attempts = max_attempts
        self._queue: deque[dict[str, Any]] = deque()
        self._last_send_time: float = 0.0
        self._total_sent: int = 0
        self._total_failed: int = 0
        self._total_enqueued: int = 0
        self._total_dequeued: int = 0
        self._total_rejected: int = 0
        self._total_requeued: int = 0
        self._total_exhausted: int = 0
        self._total_permanent_failed: int = 0

    @property
    def delay_between_messages(self) -> float:
        """Minimum seconds between consecutive outbound messages."""
        return self._delay

    @property
    def max_queue_size(self) -> int | None:
        """Maximum queue capacity, or ``None`` if unbounded."""
        return self._max_queue_size

    @property
    def max_attempts(self) -> int:
        """Maximum send attempts per queued item."""
        return self._max_attempts

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
        delivery_plan_id: str | None = None,
        outbox_id: str | None = None,
        attempt_number: int | None = None,
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
        delivery_plan_id:
            Optional delivery plan ID for deterministic queued→sent
            receipt correlation.  Propagated through the queue item into
            :class:`~medre.core.contracts.adapter.OutboundNativeRefRecord`.
        outbox_id:
            Internal outbox item correlation key.  Propagated through
            the queue item into delayed callback records for exact
            outbox-level correlation.  **Not wire metadata.**
        attempt_number:
            Delivery attempt number from pipeline retry lineage.
            Propagated for stale-callback protection.  **Not wire
            metadata.**

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
                "delivery_plan_id": delivery_plan_id,
                "outbox_id": outbox_id,
                "attempt_number": attempt_number,
                "_attempt": 1,  # internal retry counter; not sent to radio
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

        When *send_fn* is ``None`` (fake mode), this method dequeues
        one item but does not perform any real send or pacing sleep.
        It returns ``None`` to indicate no delivery was performed.

        When *send_fn* is provided, the method dequeues one item, applies
        the configured pacing delay, calls *send_fn* with the dequeued
        item, and returns a :class:`QueueDeliveryResult` containing both
        the dequeued item and an :class:`AdapterDeliveryResult`
        populated with the native packet ID (if the send result exposes
        one).

        On transient failure the item is front-requeued (``appendleft``)
        if its attempt count is below ``max_attempts``; otherwise the
        item is dropped as exhausted.  Permanent failures are never
        retried.  ``asyncio.CancelledError`` is re-raised without
        touching the item's attempt counter or dropping it.

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
            ``None`` in fake mode or when the queue is empty.
            A :class:`QueueDeliveryResult` when a send was attempted.
        """
        item = await self.dequeue()
        if item is None:
            return None

        if send_fn is None:
            # Fake mode: no send, no pacing sleep.
            return None

        # Apply pacing delay based on time since last send attempt.
        # Pacing is per-send-ATTEMPT, not per-dequeue: the timestamp is
        # recorded before send_fn so that transient retries also respect
        # the minimum inter-message gap.  A rapid burst of messages from
        # the queue will each wait for the remaining delay before their
        # send attempt, ensuring the radio is never flooded faster than
        # delay_between_messages allows.
        now = time.monotonic()
        elapsed_since_last = now - self._last_send_time
        remaining = self._delay - elapsed_since_last
        if remaining > 0:
            await asyncio.sleep(remaining)

        # Record send attempt timestamp before send_fn so transient
        # retries also observe pacing (message_delay_seconds).
        self._last_send_time = time.monotonic()

        try:
            send_result = await send_fn(item)
        except asyncio.CancelledError:
            # Re-raise immediately; do NOT swallow or requeue.
            raise
        except MeshtasticSendError as exc:
            if not exc.transient:
                # Permanent failure: no retry.
                self._total_failed += 1
                self._total_permanent_failed += 1
                _logger.warning(
                    "MeshtasticOutboundQueue: permanent send failure for "
                    "event_id=%s; dropping item (attempt %d/%d): %s",
                    item.get("event_id"),
                    item.get("_attempt", 1),
                    self._max_attempts,
                    exc,
                )
                return None
            # Transient: front-requeue if attempts remain.
            self._handle_transient_failure(item)
            return None
        except Exception:
            # Unknown exception: treat conservatively as transient for
            # bounded retry.  send_fn wraps the SDK send boundary, so
            # unexpected errors here are treated as adapter-local
            # transient failures.  Bugs outside send_fn should still
            # surface through the queue drain task.
            _logger.warning(
                "MeshtasticOutboundQueue: unexpected error during send "
                "for event_id=%s; treating as transient (attempt %d/%d)",
                item.get("event_id"),
                item.get("_attempt", 1),
                self._max_attempts,
                exc_info=True,
            )
            self._handle_transient_failure(item)
            return None

        self._total_sent += 1

        # Extract native packet ID from the send result.
        native_id = _extract_packet_id(send_result)
        channel_index = item.get("channel_index", 0)

        # Build packet snapshot metadata when result is packet-like.
        snapshot = _packet_snapshot(send_result)
        metadata = (
            MappingProxyType({"meshtastic": snapshot})
            if snapshot
            else MappingProxyType({})
        )

        delivery_result = AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(channel_index),
            delivery_status="sent",
            metadata=metadata,
        )

        return QueueDeliveryResult(item=item, delivery_result=delivery_result)

    def _handle_transient_failure(self, item: dict[str, Any]) -> None:
        """Handle a transient failure: front-requeue or exhaust.

        Increments the internal ``_attempt`` counter on the item.
        If attempts remain, the item is requeued to the **front**
        (``appendleft``) so it is retried before newer items.
        If the budget is exhausted, the item is dropped and counted
        in ``total_exhausted`` and ``total_failed``.

        Parameters
        ----------
        item:
            The dequeued item dict carrying ``_attempt`` metadata.
        """
        current_attempt = item.get("_attempt", 1)
        next_attempt = current_attempt + 1
        if next_attempt <= self._max_attempts:
            # Bump attempt counter and front-requeue for immediate retry.
            item["_attempt"] = next_attempt
            self._queue.appendleft(item)
            self._total_requeued += 1
            _logger.info(
                "MeshtasticOutboundQueue: transient failure for "
                "event_id=%s; front-requeue item (attempt %d/%d)",
                item.get("event_id"),
                next_attempt,
                self._max_attempts,
            )
        else:
            # Exhausted: drop and count.
            self._total_failed += 1
            self._total_exhausted += 1
            _logger.warning(
                "MeshtasticOutboundQueue: item %s exhausted %d attempts; dropping",
                item.get("event_id"),
                self._max_attempts,
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
        """Terminal send failures (exhausted retries + permanent failures).

        Does **not** increment on transient requeued failures.
        """
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
    def total_requeued(self) -> int:
        """Total number of items front-requeued after transient failure."""
        return self._total_requeued

    @property
    def total_exhausted(self) -> int:
        """Total number of items dropped after exhausting all attempts."""
        return self._total_exhausted

    @property
    def total_permanent_failed(self) -> int:
        """Total number of items dropped due to permanent send failure."""
        return self._total_permanent_failed

    @property
    def queue_health(self) -> dict[str, Any]:
        """Snapshot of the queue's operational state.

        Returns
        -------
        dict
            Keys: ``pending_count``, ``total_sent``, ``total_failed``,
            ``total_enqueued``, ``total_dequeued``, ``total_rejected``,
            ``total_requeued``, ``total_exhausted``,
            ``total_permanent_failed``,
            ``max_queue_size``, ``max_attempts``,
            ``utilization_pct``, ``delay_between_messages``,
            ``last_send_time``.
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
            "total_requeued": self._total_requeued,
            "total_exhausted": self._total_exhausted,
            "total_permanent_failed": self._total_permanent_failed,
            "max_queue_size": max_sz,
            "max_attempts": self._max_attempts,
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
