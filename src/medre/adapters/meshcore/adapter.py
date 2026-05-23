"""MeshCore transport adapter for the MEDRE framework.

:class:`MeshCoreAdapter` connects to a MeshCore node and bridges
inbound event payloads into the MEDRE canonical event stream and outbound
rendered payloads back to the mesh.

**Soft dependency**: all ``meshcore`` imports are guarded behind
:mod:`~medre.adapters.meshcore.compat`.  If the SDK is not installed
the adapter raises :class:`~medre.adapters.meshcore.errors.MeshCoreConnectionError`
on :meth:`start` when using non-fake connection types.

Connection modes
----------------
The adapter supports four connection types configured via
:class:`~medre.config.adapters.meshcore.MeshCoreConfig`:

``"fake"``
    No real client.  Used for testing without hardware.  Inbound
    simulation via :meth:`simulate_inbound`; outbound via :meth:`deliver`
    returns ``None`` (scaffolded for fake mode, real via session for
    production modes).

``"tcp"``
    Connects via TCP using the MeshCore SDK.

``"serial"``
    Connects via serial using the MeshCore SDK.

``"ble"``
    Connects via BLE using the MeshCore SDK (future).

All non-fake modes require the ``meshcore`` package.  Connection lifecycle
is delegated to :class:`~medre.adapters.meshcore.session.MeshCoreSession`,
which owns the SDK client instance and manages reconnection.

Lifecycle
---------
:meth:`start` and :meth:`stop` are idempotent — calling them multiple
times is safe.  The adapter tracks background :class:`asyncio.Task`
instances spawned by inbound packet callbacks and drains them on stop.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.errors import (
    MeshCoreSendError,
)
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
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
from medre.core.runtime.diagnostic_contract import sanitize_diagnostic_mapping

# Base capabilities for the MeshCore transport adapter.
# max_text_bytes and max_text_chars are overridden per-instance from config.
_MESHCORE_CAPS_BASE = AdapterCapabilities(
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
    # direct_messages=False: MEDRE does not initiate outbound DMs. Inbound
    # PRIV packets are still relayed (relay != DM initiation). See
    # packet_classifier.py for the relay-side note.
    direct_messages=False,
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_bytes=512,
    max_text_chars=512,
)


class MeshCoreAdapter(AdapterContract):
    """Transport adapter for MeshCore nodes.

    Connects to a MeshCore node, receives event payloads, and publishes
    them as canonical events.  Outbound rendered payloads are delivered
    directly via the session for local acceptance.

    The adapter delegates SDK client lifecycle to a
    :class:`~medre.adapters.meshcore.session.MeshCoreSession` instance.
    The session owns the connection, subscriptions, reconnect loop, and
    send operations.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.meshcore.MeshCoreConfig`.
    """

    adapter_id: str
    platform: str = "meshcore"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: MeshCoreConfig) -> None:
        super().__init__()
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = dataclasses.replace(
            _MESHCORE_CAPS_BASE,
            max_text_bytes=config.max_text_bytes,
            max_text_chars=config.max_text_bytes,
        )
        self._client: Any = None
        self._codec = MeshCoreCodec(config.adapter_id, config)
        self._classifier = MeshCorePacketClassifier(config)
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()

        # Aggregate in-memory classifier counters (reset on restart).
        self._classifier_packets_seen: int = 0
        self._classifier_packets_relayed: int = 0
        self._classifier_packets_ignored: int = 0
        self._classifier_packets_dropped: int = 0
        self._classifier_packets_deferred: int = 0
        self._inbound_published: int = 0

        # Session boundary — owns SDK lifecycle.
        self._session: MeshCoreSession | None = None

    # -- Lifecycle ----------------------------------------------------------

    def _reset_inbound_counters(self) -> None:
        """Zero all aggregate in-memory classifier counters.

        Called from :meth:`start` so that a reused adapter instance
        begins with a clean slate on every (re)start.
        """
        self._classifier_packets_seen = 0
        self._classifier_packets_relayed = 0
        self._classifier_packets_ignored = 0
        self._classifier_packets_dropped = 0
        self._classifier_packets_deferred = 0
        self._inbound_published = 0

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the MeshCore node and begin receiving events.

        Idempotent: calling start on an already-started adapter is a no-op.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MeshCoreConnectionError
            If ``meshcore`` SDK is not installed and connection_type is
            not ``"fake"``, or if the real connection fails.
        """
        if self._started:
            return

        self._reset_inbound_counters()

        self.ctx = ctx
        self._mark_started(ctx)

        # Create and start the session.
        self._session = MeshCoreSession(
            config=self._config,
            adapter_id=self.adapter_id,
            platform=self.platform,
            logger=ctx.logger,
        )
        await self._session.start(message_callback=self._on_message)

        self._started = True
        ctx.logger.info(
            "MeshCoreAdapter %s started (mode=%s)",
            self.adapter_id,
            self._config.connection_type,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the MeshCore node.

        Idempotent: calling stop on an already-stopped adapter is a no-op.
        Cancels all tracked background tasks and stops the session.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        # Cancel all tracked background tasks and drain them.
        await self._drain_background_tasks(timeout)

        # Stop the session (handles SDK disconnect + cleanup).
        if self._session is not None:
            await self._session.stop()
            self._session = None

        self._client = None
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info("MeshCoreAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.

        Health states:
            - ``"healthy"`` — adapter started and session connected.
            - ``"degraded"`` — adapter started but session disconnected.
            - ``"unknown"`` — adapter not yet started.
            - ``"failed"`` — client exists but start did not complete
              (subscription failure).
        """
        if self._started:
            if self._session is not None and self._session.connected:
                health = "healthy"
            elif self._session is not None and self._session.reconnecting:
                health = "degraded"
            else:
                health = "degraded"
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
        """Deliver a pre-rendered payload via the session for local acceptance.

        The *result.payload* is expected to be a MeshCore-ready content
        dict already rendered by
        :class:`~medre.adapters.meshcore.renderer.MeshCoreRenderer`.

        For **fake mode** this is scaffolded — returns ``None``.

        For **real modes** the delivery is delegated to the session's
        :meth:`~MeshCoreSession.send_text` method.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` for fake mode; delivery result for real modes.

        Raises
        ------
        AdapterPermanentError
            If the session raises a non-transient error (not initialised,
            SDK rejection, invalid input type).
        AdapterSendError
            If a transient error occurs (timeout, connection, transport).
            ``transient`` is ``True``.
        asyncio.CancelledError
            Propagates without swallowing task cancellation.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"MeshCoreAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Fake mode: scaffolded — no real delivery result.
        if self._config.connection_type == "fake":
            return None

        # Real mode: delegate to session.
        if self._session is None:
            raise AdapterPermanentError("Session not initialised")

        payload = result.payload
        if not isinstance(payload, dict):
            return None

        text = payload.get("text", "")
        channel_index = payload.get("channel_index")
        contact_id = str(payload.get("contact_id", ""))

        try:
            native_id = await self._session.send_text(
                contact_id=contact_id,
                text=str(text),
                channel_index=channel_index if isinstance(channel_index, int) else None,
            )
        except asyncio.CancelledError:
            raise
        except MeshCoreSendError as exc:
            if exc.transient:
                raise AdapterSendError(str(exc), transient=True) from exc
            else:
                raise AdapterPermanentError(str(exc)) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise AdapterSendError(str(exc), transient=True) from exc

        if native_id is None:
            return None

        return AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(channel_index) if channel_index is not None else None,
            delivery_note=(
                "MeshCore alpha — no end-to-end ACK; "
                "status reflects local acceptance only"
            ),
            metadata=MappingProxyType(
                {
                    "delivery_status": "local_accepted",
                }
            ),
        )

    # -- Inbound callback ---------------------------------------------------

    def _on_message(self, packet: dict[str, Any]) -> None:
        """Process an inbound MeshCore event payload from the session.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        This is the **session callback** — it receives plain dicts from
        :class:`~medre.adapters.meshcore.session.MeshCoreSession`, never
        SDK objects.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict.
        """
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            self._increment_classifier_counters(classification)

            # Gate: only relay action packets enter the codec pipeline
            if classification.action != "relay":
                return

            canonical = self._codec.decode(packet)
            # Schedule the async publish — _on_message is synchronous
            # so we create a tracked task that is cleaned up on stop().
            task = asyncio.create_task(self._on_message_async(canonical))
            task.add_done_callback(self._background_tasks.discard)
            self._background_tasks.add(task)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshCoreAdapter %s: error processing inbound packet",
                    self.adapter_id,
                )

    async def _on_message_async(self, canonical: CanonicalEvent) -> None:
        """Async handler for messages received via :meth:`_on_message`.

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
                self._inbound_published += 1
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshCoreAdapter %s: error in background publish",
                    self.adapter_id,
                )

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound MeshCore event payload for testing.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict.

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

        # Gate: only relay action packets enter the codec pipeline
        if classification.action != "relay":
            return

        canonical = self._codec.decode(packet)
        await self.publish_inbound(canonical)
        self._inbound_published += 1

    # -- Diagnostics --------------------------------------------------------

    def _increment_classifier_counters(self, classification: Any) -> None:
        """Increment aggregate classifier counters based on a ClassificationResult.

        Parameters
        ----------
        classification:
            A :class:`~medre.adapters.meshcore.packet_classifier.ClassificationResult`.
        """
        self._classifier_packets_seen += 1

        action = classification.action
        if action == "relay":
            self._classifier_packets_relayed += 1
        elif action == "ignore":
            self._classifier_packets_ignored += 1
        elif action == "drop":
            self._classifier_packets_dropped += 1
        elif action == "deferred":
            self._classifier_packets_deferred += 1

    def diagnostics(self) -> dict[str, Any]:
        """Return adapter-level diagnostics composed from session state.

        No secrets, private keys, or raw SDK internals are exposed.
        All values are guaranteed to be JSON-safe primitives.
        """
        base: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "mode": self._config.connection_type,
            "classifier_packets_seen": self._classifier_packets_seen,
            "classifier_packets_relayed": self._classifier_packets_relayed,
            "classifier_packets_ignored": self._classifier_packets_ignored,
            "classifier_packets_dropped": self._classifier_packets_dropped,
            "classifier_packets_deferred": self._classifier_packets_deferred,
            "inbound_published": self._inbound_published,
        }
        if self._session is not None:
            base["session"] = sanitize_diagnostic_mapping(self._session.diagnostics())
        return base

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

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MeshCoreCodec:  # type: ignore[override]
        """Return the adapter's codec.

        Returns
        -------
        MeshCoreCodec
            The codec instance.
        """
        return self._codec
