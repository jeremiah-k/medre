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

**Inbound-path lifecycle guard.** The SDK reader thread calls
:meth:`_on_packet` which schedules an async publish via
``asyncio.run_coroutine_threadsafe``.  The resulting
``concurrent.futures.Future`` is tracked in ``_inbound_futures``.
On :meth:`stop`, ``_started`` is cleared *before* draining, causing
:meth:`_on_packet` to reject late packets and :meth:`_on_packet_async`
to skip publication against a torn-down session.  Remaining inbound
futures are cancelled during :meth:`_drain_background_tasks`.

Session boundary
----------------
The adapter delegates raw transport lifecycle to
:class:`~medre.adapters.meshtastic.session.MeshtasticSession`.
The session owns the raw client; the adapter owns semantic conversion
(classification, codec, event publishing).

Inbound evidence
----------------
The adapter maintains counters for every classification action (relay,
ignore, drop, deferred) and sub-counters for specific reasons (encrypted,
detection sensor, DM, empty text, unknown portnum, malformed).  These
are exposed via :meth:`diagnostics` and provide inbound evidence without
external dependencies.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.errors import (
    MeshtasticSendError,
)
from medre.adapters.meshtastic.packet_classifier import (
    REASON_DETECTION_SENSOR,
    REASON_DIRECT_MESSAGE,
    REASON_EMPTY_TEXT,
    REASON_ENCRYPTED,
    REASON_MALFORMED,
    REASON_UNKNOWN_PORTNUM,
    MeshtasticPacketClassifier,
)
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue, QueueDeliveryResult
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.adapters.meshtastic.startup_backlog import extract_meshtastic_rx_time
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
    OutboundNativeRefRecord,
)
from medre.core.policies.startup_backlog_suppress import (
    should_suppress_startup_backlog,
)
from medre.core.rendering.renderer import RenderingResult

