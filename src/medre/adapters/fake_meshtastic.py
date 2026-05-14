"""Fake Meshtastic adapter for testing.

:class:`FakeMeshtasticAdapter` simulates a Meshtastic transport adapter
without any real radio or ``mtjk`` dependency.  It mirrors the real
:class:`~medre.adapters.meshtastic.adapter.MeshtasticAdapter` and is
intended solely for use in unit and integration tests.

Capabilities
------------
* text messaging only
* no replies, reactions, edits, deletes, attachments, or delivery receipts
* packet classification and codec decoding (via real classifier + codec)

Usage
-----
>>> config = MeshtasticConfig(adapter_id="test_mesh")
>>> adapter = FakeMeshtasticAdapter(config)
>>> await adapter.start(ctx)
>>> # Simulate an inbound Meshtastic text packet
>>> event = adapter.make_text_event("Hello from mesh!")
>>> await adapter.simulate_inbound(packet_dict)
>>> # Deliver an outbound rendered payload
>>> delivery = await adapter.deliver(result)
>>> assert adapter.delivered_payloads
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    AdapterSendError,
    BaseAdapter,
)
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.core.events.canonical import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult

_logger = logging.getLogger(__name__)

# Maximum history size for fake adapter tracking lists.
_MAX_FAKE_HISTORY: int = 1000


def _trim(lst: list[Any], maxsize: int = _MAX_FAKE_HISTORY) -> None:
    """Evict oldest entries from *lst* when it exceeds *maxsize*."""
    if len(lst) > maxsize:
        excess = len(lst) - maxsize
        del lst[:excess]
        _logger.warning(
            "Fake adapter history trimmed %d oldest entries (cap=%d)",
            excess, maxsize,
        )


class FakeMeshtasticClient:
    """Deterministic fake Meshtastic client for testing outbound delivery.

    Tracks every ``send_text`` call and returns sequential packet IDs
    so that tests can assert on deterministic native IDs.

    Attributes
    ----------
    sent_packets:
        List of dicts for each sent packet.
    sent_count:
        Number of packets sent.
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self.sent_packets: list[dict[str, Any]] = []
        self.sent_count: int = 0

    async def send_text(
        self,
        text: str,
        channel_index: int,
        meshnet_name: str = "",
        dest_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a text message and return a deterministic packet ID.

        Parameters
        ----------
        text:
            The text payload.
        channel_index:
            Target radio channel index.
        meshnet_name:
            Optional meshnet name (unused by fake).
        dest_id:
            Optional destination node ID for DMs.

        Returns
        -------
        dict
            ``{"packet_id": <int>}`` with a sequential ID.
        """
        packet_id = self._next_id
        self._next_id += 1
        self.sent_packets.append({
            "text": text,
            "channel_index": channel_index,
            "meshnet_name": meshnet_name,
            "dest_id": dest_id,
            "packet_id": packet_id,
        })
        _trim(self.sent_packets)
        self.sent_count += 1
        return {"packet_id": packet_id}


# Default capabilities for the fake Meshtastic adapter.
_FAKE_MESHTASTIC_CAPABILITIES = AdapterCapabilities(
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


class FakeMeshtasticAdapter(BaseAdapter):
    """Simulated Meshtastic transport adapter for testing.

    **Rendering Boundary**: this adapter consumes :class:`RenderingResult`
    objects and must **not** contain event-kind-specific formatting logic.
    All rendering is performed upstream by renderers; the adapter merely
    stores and delivers the pre-rendered payload.

    Stores every outbound event delivered via :meth:`deliver` and every
    inbound event published via :meth:`simulate_inbound` in public lists
    that test code can inspect.

    Parameters
    ----------
    config:
        A :class:`MeshtasticConfig` instance.  Defaults to a fake config.

    Attributes
    ----------
    delivered_payloads:
        :class:`RenderingResult` payloads stored for test inspection.
    inbound_events:
        Events published inbound via :meth:`simulate_inbound`.
    ctx:
        The :class:`AdapterContext` injected by :meth:`start`, or
        ``None`` if the adapter has not been started.
    """

    adapter_id: str
    platform: str = "meshtastic"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(
        self,
        config: MeshtasticConfig | None = None,
        *,
        adapter_id: str | None = None,
    ) -> None:
        if config is None:
            if adapter_id is None:
                adapter_id = "fake_meshtastic"
            config = MeshtasticConfig(adapter_id=adapter_id)
        self._config = config
        self.adapter_id = config.adapter_id
        self.ctx: AdapterContext | None = None
        self.delivered_payloads: list[RenderingResult] = []
        self.inbound_events: list[CanonicalEvent] = []
        self._started: bool = False
        self._codec = MeshtasticCodec(config.adapter_id, config)
        self._classifier = MeshtasticPacketClassifier(config)
        self._fake_client = FakeMeshtasticClient()
        self._deliver_failure: bool = False

    @property
    def fake_client(self) -> FakeMeshtasticClient:
        """The underlying fake client for test inspection."""
        return self._fake_client

    def set_deliver_failure(self, fail: bool = True) -> None:
        """Configure the adapter to raise on the next ``deliver()`` call.

        Useful for testing pipeline error handling.
        """
        self._deliver_failure = fail

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        self.ctx = ctx
        self._started = True
        ctx.logger.info("FakeMeshtasticAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "FakeMeshtasticAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=_FAKE_MESHTASTIC_CAPABILITIES,
            health="healthy" if self._started else "unknown",
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`TypeError`, enforcing
        the rendering boundary at the adapter level.

        Uses the internal :class:`FakeMeshtasticClient` to generate
        deterministic packet IDs.

        Parameters
        ----------
        result:
            The rendering result to deliver.

        Returns
        -------
        AdapterDeliveryResult
            Contains the deterministic native_message_id and
            native_channel_id from the fake client.

        Raises
        ------
        TypeError
            If *result* is not a :class:`RenderingResult`.
        MeshtasticSendError
            If ``set_deliver_failure(True)`` was called.
        """
        if not isinstance(result, RenderingResult):
            raise TypeError(
                f"FakeMeshtasticAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        if self._deliver_failure:
            raise AdapterSendError("FakeMeshtasticAdapter: simulated send failure", transient=True)

        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)

        text = str(result.payload.get("text", ""))
        channel_index = result.payload.get("channel_index", 0)
        if not isinstance(channel_index, int):
            channel_index = 0
        meshnet_name = str(result.payload.get("meshnet_name", ""))

        send_result = await self._fake_client.send_text(
            text=text,
            channel_index=channel_index,
            meshnet_name=meshnet_name,
        )
        packet_id = send_result["packet_id"]

        return AdapterDeliveryResult(
            native_message_id=str(packet_id),
            native_channel_id=str(channel_index),
        )

    # -- Inbound simulation -------------------------------------------------

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound Meshtastic packet.

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
        self.inbound_events.append(canonical)
        _trim(self.inbound_events)

    # -- Test helpers -------------------------------------------------------

    def make_text_event(
        self,
        body: str = "hello",
        sender: str = "!default",
        channel: int = 0,
        packet_id: int = 12345,
    ) -> CanonicalEvent:
        """Create a minimal :class:`CanonicalEvent` from Meshtastic-like
        packet data by constructing a fake packet and decoding it.

        Parameters
        ----------
        body:
            Body text for the event payload.
        sender:
            Sender node ID.
        channel:
            Radio channel index.
        packet_id:
            Packet ID.

        Returns
        -------
        CanonicalEvent
            A ready-to-publish canonical event.
        """
        packet = {
            "fromId": sender,
            "toId": "",
            "channel": channel,
            "id": packet_id,
            "decoded": {
                "portnum": "text_message",
                "text": body,
            },
        }
        return self._codec.decode(packet)

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
