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
from collections.abc import Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

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
    REASON_SELF_ECHO,
    REASON_UNKNOWN_PORTNUM,
    MeshtasticPacketClassifier,
)
from medre.adapters.meshtastic.queue import (
    MeshtasticOutboundQueue,
    QueueDeliveryResult,
    QueueTerminalResult,
)
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
    QueueTerminalRecord,
)
from medre.core.policies.startup_backlog_suppress import (
    should_suppress_startup_backlog,
)
from medre.core.rendering.renderer import RenderingResult


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
            max_text_bytes=config.max_text_bytes,
        )
        self._session: MeshtasticSession | None = None
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
        self._classifier_packets_self_echo_ignored: int = 0
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
            raise

        # Session-scoped startup backlog baseline — set AFTER session connects
        self._adapter_start_epoch = time.time()
        self._startup_backlog_packets_seen = 0
        self._startup_backlog_packets_suppressed = 0

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
        elif self._session is not None and not self._started:
            # Session exists but start did not complete — subscription failure.
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

        .. note::

           Direct ``deliver()`` calls (outside the queue path) produce
           results without outbox correlation.  This is acceptable for
           adapter-boundary tests and manual probes but not for normal
           queue-based delivery, which requires ``outbox_id`` +
           ``attempt_number`` for exact lifecycle correlation.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in fake mode (send is async via queue).

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
            await self._queue.enqueue(
                payload,
                channel_index,
                event_id=result.event_id,
                delivery_plan_id=result.delivery_plan_id,
                outbox_id=result.outbox_id,
                attempt_number=result.attempt_number,
            )
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
            metadata=MappingProxyType(
                {
                    "meshtastic": {
                        "channel_index": channel_index,
                    }
                }
            ),
        )

    # -- Inbound callback ---------------------------------------------------

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
        if classification.reason == REASON_SELF_ECHO:
            self._classifier_packets_self_echo_ignored += 1

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

    def _enrich_with_node_info(self, packet: dict[str, Any]) -> dict[str, str] | None:
        """Look up node info (longname/shortname) from the session node database.

        Reads ``fromId`` directly from the raw packet dict so we never
        need a preliminary codec decode just to obtain the sender ID.

        Returns ``None`` when the session is unavailable, the node ID is
        empty, the node is unknown, or the lookup raises.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict.

        Returns
        -------
        dict[str, str] | None
            ``{"longname": ..., "shortname": ...}`` or ``None``.
        """
        from_id = str(packet.get("fromId", "") or "")
        if not from_id or self._session is None:
            return None
        try:
            return self._session.get_node_info(from_id)
        except Exception:
            return None  # Non-critical enrichment; names are best-effort

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
            session = self._session
            own_node_id = session.node_id if session is not None else None
            classification = self._classifier.classify(packet, own_node_id=own_node_id)
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

            # Enrich longname/shortname via the session's node database.
            # Text message packets don't carry user info; that comes from
            # separate NODEINFO_APP packets.  The codec handles embedding
            # into native metadata when node_info is provided.
            node_info = self._enrich_with_node_info(packet)
            canonical = self._codec.decode(packet, node_info=node_info)
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

        session = self._session
        classification = self._classifier.classify(
            packet,
            own_node_id=(session.node_id if session is not None else None),
        )
        self._increment_classifier_counters(classification)
        self._log_classification(classification)

        if classification.action != "relay":
            return

        # Startup backlog suppression gate
        self._startup_backlog_packets_seen += 1
        if self._check_startup_backlog_suppress(packet, classification.packet_id):
            self._startup_backlog_packets_suppressed += 1
            return

        # Enrich with node info (same path as _on_packet)
        node_info = self._enrich_with_node_info(packet)
        canonical = self._codec.decode(packet, node_info=node_info)
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
            "classifier_packets_self_echo_ignored": self._classifier_packets_self_echo_ignored,
            "inbound_published": self._inbound_published,
            # Startup backlog suppression
            "startup_backlog_packets_seen": self._startup_backlog_packets_seen,
            "startup_backlog_packets_suppressed": self._startup_backlog_packets_suppressed,
            "startup_backlog_suppress_seconds": self._config.startup_backlog_suppress_seconds,
            "adapter_start_epoch": self._adapter_start_epoch,
            # Outbound gate
            "outbound_mode": self._config.outbound_mode,
            "outbound_gate_suppressed": self._outbound_gate_suppressed,
            # Full queue health snapshot (structurally identical to the
            # individual queue_* keys above but provided as a single
            # nested dict for programmatic consumers).
            "queue": self._queue.queue_health,
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

    def _observe_callback_task(
        self,
        task: asyncio.Task,
        label: str,
        event_id: str = "",
    ) -> None:
        """Observe a callback task's result after cancellation.

        When ``_process_queue`` is cancelled, shielded callback tasks are
        added to ``_background_tasks`` for later draining.  This helper
        registers a done-callback that retrieves and logs any exception
        before the task is removed from ``_background_tasks``, preventing
        ``"Task exception was never retrieved"`` warnings from the event
        loop.

        Parameters
        ----------
        task:
            The background callback task to observe.
        label:
            Human-readable label for log messages (e.g. ``"terminal"``,
            ``"native-ref"``).
        event_id:
            Optional event ID for log context.
        """
        adapter_id = self.adapter_id

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                if self.ctx is not None:
                    self.ctx.logger.warning(
                        "MeshtasticAdapter %s: %s callback "
                        "exception observed for event_id=%s: %r",
                        adapter_id,
                        label,
                        event_id,
                        exc,
                    )

        self._background_tasks.add(task)
        task.add_done_callback(_on_done)

    def _observe_detached_task(self, task: asyncio.Task) -> None:
        """Done callback for tasks detached after shutdown timeout.

        When a critical callback suppresses CancelledError and remains
        pending past the drain timeout, it is detached from
        ``_background_tasks`` with this callback.  When the task finally
        completes, this callback retrieves and logs any exception so
        nothing becomes "Task exception was never retrieved."
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self.ctx is not None:
            self.ctx.logger.warning(
                "MeshtasticAdapter %s: detached background callback "
                "exception after shutdown timeout: %r",
                self.adapter_id,
                exc,
            )

    async def _drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Drain inbound futures and await tracked background tasks.

        First drains ``concurrent.futures.Future`` instances submitted by
        :meth:`_on_packet` via ``run_coroutine_threadsafe`` — cancelling
        any that haven't started yet and suppressing results from those
        still in flight.  Then awaits tracked :class:`asyncio.Task`
        instances (terminal / native-ref callbacks) with a bounded timeout.

        Completed tasks have their exceptions observed and logged.  If any
        tasks are still pending after the timeout, they are explicitly
        cancelled and given a second bounded timeout to finish.  Tasks
        that suppress ``CancelledError`` and remain pending after the
        second timeout are detached with a done-callback observer so their
        exceptions are still logged if/when they eventually finish.  No
        callback exception is intentionally left unobserved.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for tasks to finish in each phase
            (initial drain and post-cancel drain).
        """
        # --- Drain inbound futures (run_coroutine_threadsafe) ---
        # Cancel any that haven't started; suppress results from the rest.
        for future in list(self._inbound_futures):
            future.cancel()
        self._inbound_futures.clear()

        # --- Drain asyncio background tasks ---
        # Critical callback tasks (terminal / native-ref) are given a
        # bounded timeout to complete.  Completed tasks have exceptions
        # observed; pending tasks are cancelled and given a second
        # timeout.  Still-pending tasks are detached with an observer.
        if self._background_tasks:
            _done, _pending = await asyncio.wait(
                self._background_tasks,
                timeout=timeout,
            )

            # Retrieve and log exceptions from completed tasks.
            for task in _done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and self.ctx is not None:
                    self.ctx.logger.warning(
                        "MeshtasticAdapter %s: background callback "
                        "exception during drain: %r",
                        self.adapter_id,
                        exc,
                    )

            # If any tasks are still pending after timeout, cancel them
            # explicitly and await so no task remains untracked or raises
            # an unobserved exception.
            if _pending:
                for task in _pending:
                    task.cancel()
                # Await cancelled tasks to collect CancelledError.
                done_after_cancel, still_pending = await asyncio.wait(
                    _pending,
                    timeout=timeout,
                )
                for task in done_after_cancel:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None and self.ctx is not None:
                        self.ctx.logger.warning(
                            "MeshtasticAdapter %s: background callback "
                            "exception after timeout cancel: %r",
                            self.adapter_id,
                            exc,
                        )
                # Tasks that suppress CancelledError and remain pending
                # after the second timeout are detached with a done
                # callback that observes their result.  They are NOT
                # left in _background_tasks (shutdown must progress) but
                # their exceptions will be observed if/when they finish.
                for task in still_pending:
                    if self.ctx is not None:
                        self.ctx.logger.warning(
                            "MeshtasticAdapter %s: background callback "
                            "task still pending after shutdown timeout; "
                            "detaching with observer",
                            self.adapter_id,
                        )
                    self._background_tasks.discard(task)
                    task.add_done_callback(self._observe_detached_task)

        self._background_tasks.clear()

    # -- Queue / send helpers -----------------------------------------------

    async def send_one(self) -> QueueDeliveryResult | QueueTerminalResult | None:
        """Send one queued payload via the session, if connected.

        Creates an async wrapper around the session's ``send`` method
        and delegates to :meth:`MeshtasticOutboundQueue.process_one`.

        Returns ``None`` if the queue is empty or the session is not
        connected (fake mode).

        Returns
        -------
        QueueDeliveryResult | QueueTerminalResult | None
            Delivery result on success, terminal result on exhaustion or
            permanent failure, or ``None`` when the queue is empty /
            session is disconnected.
        """
        session = self._session
        if session is None or not session.connected:
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

        When :meth:`send_one` returns a :class:`QueueTerminalResult`
        (exhausted or permanent failure), reports the terminal outcome
        to core via the ``record_outbound_terminal`` callback.  On
        cancellation or shutdown, the in-flight cancelled item is reported.
        Remaining queued items are drained and reported as abandoned only
        when there is evidence the drain task was actively processing work.
        If no in-flight item existed when stop cancelled the drain task,
        local queued items remain in memory for next start and the durable
        outbox remains queued/stale-recoverable.

        See :meth:`_report_cancelled_and_drain` for the drain-on-cancel
        implementation.
        """
        try:
            while self._started:
                try:
                    result = await self.send_one()
                    if result is None:
                        await asyncio.sleep(0.1)
                        continue

                    if isinstance(result, QueueTerminalResult):
                        # Shield the terminal callback so stop() cancelling
                        # _drain_task cannot abort the report for an item
                        # already dequeued from the queue.  The inner task
                        # is tracked so that stop()/_drain_background_tasks
                        # can await it if the drain task is cancelled mid-callback.
                        cb_task = asyncio.ensure_future(
                            self._report_queue_terminal(result)
                        )
                        try:
                            await asyncio.shield(cb_task)
                        except asyncio.CancelledError:
                            _evt = result.item.get("event_id") or ""
                            self._observe_callback_task(
                                cb_task,
                                "terminal",
                                _evt,
                            )
                            raise
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
                            # Shield the native-ref callback for the same
                            # reason as _report_queue_terminal above.  The
                            # inner task is tracked so that stop() can drain
                            # it even if the drain task is cancelled mid-callback.
                            cb_task = asyncio.ensure_future(
                                self._record_delayed_outbound_ref(
                                    result, event_id, delivery
                                )
                            )
                            try:
                                await asyncio.shield(cb_task)
                            except asyncio.CancelledError:
                                self._observe_callback_task(
                                    cb_task,
                                    "native-ref",
                                    event_id or "",
                                )
                                raise
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
            # Report the in-flight cancelled item (if any) and drain
            # remaining queued items as abandoned.
            await self._report_cancelled_and_drain()

    async def _report_queue_terminal(self, result: QueueTerminalResult) -> None:
        """Report a terminal queue outcome to core.

        Constructs a :class:`QueueTerminalRecord` from the terminal
        result and calls the ``record_outbound_terminal`` callback.
        Failures are caught and logged so they never crash the queue
        drain loop.

        Parameters
        ----------
        result:
            The terminal result from :meth:`send_one`.
        """
        record = QueueTerminalRecord(
            event_id=result.item.get("event_id") or "",
            adapter=self.adapter_id,
            outbox_id=result.item.get("outbox_id"),
            delivery_plan_id=result.item.get("delivery_plan_id"),
            attempt_number=result.item.get("attempt_number"),
            native_channel_id=(
                str(ch) if (ch := result.item.get("channel_index")) is not None else ""
            ),
            outcome=result.outcome,
            error=result.error,
        )
        callback = self.ctx.record_outbound_terminal if self.ctx is not None else None
        if callback is not None:
            try:
                await callback(record)
            except Exception:
                if self.ctx is not None:
                    self.ctx.logger.exception(
                        "MeshtasticAdapter %s: error reporting terminal "
                        "queue outcome for event_id=%s outcome=%s",
                        self.adapter_id,
                        record.event_id,
                        record.outcome,
                    )

    async def _report_cancelled_and_drain(self) -> None:
        """Report the in-flight cancelled item and drain remaining items.

        Called when the queue drain task catches CancelledError.  Retrieves
        the in-flight item that was being processed when cancellation
        occurred (via :meth:`pop_cancelled_item`) and reports it as
        ``"cancelled"``.

        Only drains remaining queued items when there is evidence the
        drain task was actively processing work (a cancelled in-flight
        item exists).  When no item was in-flight the drain task was
        cancelled before doing any work (e.g. immediately after start());
        remaining items are left in the queue so they survive across the
        stop boundary for the next start() cycle.
        """
        callback = self.ctx.record_outbound_terminal if self.ctx is not None else None

        # Report the in-flight cancelled item.
        cancelled_item = self._queue.pop_cancelled_item()
        if cancelled_item is not None:
            if callback is not None:
                record = QueueTerminalRecord(
                    event_id=cancelled_item.get("event_id") or "",
                    adapter=self.adapter_id,
                    outbox_id=cancelled_item.get("outbox_id"),
                    delivery_plan_id=cancelled_item.get("delivery_plan_id"),
                    attempt_number=cancelled_item.get("attempt_number"),
                    native_channel_id=(
                        str(ch)
                        if (ch := cancelled_item.get("channel_index")) is not None
                        else ""
                    ),
                    outcome="cancelled",
                    error="queue drain task cancelled while item was in-flight",
                )
                try:
                    cb_task = asyncio.ensure_future(callback(record))
                    try:
                        await asyncio.shield(cb_task)
                    except asyncio.CancelledError:
                        # stop() is cancelling us — observe the shielded
                        # task so its exception (if any) is not lost, then
                        # re-raise so shutdown can progress.
                        self._observe_callback_task(
                            cb_task,
                            "cancelled",
                            record.event_id,
                        )
                        raise
                except Exception:
                    if self.ctx is not None:
                        self.ctx.logger.exception(
                            "MeshtasticAdapter %s: error reporting cancelled "
                            "item for event_id=%s",
                            self.adapter_id,
                            record.event_id,
                        )

            # Drain remaining items as abandoned — the drain task was
            # actively processing, so all remaining work is orphaned.
            remaining = self._queue.drain_all()
            if callback is not None:
                for item in remaining:
                    record = QueueTerminalRecord(
                        event_id=item.get("event_id") or "",
                        adapter=self.adapter_id,
                        outbox_id=item.get("outbox_id"),
                        delivery_plan_id=item.get("delivery_plan_id"),
                        attempt_number=item.get("attempt_number"),
                        native_channel_id=(
                            str(ch)
                            if (ch := item.get("channel_index")) is not None
                            else ""
                        ),
                        outcome="abandoned",
                        error="adapter shutdown with unsent queued items",
                    )
                    try:
                        cb_task = asyncio.ensure_future(callback(record))
                        try:
                            await asyncio.shield(cb_task)
                        except asyncio.CancelledError:
                            self._observe_callback_task(
                                cb_task,
                                "abandoned",
                                record.event_id,
                            )
                            raise
                    except Exception:
                        if self.ctx is not None:
                            self.ctx.logger.exception(
                                "MeshtasticAdapter %s: error reporting abandoned "
                                "item for event_id=%s",
                                self.adapter_id,
                                record.event_id,
                            )
            else:
                # No callback — just drain silently.
                self._queue.drain_all()

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

        # Merge delivery metadata into the ``meshtastic`` namespace.
        meshtastic_meta: dict[str, object] = {}
        transport_keys = {
            "id",
            "packet_id",
            "channel",
            "reply_id",
            "emoji",
            "reaction_id",
            "to",
        }
        for k, v in (delivery.metadata or {}).items():
            if k == "meshtastic" and isinstance(v, Mapping):
                meshtastic_meta.update(dict(v))
            elif k in transport_keys:
                meshtastic_meta[k] = v
            else:
                # Defensive normalization: delivery metadata should already
                # be namespaced under the transport key, but legacy or
                # non-namespaced keys (e.g. source_bridge, seq) are placed
                # into the meshtastic namespace rather than leaking to the
                # top level of NativeMessageRef.metadata.
                meshtastic_meta[k] = v
        # Add useful send context from the queued payload into the
        # meshtastic namespace (transport-specific data must live
        # under metadata[<transport>]).
        payload = result.item.get("payload", {})
        text = payload.get("text")
        if text is not None:
            meshtastic_meta["text"] = str(text)
        meshnet_name = payload.get("meshnet_name")
        if meshnet_name is not None and meshnet_name != "":
            meshtastic_meta["meshnet_name"] = str(meshnet_name)
        channel_name = payload.get("channel_name")
        if channel_name is not None and channel_name != "":
            meshtastic_meta["channel_name"] = str(channel_name)
        reply_id = payload.get("reply_id")
        if reply_id is not None:
            meshtastic_meta["reply_id"] = reply_id
        emoji = payload.get("emoji")
        if emoji is not None:
            meshtastic_meta["emoji"] = emoji

        # Always carry the transport key so the record is self-identifying
        # even when no Meshtastic-specific metadata is available.
        send_meta["meshtastic"] = meshtastic_meta

        # Caller guarantees native_message_id is non-None, but the type
        # checker cannot see the guard in _process_queue through the
        # method boundary.
        if delivery.native_message_id is None:
            raise RuntimeError(
                "delivery.native_message_id must be non-None when recording "
                "delayed outbound ref"
            )

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter=self.adapter_id,
            native_channel_id=delivery.native_channel_id,
            native_message_id=delivery.native_message_id,
            native_thread_id=delivery.native_thread_id,
            native_relation_id=delivery.native_relation_id,
            delivery_plan_id=result.item.get("delivery_plan_id"),
            outbox_id=result.item.get("outbox_id"),
            attempt_number=result.item.get("attempt_number"),
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

    def get_codec(self) -> MeshtasticCodec:
        """Return the adapter's codec.

        Returns
        -------
        MeshtasticCodec
            The codec instance.
        """
        return self._codec