# Base capabilities for the Meshtastic transport adapter (invariant flags).
# Instance capabilities are constructed in __init__ with the configured
# max_text_bytes.
_MESHTASTIC_CAPS_FLAGS: dict[str, object] = dict(
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
    max_text_chars=None,
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

    **Inbound-path lifecycle guard.**  ``run_coroutine_threadsafe``
    Futures are tracked in ``_inbound_futures`` and cancelled on stop.
    The ``_started`` flag gates both :meth:`_on_packet` (sync, SDK
    thread) and :meth:`_on_packet_async` (async, event loop) to
    prevent late-packet processing after session teardown.

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
        self._capabilities = AdapterCapabilities(
            **_MESHTASTIC_CAPS_FLAGS,
            max_text_bytes=config.max_text_bytes,
        )
        self._session: MeshtasticSession | None = None
        self._client: Any = None  # mirrors session.client for diagnostics
        self._codec = MeshtasticCodec(config.adapter_id, config)
        self._classifier = MeshtasticPacketClassifier(config)
        self._queue = MeshtasticOutboundQueue(
            delay_between_messages=config.message_delay_seconds,
            max_attempts=config.queue_send_max_attempts,
        )
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()
        self._inbound_futures: set[concurrent.futures.Future[object]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task | None = None

        # Classifier counters — inbound evidence
        self._classifier_packets_seen: int = 0
        self._classifier_packets_relayed: int = 0
        self._classifier_packets_ignored: int = 0
        self._classifier_packets_dropped: int = 0
        self._classifier_packets_deferred: int = 0
        self._classifier_packets_malformed: int = 0
        self._classifier_packets_encrypted_dropped: int = 0
        self._classifier_packets_detection_sensor_deferred: int = 0
        self._classifier_packets_dm_ignored: int = 0
        self._classifier_packets_empty_text_ignored: int = 0
        self._classifier_packets_unknown_portnum_deferred: int = 0
        self._inbound_published: int = 0

        # Startup backlog suppression
        self._adapter_start_epoch: float | None = None
        self._startup_backlog_packets_seen: int = 0
        self._startup_backlog_packets_suppressed: int = 0

        # Outbound gate suppression counter
        self._outbound_gate_suppressed: int = 0

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

        # Session-scoped startup backlog baseline — set AFTER session connects
        self._adapter_start_epoch = time.time()
        self._startup_backlog_packets_seen = 0
        self._startup_backlog_packets_suppressed = 0

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
        Cancels all tracked background tasks and inbound futures before
        shutting down.

        The ``_started`` flag is cleared *before* draining so that the
        inbound-path guard in :meth:`_on_packet` rejects any late packets
        arriving from the SDK reader thread during teardown.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        # Gate new inbound scheduling *before* draining.  The SDK reader
        # thread may still call _on_packet during teardown; this flag
        # causes _on_packet to return early, preventing new
        # run_coroutine_threadsafe submissions.
        self._started = False

        # Cancel the queue drain background task.
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await asyncio.wait_for(self._drain_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._drain_task = None

        # Cancel all tracked background tasks and drain inbound futures.
        await self._drain_background_tasks(timeout)

        # Delegate stop to session
        if self._session is not None:
            await self._session.stop(timeout=timeout)

        self._client = None
        self._session = None
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

        # Outbound gate: suppress radio sends when listen_only.
        if self._config.outbound_mode == "listen_only":
            self._outbound_gate_suppressed += 1
            raise AdapterPermanentError("outbound suppressed: listen_only mode")

        payload = dict(result.payload)
        channel_index = payload.get("channel_index", self._config.default_channel)
        if not isinstance(channel_index, int):
            channel_index = self._config.default_channel

        try:
            await self._queue.enqueue(payload, channel_index, event_id=result.event_id)
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
        # delivery_status="enqueued" signals to the pipeline that this
        # receipt should be recorded as "queued" rather than "sent";
        # a supplemental "sent" receipt will be appended later when the
        # queue drain produces a real native_message_id.
        return AdapterDeliveryResult(
            native_message_id=None,
            native_channel_id=str(channel_index),
            delivery_note="locally enqueued",
            delivery_status="enqueued",
            metadata=MappingProxyType({"meshtastic_channel_index": channel_index}),
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

    def _increment_classifier_counters(self, classification: Any) -> None:
        """Increment classifier counters based on a ClassificationResult.

        Parameters
        ----------
        classification:
            A :class:`~medre.adapters.meshtastic.packet_classifier.ClassificationResult`.
        """
        self._classifier_packets_seen += 1

        action = classification.action

        # Action-level counter
        if action == "relay":
            self._classifier_packets_relayed += 1
        elif action == "ignore":
            self._classifier_packets_ignored += 1
        elif action == "drop":
            self._classifier_packets_dropped += 1
        elif action == "deferred":
            self._classifier_packets_deferred += 1

        # Reason-level sub-counters (exact match)
        if classification.reason == REASON_MALFORMED:
            self._classifier_packets_malformed += 1
        if classification.reason == REASON_ENCRYPTED:
            self._classifier_packets_encrypted_dropped += 1
        if classification.reason == REASON_DETECTION_SENSOR:
            self._classifier_packets_detection_sensor_deferred += 1
        if classification.reason == REASON_DIRECT_MESSAGE:
            self._classifier_packets_dm_ignored += 1
        if classification.reason == REASON_EMPTY_TEXT:
            self._classifier_packets_empty_text_ignored += 1
        if classification.reason == REASON_UNKNOWN_PORTNUM:
            self._classifier_packets_unknown_portnum_deferred += 1

    def _log_classification(self, classification: Any) -> None:
        """Log a structured classification decision.

        Parameters
        ----------
        classification:
            A :class:`~medre.adapters.meshtastic.packet_classifier.ClassificationResult`.
        """
        if self.ctx is None:
            return
        _logger = self.ctx.logger
        action = classification.action
        reason = classification.reason

        if action == "drop":
            _logger.debug(
                "MeshtasticAdapter %s: packet dropped reason=%s portnum=%s from_id=%s",
                self.adapter_id,
                reason,
                classification.portnum,
                classification.from_id,
            )
        elif action == "ignore":
            _logger.debug(
                "MeshtasticAdapter %s: packet ignored reason=%s category=%s portnum=%s",
                self.adapter_id,
                reason,
                classification.category,
                classification.portnum,
            )
        elif action == "deferred":
            _logger.debug(
                "MeshtasticAdapter %s: packet deferred reason=%s portnum=%s packet_id=%s",
                self.adapter_id,
                reason,
                classification.portnum,
                classification.packet_id,
            )
        elif action == "relay":
            # Preview text from the packet's decoded text for relay log
            _logger.info(
                "MeshtasticAdapter %s: packet relayed packet_id=%s from_id=%s",
                self.adapter_id,
                classification.packet_id,
                classification.from_id,
            )

    def _check_startup_backlog_suppress(
        self, packet: dict[str, Any], packet_id: Any
    ) -> bool:
        """Check whether a relay-classified packet should be suppressed as stale backlog.

        Delegates ``rxTime`` extraction to
        :func:`~medre.adapters.meshtastic.startup_backlog.extract_meshtastic_rx_time`
        and the suppression decision to
        :func:`~medre.core.policies.startup_backlog_suppress.should_suppress_startup_backlog`.

        Returns ``True`` when the packet's ``rxTime`` predates
        ``adapter_start_epoch - startup_backlog_suppress_seconds``.

        Conservative: missing, ``None``, or non-numeric ``rxTime`` are **not**
        suppressed.  A suppression window of ``0`` disables suppression entirely.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict (must contain top-level ``rxTime``).
        packet_id:
            The packet ID for safe logging.

        Returns
        -------
        bool
            ``True`` if the packet should be suppressed; ``False`` otherwise.
        """
        window = self._config.startup_backlog_suppress_seconds
        if window <= 0 or self._adapter_start_epoch is None:
            return False

        packet_time = extract_meshtastic_rx_time(packet)
        adapter_start = datetime.fromtimestamp(
            self._adapter_start_epoch, tz=timezone.utc
        )

        if should_suppress_startup_backlog(packet_time, adapter_start, float(window)):
            if self.ctx is not None:
                cutoff = self._adapter_start_epoch - float(window)
                self.ctx.logger.debug(
                    "MeshtasticAdapter %s: startup backlog suppressed "
                    "transport=meshtastic packet_id=%s rxTime=%s cutoff=%s "
                    "window=%s",
                    self.adapter_id,
                    packet_id,
                    packet.get("rxTime"),
                    cutoff,
                    window,
                )
            return True

        return False

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Process an inbound Meshtastic packet.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        Called from the Meshtastic SDK reader thread.  The ``_started``
        guard rejects packets that arrive after :meth:`stop` has been
        called, preventing late coroutine scheduling against a torn-down
        session.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.
        """
        # Inbound-path lifecycle guard: reject packets arriving after
        # stop() has cleared _started.  This check runs before any
        # coroutine is scheduled via run_coroutine_threadsafe.
        if not self._started:
            return
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            self._increment_classifier_counters(classification)
            self._log_classification(classification)

            # Only relay packets proceed to decode and publish
            if classification.action != "relay":
                return

            # Startup backlog suppression gate
            self._startup_backlog_packets_seen += 1
            if self._check_startup_backlog_suppress(packet, classification.packet_id):
                self._startup_backlog_packets_suppressed += 1
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
            # in the current thread).  The resulting Future is tracked in
            # _inbound_futures so that stop() can drain/cancel it.
            if self._loop is not None and not self._loop.is_closed():
                future: concurrent.futures.Future[object] = (
                    asyncio.run_coroutine_threadsafe(
                        self._on_packet_async(canonical), self._loop
                    )
                )
                self._inbound_futures.add(future)
                future.add_done_callback(lambda f: self._inbound_futures.discard(f))
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

        The ``_started`` guard prevents publication after :meth:`stop`
        has been called — this catches coroutines that were already
        scheduled via ``run_coroutine_threadsafe`` but haven't executed
        yet when teardown begins.

        Parameters
        ----------
        canonical:
            The decoded canonical event to publish.
        """
        try:
            if self.ctx is not None and self._started:
                await self.publish_inbound(canonical)
                self._inbound_published += 1
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
        self._increment_classifier_counters(classification)
        self._log_classification(classification)

        if classification.action != "relay":
            return

        # Startup backlog suppression gate
        self._startup_backlog_packets_seen += 1
        if self._check_startup_backlog_suppress(packet, classification.packet_id):
            self._startup_backlog_packets_suppressed += 1
            return

        canonical = self._codec.decode(packet)
        await self.publish_inbound(canonical)
        self._inbound_published += 1

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
        drain_task = self._drain_task
        result: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "connection_type": self._config.connection_type,
            "queue_pending": self._queue.pending_count,
            "queue_total_sent": self._queue.total_sent,
            "queue_total_failed": self._queue.total_failed,
            "queue_total_enqueued": self._queue.total_enqueued,
            "queue_total_dequeued": self._queue.total_dequeued,
            "queue_total_rejected": self._queue.total_rejected,
            "queue_total_requeued": self._queue.total_requeued,
            "queue_total_exhausted": self._queue.total_exhausted,
            "queue_total_permanent_failed": self._queue.total_permanent_failed,
            "queue_max_size": self._queue.max_queue_size,
            "queue_send_max_attempts": self._queue.max_attempts,
            "queue_utilization_pct": self._queue.queue_health["utilization_pct"],
            "queue_delay_between_messages": self._queue.delay_between_messages,
            "queue_last_send_time": self._queue.queue_health["last_send_time"],
            "drain_task_running": (drain_task is not None and not drain_task.done()),
            "background_tasks": len(self._background_tasks),
            # Classifier counters — inbound evidence
            "classifier_packets_seen": self._classifier_packets_seen,
            "classifier_packets_relayed": self._classifier_packets_relayed,
            "classifier_packets_ignored": self._classifier_packets_ignored,
            "classifier_packets_dropped": self._classifier_packets_dropped,
            "classifier_packets_deferred": self._classifier_packets_deferred,
            "classifier_packets_malformed": self._classifier_packets_malformed,
            "classifier_packets_encrypted_dropped": self._classifier_packets_encrypted_dropped,
            "classifier_packets_detection_sensor_deferred": self._classifier_packets_detection_sensor_deferred,
            "classifier_packets_dm_ignored": self._classifier_packets_dm_ignored,
            "classifier_packets_empty_text_ignored": self._classifier_packets_empty_text_ignored,
            "classifier_packets_unknown_portnum_deferred": self._classifier_packets_unknown_portnum_deferred,
            "inbound_published": self._inbound_published,
            # Startup backlog suppression
            "startup_backlog_packets_seen": self._startup_backlog_packets_seen,
            "startup_backlog_packets_suppressed": self._startup_backlog_packets_suppressed,
            "startup_backlog_suppress_seconds": self._config.startup_backlog_suppress_seconds,
            "adapter_start_epoch": self._adapter_start_epoch,
            # Outbound gate
            "outbound_mode": self._config.outbound_mode,
            "outbound_gate_suppressed": self._outbound_gate_suppressed,
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
        """Cancel and await all tracked background tasks and inbound futures.

        First drains ``concurrent.futures.Future`` instances submitted by
        :meth:`_on_packet` via ``run_coroutine_threadsafe`` — cancelling
        any that haven't started yet and suppressing results from those
        still in flight.  Then cancels and awaits tracked
        :class:`asyncio.Task` instances.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for tasks to finish after cancellation.
        """
        # --- Drain inbound futures (run_coroutine_threadsafe) ---
        # Cancel any that haven't started; suppress results from the rest.
        for future in list(self._inbound_futures):
            future.cancel()
        self._inbound_futures.clear()

        # --- Drain asyncio background tasks ---
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

    async def send_one(self) -> QueueDeliveryResult | None:
        """Send one queued payload via the session, if connected.

        Creates an async wrapper around the session's ``send`` method
        and delegates to :meth:`MeshtasticOutboundQueue.process_one`.

        Returns ``None`` if the queue is empty or the session is not
        connected (fake mode).

        Returns
        -------
        QueueDeliveryResult | None
            Delivery result with both the queued item and adapter result,
            or ``None``.
        """
        session = self._session
        if session is None or session.client is None:
            return None

        async def _send_fn(item: dict[str, Any]) -> Any:
            payload = item.get("payload", {})
            raw_text = payload.get("text", "")
            text = (
                raw_text
                if isinstance(raw_text, str)
                else ("" if raw_text is None else str(raw_text))
            )
            send_dict: dict[str, Any] = {
                "text": text,
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

        After each successful send that yields a real native message ID,
        records a delayed outbound :class:`OutboundNativeRefRecord` via
        the ``record_outbound_native_ref`` callback on
        :class:`AdapterContext` (if wired).  Callback failures are
        caught and logged so they never crash the queue drain.
        """
        try:
            while self._started:
                try:
                    result = await self.send_one()
                    if result is None:
                        await asyncio.sleep(0.1)
                        continue

                    # Record delayed outbound native ref when both
                    # event_id and native_message_id are available.
                    event_id = result.item.get("event_id")
                    delivery = result.delivery_result
                    if (
                        event_id
                        and delivery.native_message_id
                        and self.ctx is not None
                        and self.ctx.record_outbound_native_ref is not None
                    ):
                        try:
                            await self._record_delayed_outbound_ref(
                                result, event_id, delivery
                            )
                        except Exception:
                            if self.ctx is not None:
                                self.ctx.logger.exception(
                                    "MeshtasticAdapter %s: error recording "
                                    "delayed outbound native ref for event_id=%s",
                                    self.adapter_id,
                                    event_id,
                                )
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

    async def _record_delayed_outbound_ref(
        self,
        result: QueueDeliveryResult,
        event_id: str,
        delivery: AdapterDeliveryResult,
    ) -> None:
        """Build and record an :class:`OutboundNativeRefRecord`.

        Assembles metadata from the queued payload and the delivery
        result, excluding private/internal keys and keeping only
        JSON-safe values.

        Parameters
        ----------
        result:
            The queue delivery result containing the dequeued item.
        event_id:
            The canonical event ID associated with this send.
        delivery:
            The adapter delivery result with native IDs and metadata.
        """
        # Build enriched metadata from delivery result + payload context.
        send_meta: dict[str, object] = {}

        # Merge delivery metadata (packet snapshot: id, channel, reply_id, etc.)
        for k, v in (delivery.metadata or {}).items():
            send_meta[k] = v

        # Add useful send context from the queued payload.
        payload = result.item.get("payload", {})
        text = payload.get("text")
        if text is not None:
            send_meta["text"] = str(text)
        meshnet_name = payload.get("meshnet_name")
        if meshnet_name is not None and meshnet_name != "":
            send_meta["meshnet_name"] = str(meshnet_name)
        channel_name = payload.get("channel_name")
        if channel_name is not None and channel_name != "":
            send_meta["channel_name"] = str(channel_name)
        reply_id = payload.get("reply_id")
        if reply_id is not None:
            send_meta["reply_id"] = reply_id
        emoji = payload.get("emoji")
        if emoji is not None:
            send_meta["emoji"] = emoji

        # Caller guarantees native_message_id is non-None, but the type
        # checker cannot see the guard in _process_queue through the
        # method boundary.
        assert delivery.native_message_id is not None

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter=self.adapter_id,
            native_channel_id=delivery.native_channel_id,
            native_message_id=delivery.native_message_id,
            native_thread_id=delivery.native_thread_id,
            native_relation_id=delivery.native_relation_id,
            metadata=send_meta,
        )
        callback = self.ctx.record_outbound_native_ref if self.ctx else None
        if callback is not None:
            await callback(record)

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
