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
:class:`~medre.config.adapters.meshtastic.MeshtasticConfig`:

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
delegated to the :class:`~medre.adapters.meshtastic.session.MeshtasticSession`,
which can be overridden in tests or monkeypatched with fake modules.

Lifecycle
---------
:meth:`start` and :meth:`stop` are idempotent — calling them multiple
times is safe.  The adapter tracks background :class:`asyncio.Task`
instances spawned by inbound packet callbacks and drains them on stop.

Session boundary
----------------
The adapter delegates raw transport lifecycle to
:class:`~medre.adapters.meshtastic.session.MeshtasticSession`.
The session owns the raw client; the adapter owns semantic conversion
(classification, codec, event publishing).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.errors import (
    MeshtasticSendError,
)
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the Meshtastic transport adapter.
_MESHTASTIC_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    reactions="native",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=False,
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_bytes=512,
    max_text_chars=512,
)


class MeshtasticAdapter(AdapterContract):
    """Transport adapter for Meshtastic radio nodes.

    Connects to a Meshtastic node, receives radio packets, and publishes
    them as canonical events.  Outbound rendered payloads are enqueued
    for paced delivery.

    Delegates raw transport lifecycle to
    :class:`~medre.adapters.meshtastic.session.MeshtasticSession`.
    The session owns the raw client; the adapter owns semantic conversion
    (classification, codec, event publishing).

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`.
    """

    adapter_id: str
    platform: str = "meshtastic"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: MeshtasticConfig) -> None:
        super().__init__()
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _MESHTASTIC_CAPABILITIES
        self._session: MeshtasticSession | None = None
        self._client: Any = None  # mirrors session.client for diagnostics
        self._codec = MeshtasticCodec(config.adapter_id, config)
        self._classifier = MeshtasticPacketClassifier(config)
        self._queue = MeshtasticOutboundQueue(
            delay_between_messages=config.message_delay_seconds,
        )
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task | None = None

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
        self._mark_started(ctx)

        # Create session and delegate lifecycle
        self._session = MeshtasticSession(
            config=self._config,
            adapter_id=self.adapter_id,
            platform=self.platform,
            logger=ctx.logger if ctx.logger else None,
        )

        # Register our inbound packet callback with the session
        try:
            await self._session.start(message_callback=self._on_packet)
        except Exception:
            # Clean up session on failure
            self._session = None
            self._client = None
            raise

        # Mirror session client for diagnostics
        self._client = self._session.client

        self._loop = asyncio.get_running_loop()
        self._drain_task = asyncio.create_task(self._process_queue())

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

        # Cancel the queue drain background task.
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await asyncio.wait_for(self._drain_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._drain_task = None

        # Cancel all tracked background tasks and drain them.
        await self._drain_background_tasks(timeout)

        # Delegate stop to session
        if self._session is not None:
            await self._session.stop(timeout=timeout)

        self._client = None
        self._session = None
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info("MeshtasticAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Composes health from session diagnostics when available.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.
        """
        if self._started and self._session is not None:
            if self._session.connected or self._config.connection_type == "fake":
                health = "healthy"
            elif self._session.reconnecting:
                health = "degraded"
            else:
                health = "unknown"
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

        For fake mode, the payload is enqueued and ``None`` is returned.
        For real modes, the payload is enqueued for queue-based delivery.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in scaffold/fake mode (send is async via queue).

        Raises
        ------
        AdapterPermanentError
            If a permanent error occurs (invalid input type, adapter not
            started, payload encoding failure).
        AdapterSendError
            If a transient error occurs (timeout, connection, transport).
            ``transient`` is ``True``.
        asyncio.CancelledError
            Propagates without swallowing task cancellation.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"MeshtasticAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Lifecycle/startup state missing — cannot be repaired by retry.
        # Fake mode does not require start (queue is always available).
        if not self._started and self._config.connection_type != "fake":
            raise AdapterPermanentError("Adapter not started")

        payload = dict(result.payload)
        channel_index = payload.get("channel_index", self._config.default_channel)
        if not isinstance(channel_index, int):
            channel_index = self._config.default_channel

        try:
            await self._queue.enqueue(payload, channel_index)
        except asyncio.CancelledError:
            raise
        except MeshtasticSendError as exc:
            if exc.transient:
                raise AdapterSendError(str(exc), transient=True) from exc
            else:
                raise AdapterPermanentError(str(exc)) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise AdapterSendError(str(exc), transient=True) from exc

        # Queue-based enqueue accepted locally.  Actual send is async via
        # queue.process_one.  No native message ID is available yet.
        return AdapterDeliveryResult(
            native_message_id=None,
            native_channel_id=str(channel_index),
            delivery_note="locally enqueued",
        )

    # -- Inbound callback ---------------------------------------------------

    def _on_receive_callback(
        self, packet: dict[str, Any], interface: Any = None
    ) -> None:
        """Pubsub callback matching the Meshtastic ``onReceive(packet, interface)`` signature.

        Delegates to :meth:`_on_packet` for classification and processing.
        The session's ``_on_receive`` forwards to the adapter's ``_on_packet``
        via the message_callback.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.
        interface:
            The interface that received the packet (unused).
        """
        self._on_packet(packet)

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
            # Only text packets are currently decoded
            if classification["category"] != "text":
                return
            if classification["is_ack"]:
                return

            canonical = self._codec.decode(packet)

            # Enrich longname/shortname from the SDK's nodes dict.
            # Text message packets don't carry user info; that comes from
            # separate NODEINFO_APP packets.  The SDK client maintains a
            # nodes dict mapping node IDs to user info.
            try:
                if (
                    self._session is not None
                    and self._session.client is not None
                    and canonical.metadata.native is not None
                ):
                    from_id = canonical.metadata.native.data.get("from_id", "")
                    if isinstance(from_id, str) and from_id:
                        client_nodes = getattr(self._session.client, "nodes", None)
                        if isinstance(client_nodes, dict):
                            node_info = client_nodes.get(from_id, {})
                            if isinstance(node_info, dict):
                                user_info = node_info.get("user", {})
                                if isinstance(user_info, dict):
                                    ln = str(user_info.get("longName", "") or "")
                                    sn = str(user_info.get("shortName", "") or "")
                                    if ln or sn:
                                        updated_data = dict(
                                            canonical.metadata.native.data
                                        )
                                        updated_data["longname"] = ln
                                        updated_data["shortname"] = sn
                                        new_native = msgspec.structs.replace(
                                            canonical.metadata.native,
                                            data=updated_data,
                                        )
                                        new_metadata = msgspec.structs.replace(
                                            canonical.metadata,
                                            native=new_native,
                                        )
                                        canonical = msgspec.structs.replace(
                                            canonical,
                                            metadata=new_metadata,
                                        )
            except Exception:
                pass  # Non-critical enrichment; names are best-effort
            # Schedule the async publish — _on_packet is called from the
            # Meshtastic SDK reader thread, so we use run_coroutine_threadsafe
            # instead of asyncio.create_task (which requires a running loop
            # in the current thread).
            if self._loop is not None and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._on_packet_async(canonical), self._loop
                )
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
                await self.publish_inbound(canonical)
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
        await self.publish_inbound(canonical)

    # -- Diagnostics --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic snapshot combining adapter and session state.

        No secrets, private keys, raw protobuf dumps, or sensitive radio
        identifiers beyond what is public.

        Returns
        -------
        dict
            Combined adapter + session diagnostics.
        """
        result: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "connection_type": self._config.connection_type,
            "queue_pending": self._queue.pending_count,
            "queue_total_sent": self._queue.total_sent,
            "queue_total_failed": self._queue.total_failed,
            "queue_total_dropped": self._queue.total_dropped,
            "background_tasks": len(self._background_tasks),
        }

        if self._session is not None:
            session_diag = self._session.diagnostics()
            result["session"] = {
                "connected": session_diag.connected,
                "reconnecting": session_diag.reconnecting,
                "reconnect_attempts": session_diag.reconnect_attempts,
                "last_packet_time": session_diag.last_packet_time,
                "node_id": session_diag.node_id,
                "channel_count": session_diag.channel_count,
                "transient_delivery_failures": session_diag.transient_delivery_failures,
                "permanent_delivery_failures": session_diag.permanent_delivery_failures,
                "last_error": session_diag.last_error,
            }

        return result

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
        """Send one queued payload via the session, if connected.

        Creates an async wrapper around the session's ``send`` method
        and delegates to :meth:`MeshtasticOutboundQueue.process_one`.

        Returns ``None`` if the queue is empty or the session is not
        connected (fake mode).

        Returns
        -------
        AdapterDeliveryResult | None
            Delivery metadata or ``None``.
        """
        session = self._session
        if session is None or session.client is None:
            return None

        async def _send_fn(item: dict[str, Any]) -> Any:
            payload = item.get("payload", {})
            send_dict: dict[str, Any] = {
                "text": str(payload.get("text", "")),
                "channel_index": item.get("channel_index", 0),
            }
            if "reply_id" in payload:
                send_dict["reply_id"] = payload["reply_id"]
            if "emoji" in payload:
                send_dict["emoji"] = payload["emoji"]
            return await session.send(send_dict)

        return await self._queue.process_one(send_fn=_send_fn)

    async def _process_queue(self) -> None:
        """Background task that continuously drains the outbound queue.

        Started during :meth:`start` and cancelled during :meth:`stop`.
        Calls :meth:`send_one` in a loop; sleeps when the queue is empty
        or on transient errors.
        """
        try:
            while self._started:
                try:
                    result = await self.send_one()
                    if result is None:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self.ctx is not None:
                        self.ctx.logger.exception(
                            "MeshtasticAdapter %s: error in queue drain",
                            self.adapter_id,
                        )
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

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
