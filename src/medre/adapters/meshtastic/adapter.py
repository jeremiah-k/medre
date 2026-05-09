"""Meshtastic transport adapter for the MEDRE framework.

:class:`MeshtasticAdapter` connects to a Meshtastic radio node and bridges
inbound radio packets into the MEDRE canonical event stream and outbound
rendered payloads back to the radio mesh.

**Soft dependency**: all ``meshtastic`` imports are guarded behind
:mod:`~medre.adapters.meshtastic.compat`.  If ``mtjk`` is not installed
the adapter raises :class:`~medre.adapters.meshtastic.errors.MeshtasticConnectionError`
on :meth:`start` when using non-fake connection types.

Connection modes
----------------
The adapter supports four connection types configured via
:class:`~medre.adapters.meshtastic.config.MeshtasticConfig`:

``"fake"``
    No real client.  Used for testing without hardware.  Inbound
    simulation via :meth:`simulate_inbound`; outbound via :meth:`deliver`
    enqueues to the internal queue but :meth:`deliver` returns ``None``
    (no real send).

``"tcp"``
    Connects via TCP using ``meshtastic.tcp_interface.TCPInterface(hostname, portNumber)``.

``"serial"``
    Connects via serial using ``meshtastic.serial_interface.SerialInterface(devPath)``.

``"ble"``
    Connects via BLE using ``meshtastic.ble_interface.BLEInterface(address)``.

All non-fake modes require the ``mtjk`` package.  Client creation is
delegated to :meth:`_create_client`, which can be overridden in tests
or monkeypatched with fake modules.

Lifecycle
---------
:meth:`start` and :meth:`stop` are idempotent — calling them multiple
times is safe.  The adapter tracks background :class:`asyncio.Task`
instances spawned by inbound packet callbacks and drains them on stop.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.errors import (
    MeshtasticConnectionError,
    MeshtasticSendError,
)
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the Meshtastic transport adapter.
_MESHTASTIC_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=False,
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_bytes=512,
    max_text_chars=512,
)


class MeshtasticAdapter(BaseAdapter):
    """Transport adapter for Meshtastic radio nodes.

    Connects to a Meshtastic node, receives radio packets, and publishes
    them as canonical events.  Outbound rendered payloads are enqueued
    for paced delivery.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.meshtastic.config.MeshtasticConfig`.
    """

    adapter_id: str
    platform: str = "meshtastic"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: MeshtasticConfig) -> None:
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _MESHTASTIC_CAPABILITIES
        self._client: Any = None
        self._codec = MeshtasticCodec(config.adapter_id, config)
        self._classifier = MeshtasticPacketClassifier(config)
        self._queue = MeshtasticOutboundQueue(
            delay_between_messages=config.message_delay_seconds,
        )
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._subscribed: bool = False
        self._background_tasks: set[asyncio.Task] = set()

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the Meshtastic node and begin receiving packets.

        Idempotent: calling start on an already-started adapter is a no-op.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MeshtasticConnectionError
            If ``mtjk`` is not installed and connection_type is not ``"fake"``.
        """
        if self._started:
            return

        self.ctx = ctx

        if self._config.connection_type == "fake":
            # No real client needed for fake mode.
            self._client = None
        else:
            if not HAS_MESHTASTIC:
                raise MeshtasticConnectionError(
                    "mtjk not installed; pip install mtjk"
                )
            self._client = self._create_client()

            # Subscribe to inbound packets via pubsub callback.
            try:
                self._subscribe_callbacks()
            except Exception:
                # Clean up client on subscription failure.
                self._subscribed = False
                try:
                    close_fn = getattr(self._client, "close", None)
                    if close_fn is not None:
                        close_fn()
                except Exception:
                    pass
                self._client = None
                raise

        self._started = True
        ctx.logger.info(
            "MeshtasticAdapter %s started (mode=%s)",
            self.adapter_id,
            self._config.connection_type,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the Meshtastic node.

        Idempotent: calling stop on an already-stopped adapter is a no-op.
        Cancels all tracked background tasks before shutting down.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        # Cancel all tracked background tasks and drain them.
        await self._drain_background_tasks(timeout)

        # Unsubscribe pubsub callbacks and close the client.
        self._unsubscribe_callbacks()

        if self._client is not None:
            try:
                close_fn = getattr(self._client, "close", None)
                if close_fn is not None:
                    close_fn()
            except Exception:
                pass

        self._client = None
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "MeshtasticAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.
        """
        if self._started:
            health = "healthy"
        elif self._client is not None and not self._started:
            # Client exists but start did not complete — subscription failure.
            health = "failed"
        else:
            health = "unknown"
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities,
            health=health,
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Enqueue a pre-rendered payload for paced delivery.

        The *result.payload* is expected to be a Meshtastic-ready content
        dict already rendered by
        :class:`~medre.adapters.meshtastic.renderer.MeshtasticRenderer`.

        In the current scaffold the real adapter enqueues the payload and
        returns ``None`` — send completion is asynchronous and managed
        by the queue's ``process_one`` loop (not by this method).
        Tests and docs must not overclaim production connectivity.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in scaffold mode (send is async via queue).

        Raises
        ------
        TypeError
            If *result* is not a :class:`RenderingResult`.
        """
        if not isinstance(result, RenderingResult):
            raise TypeError(
                f"MeshtasticAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        payload = dict(result.payload)
        channel_index = payload.get("channel_index", self._config.default_channel)
        if not isinstance(channel_index, int):
            channel_index = self._config.default_channel

        await self._queue.enqueue(payload, channel_index)

        # Scaffold: enqueue only; actual send is async via queue.process_one.
        # Returns None — no overclaim of delivery completion.
        return None

    # -- Inbound callback ---------------------------------------------------

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Process an inbound Meshtastic packet.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.
        """
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            # Only process text packets in tranche 1
            if classification["category"] != "text":
                return
            if classification["is_ack"]:
                return

            canonical = self._codec.decode(packet)
            # Schedule the async publish — _on_packet is synchronous
            # so we create a tracked task that is cleaned up on stop().
            task = asyncio.create_task(self._on_packet_async(canonical))
            task.add_done_callback(self._background_tasks.discard)
            self._background_tasks.add(task)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshtasticAdapter %s: error processing inbound packet",
                    self.adapter_id,
                )

    async def _on_packet_async(self, canonical: CanonicalEvent) -> None:
        """Async handler for packets received via :meth:`_on_packet`.

        Publishes the canonical event and logs exceptions from the
        background task.

        Parameters
        ----------
        canonical:
            The decoded canonical event to publish.
        """
        try:
            if self.ctx is not None:
                await self.ctx.publish_inbound(canonical)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshtasticAdapter %s: error in background publish",
                    self.adapter_id,
                )

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound Meshtastic packet for testing.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.

        Raises
        ------
        RuntimeError
            If the adapter has not been started yet.
        """
        if self.ctx is None:
            raise RuntimeError(
                f"Adapter {self.adapter_id!r} has not been started; "
                "call start() before simulate_inbound()."
            )

        classification = self._classifier.classify(packet)
        if classification["category"] != "text":
            return
        if classification["is_ack"]:
            return

        canonical = self._codec.decode(packet)
        await self.ctx.publish_inbound(canonical)

    # -- Client creation (overridable for testing) --------------------------

    def _create_client(self) -> Any:
        """Create a Meshtastic interface client based on config.

        Uses the real ``meshtastic`` library interfaces:

        * TCP: ``TCPInterface(hostname, portNumber)``
        * Serial: ``SerialInterface(devPath)``
        * BLE: ``BLEInterface(address)``

        This method is the single injection point for client creation
        and can be monkeypatched in tests to inject fake clients.

        Returns
        -------
        object
            A Meshtastic interface instance.

        Raises
        ------
        MeshtasticConnectionError
            If the client cannot be created.
        """
        try:
            conn = self._config.connection_type
            if conn == "tcp":
                from meshtastic.tcp_interface import TCPInterface
                assert self._config.host is not None  # validated by config
                return TCPInterface(
                    hostname=self._config.host,
                    portNumber=self._config.port if self._config.port is not None else 4403,
                )
            elif conn == "serial":
                from meshtastic.serial_interface import SerialInterface
                return SerialInterface(
                    devPath=self._config.serial_port,
                )
            elif conn == "ble":
                from meshtastic.ble_interface import BLEInterface  # type: ignore[attr-defined]
                assert self._config.ble_address is not None  # validated by config
                return BLEInterface(
                    address=self._config.ble_address,
                )
            else:
                raise MeshtasticConnectionError(
                    f"Unsupported connection_type: {conn!r}"
                )
        except MeshtasticConnectionError:
            raise
        except Exception as exc:
            raise MeshtasticConnectionError(
                f"Failed to create {self._config.connection_type} client: {exc}"
            ) from exc

    def _subscribe_callbacks(self) -> None:
        """Subscribe to Meshtastic pubsub callbacks for inbound packets.

        Only called when a real client exists.  Uses ``pubsub.pub.subscribe``
        with the ``meshtastic.receive`` topic and the ``onReceive``
        callback signature ``(packet, interface)``.

        Raises
        ------
        MeshtasticConnectionError
            If callback registration fails.
        """
        try:
            from pubsub import pub
            pub.subscribe(self._on_receive_callback, "meshtastic.receive")
        except Exception as exc:
            raise MeshtasticConnectionError(
                f"Failed to subscribe to meshtastic.receive: {exc}"
            ) from exc
        self._subscribed = True

    def _unsubscribe_callbacks(self) -> None:
        """Unsubscribe from Meshtastic pubsub callbacks.

        Only attempts unsubscription if a previous subscription succeeded.
        Failures are logged but not raised.
        """
        if not self._subscribed:
            return
        try:
            from pubsub import pub
            pub.unsubscribe(self._on_receive_callback, "meshtastic.receive")
        except Exception:
            pass
        self._subscribed = False

    def _on_receive_callback(self, packet: dict[str, Any], interface: Any = None) -> None:
        """Pubsub callback matching the Meshtastic ``onReceive(packet, interface)`` signature.

        Delegates to :meth:`_on_packet` for classification and processing.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.
        interface:
            The interface that received the packet (unused).
        """
        self._on_packet(packet)

    # -- Background task management -----------------------------------------

    async def _drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Cancel and await all tracked background tasks.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for tasks to finish after cancellation.
        """
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
        self._background_tasks.clear()

    # -- Queue / send helpers -----------------------------------------------

    async def send_one(self) -> AdapterDeliveryResult | None:
        """Send one queued payload via the real client, if connected.

        Creates an async wrapper around the client's ``sendText`` method
        and delegates to :meth:`MeshtasticOutboundQueue.process_one`.

        Returns ``None`` if the queue is empty or the client is not
        connected (fake mode).

        Returns
        -------
        AdapterDeliveryResult | None
            Delivery metadata or ``None``.
        """
        if self._client is None:
            return None

        async def _send_fn(item: dict[str, Any]) -> Any:
            payload = item.get("payload", {})
            channel_index = item.get("channel_index", 0)
            text = str(payload.get("text", ""))
            # sendText is synchronous; wrap in executor.
            return await asyncio.to_thread(
                self._client.sendText,
                text,
                channelIndex=channel_index,
            )

        return await self._queue.process_one(send_fn=_send_fn)

    @property
    def queue_health(self) -> dict[str, Any]:
        """Proxy for the outbound queue's health snapshot.

        Returns
        -------
        dict
            Queue operational state (pending, sent, failed counts, etc.).
        """
        return self._queue.queue_health

    @property
    def queue(self) -> MeshtasticOutboundQueue:
        """The adapter's owned outbound queue."""
        return self._queue

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MeshtasticCodec:  # type: ignore[override]
        """Return the adapter's codec.

        Returns
        -------
        MeshtasticCodec
            The codec instance.
        """
        return self._codec
