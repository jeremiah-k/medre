"""MeshCore transport adapter for the MEDRE framework.

:class:`MeshCoreAdapter` connects to a MeshCore node and bridges
inbound event payloads into the MEDRE canonical event stream and outbound
rendered payloads back to the mesh.

**No real MeshCore connectivity**: all MeshCore imports are scaffolded.
The adapter raises :class:`~medre.adapters.meshcore.errors.MeshCoreConnectionError`
on :meth:`start` when using non-fake connection types.
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
from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
)
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the MeshCore transport adapter.
_MESHCORE_CAPABILITIES = AdapterCapabilities(
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
    max_text_bytes=512,
    max_text_chars=512,
)


class MeshCoreAdapter(BaseAdapter):
    """Transport adapter for MeshCore nodes.

    Connects to a MeshCore node, receives event payloads, and publishes
    them as canonical events.  Outbound rendered payloads are enqueued
    for paced delivery.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.meshcore.config.MeshCoreConfig`.
    """

    adapter_id: str
    platform: str = "meshcore"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: MeshCoreConfig) -> None:
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _MESHCORE_CAPABILITIES
        self._client: Any = None
        self._codec = MeshCoreCodec(config.adapter_id, config)
        self._classifier = MeshCorePacketClassifier(config)
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the MeshCore node and begin receiving events.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MeshCoreConnectionError
            If connection_type is not ``"fake"`` (real connections not
            yet implemented).
        """
        self.ctx = ctx
        self._started = True

        if self._config.connection_type == "fake":
            # No real client needed for fake mode.
            self._client = None
        else:
            raise MeshCoreConnectionError(
                "Real MeshCore connections not yet implemented; "
                "use connection_type='fake'"
            )

        ctx.logger.info("MeshCoreAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the MeshCore node.

        Cancels all tracked background tasks before shutting down.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        # Cancel all tracked background tasks
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(
                *self._background_tasks, return_exceptions=True
            )
        self._background_tasks.clear()

        self._client = None
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "MeshCoreAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.
        """
        health = "healthy" if self._started else "unknown"
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

        The *result.payload* is expected to be a MeshCore-ready content
        dict already rendered by
        :class:`~medre.adapters.meshcore.renderer.MeshCoreRenderer`.

        In tranche 1 this is scaffolded — returns ``None``.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

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
                f"MeshCoreAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Tranche 1: scaffolded — no real delivery result.
        return None

    # -- Inbound callback ---------------------------------------------------

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Process an inbound MeshCore event payload.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict.
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
                    "MeshCoreAdapter %s: error processing inbound packet",
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
        if classification["category"] != "text":
            return
        if classification["is_ack"]:
            return

        canonical = self._codec.decode(packet)
        await self.ctx.publish_inbound(canonical)

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MeshCoreCodec:  # type: ignore[override]
        """Return the adapter's codec.

        Returns
        -------
        MeshCoreCodec
            The codec instance.
        """
        return self._codec
