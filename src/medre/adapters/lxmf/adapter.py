"""LXMF transport adapter for the MEDRE framework.

:class:`LxmfAdapter` connects to an LXMF router/node and bridges
inbound message payloads into the MEDRE canonical event stream and
outbound rendered payloads back to the mesh.

**Soft dependency**: all ``lxmf`` / ``RNS`` imports are guarded behind
:mod:`~medre.adapters.lxmf.compat`.  If the packages are not installed
the adapter raises :class:`~medre.adapters.lxmf.errors.LxmfConnectionError`
on :meth:`start` when using non-fake connection types.

Connection modes
----------------
The adapter supports connection types configured via
:class:`~medre.adapters.lxmf.config.LxmfConfig`:

``"fake"``
    No real client.  Used for testing without hardware.  Inbound
    simulation via :meth:`simulate_inbound`; outbound via :meth:`deliver`
    returns ``None`` (scaffolded).

``"reticulum"``
    **Not implemented yet.**  :meth:`start` always raises
    :class:`~medre.adapters.lxmf.errors.LxmfConnectionError` for
    non-fake connection types, regardless of whether ``lxmf``/``RNS``
    are installed.  Production connectivity is deferred to a future
    tranche.

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
from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.compat import HAS_LXMF
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.errors import (
    LxmfConnectionError,
)
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the LXMF transport adapter.
_LXMF_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=True,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=True,
    channels=False,
    async_delivery=True,
    identity_encryption=True,
    mesh_routing=True,
    max_text_bytes=None,
    max_text_chars=16384,
)


class LxmfAdapter(BaseAdapter):
    """Transport adapter for LXMF routers/nodes.

    Connects to an LXMF router, receives message payloads, and publishes
    them as canonical events.  Outbound rendered payloads are enqueued
    for paced delivery.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.lxmf.config.LxmfConfig`.
    """

    adapter_id: str
    platform: str = "lxmf"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: LxmfConfig) -> None:
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _LXMF_CAPABILITIES
        self._client: Any = None
        self._codec = LxmfCodec(config.adapter_id, config)
        self._classifier = LxmfPacketClassifier(config)
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the LXMF router/node and begin receiving events.

        Idempotent: calling start on an already-started adapter is a no-op.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        LxmfConnectionError
            If ``lxmf`` / ``RNS`` are not installed and connection_type
            is not ``"fake"``.
        """
        if self._started:
            return

        self.ctx = ctx

        if self._config.connection_type == "fake":
            self._client = None
        else:
            if not HAS_LXMF:
                raise LxmfConnectionError(
                    "lxmf/RNS not installed; pip install lxmf. "
                    f"connection_type={self._config.connection_type!r}"
                )
            # Production LXMF/Reticulum connectivity is not implemented yet.
            # Even when the SDK is installed, no real client is created.
            raise LxmfConnectionError(
                "production LXMF/Reticulum connectivity is not implemented yet"
            )

        self._started = True
        ctx.logger.info(
            "LxmfAdapter %s started (mode=%s)",
            self.adapter_id,
            self._config.connection_type,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the LXMF router/node.

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

        # Unsubscribe event callbacks.
        self._unsubscribe_events()

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
                "LxmfAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state with a health
            string of ``"healthy"``, ``"unknown"``, or ``"failed"``.
        """
        if self._started:
            health = "healthy"
        elif self._client is not None and not self._started:
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

    # -- Event subscription scaffold ----------------------------------------

    def _subscribe_events(self) -> None:
        """Subscribe to LXMRouter inbound message callbacks.

        Scaffold: wires ``lxmf`` / ``RNS`` callbacks when a real
        LXMRouter is available.  Currently logs intent only; actual
        callback registration deferred to production implementation.

        Raises
        ------
        LxmfConnectionError
            If callback registration fails.
        """
        if self.ctx is not None:
            self.ctx.logger.debug(
                "LxmfAdapter %s: _subscribe_events scaffold called",
                self.adapter_id,
            )
        # Future: register LXMRouter message callback
        # router = lxmf.LXMRouter(identity=...)
        # router.register_delivery_callback(self._on_lxmf_message)

    def _unsubscribe_events(self) -> None:
        """Unsubscribe from LXMRouter inbound message callbacks.

        Scaffold: tears down ``lxmf`` / ``RNS`` callbacks.  Currently
        logs intent only.  Failures are logged but not raised.
        """
        if self.ctx is not None:
            self.ctx.logger.debug(
                "LxmfAdapter %s: _unsubscribe_events scaffold called",
                self.adapter_id,
            )
        # Future: unregister LXMRouter message callback

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
                    asyncio.gather(
                        *self._background_tasks, return_exceptions=True
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
        self._background_tasks.clear()

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Enqueue a pre-rendered payload for paced delivery.

        The *result.payload* is expected to be an LXMF-ready content
        dict already rendered by
        :class:`~medre.adapters.lxmf.renderer.LxmfRenderer`.

        In tranche 1 this is scaffolded — returns ``None``.

        Parameters
        ----------
        result:
            The rendered payload to deliver.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` in tranche 1 (scaffolded).

        Raises
        ------
        TypeError
            If *result* is not a :class:`RenderingResult`.
        """
        if not isinstance(result, RenderingResult):
            raise TypeError(
                f"LxmfAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        return None

    # -- Inbound callback ---------------------------------------------------

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Process an inbound LXMF message payload.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        Parameters
        ----------
        packet:
            Raw LXMF message payload dict.
        """
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            if classification["category"] != "text":
                return
            if classification["is_ack"]:
                return

            canonical = self._codec.decode(packet)
            task = asyncio.create_task(self._on_packet_async(canonical))
            task.add_done_callback(self._background_tasks.discard)
            self._background_tasks.add(task)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "LxmfAdapter %s: error processing inbound packet",
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
                    "LxmfAdapter %s: error in background publish",
                    self.adapter_id,
                )

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound LXMF message payload for testing.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.

        Parameters
        ----------
        packet:
            Raw LXMF message payload dict.

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

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> LxmfCodec:  # type: ignore[override]
        """Return the adapter's codec.

        Returns
        -------
        LxmfCodec
            The codec instance.
        """
        return self._codec
