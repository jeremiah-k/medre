"""Meshtastic transport adapter for the MEDRE framework.

:class:`MeshtasticAdapter` connects to a Meshtastic radio node and bridges
inbound radio packets into the MEDRE canonical event stream and outbound
rendered payloads back to the radio mesh.

**Soft dependency**: all ``meshtastic`` imports are guarded behind
:mod:`~medre.adapters.meshtastic.compat`.  If ``mtjk`` is not installed
the adapter raises :class:`~medre.adapters.meshtastic.errors.MeshtasticConnectionError`
on :meth:`start` when using non-fake connection types.
"""
from __future__ import annotations

from typing import Any

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
    role: AdapterRole = AdapterRole.PRESENTATION

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

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the Meshtastic node and begin receiving packets.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MeshtasticConnectionError
            If ``mtjk`` is not installed and connection_type is not ``"fake"``.
        """
        self.ctx = ctx
        self._started = True

        if self._config.connection_type == "fake":
            # No real client needed for fake mode.
            self._client = None
        else:
            if not HAS_MESHTASTIC:
                raise MeshtasticConnectionError(
                    "mtjk not installed; pip install mtjk"
                )
            # Real client creation is deferred to a later tranche.
            # The adapter stores None for now and will be populated
            # when real connection code is added.
            self._client = None

        ctx.logger.info("MeshtasticAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the Meshtastic node.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
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

        The *result.payload* is expected to be a Meshtastic-ready content
        dict already rendered by
        :class:`~medre.adapters.meshtastic.renderer.MeshtasticRenderer`.

        In tranche 1 this is scaffolded — the queue is no-op and returns
        ``None``.

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
                f"MeshtasticAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        payload = dict(result.payload)
        channel_index = payload.get("channel_index", self._config.default_channel)
        if not isinstance(channel_index, int):
            channel_index = self._config.default_channel

        await self._queue.enqueue(payload, channel_index)

        # Tranche 1: scaffolded — no real delivery result.
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
            # so we use the event loop directly.
            import asyncio

            asyncio.ensure_future(self.ctx.publish_inbound(canonical))
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshtasticAdapter %s: error processing inbound packet",
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

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MeshtasticCodec:  # type: ignore[override]
        """Return the adapter's codec.

        Returns
        -------
        MeshtasticCodec
            The codec instance.
        """
        return self._codec
