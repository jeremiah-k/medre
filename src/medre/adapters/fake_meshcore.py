"""Fake MeshCore adapter for testing.

:class:`FakeMeshCoreAdapter` simulates a MeshCore transport adapter
without any real radio or MeshCore dependency.  It mirrors the real
:class:`~medre.adapters.meshcore.adapter.MeshCoreAdapter` and is
intended solely for use in unit and integration tests.

Capabilities
------------
* text messaging only
* no replies, reactions, edits, deletes, attachments, or delivery receipts
* packet classification and codec decoding (via real classifier + codec)

Usage
-----
>>> config = MeshCoreConfig(adapter_id="test_meshcore")
>>> adapter = FakeMeshCoreAdapter(config)
>>> await adapter.start(ctx)
>>> # Simulate an inbound MeshCore text packet
>>> event = adapter.make_text_event("Hello from meshcore!")
>>> await adapter.simulate_inbound(packet_dict)
>>> # Deliver an outbound rendered payload
>>> delivery = await adapter.deliver(result)
>>> assert adapter.delivered_payloads
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
from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.meshcore.errors import MeshCoreSendError
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
from medre.core.events.canonical import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult


class FakeMeshCoreClient:
    """Deterministic fake MeshCore client for testing outbound delivery.

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
            Target channel index.
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
        self.sent_count += 1
        return {"packet_id": packet_id}


# Default capabilities for the fake MeshCore adapter.
_FAKE_MESHCORE_CAPABILITIES = AdapterCapabilities(
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


class FakeMeshCoreAdapter(BaseAdapter):
    """Simulated MeshCore transport adapter for testing.

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
        A :class:`MeshCoreConfig` instance.  Defaults to a fake config.

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
    platform: str = "meshcore"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(
        self,
        config: MeshCoreConfig | None = None,
    ) -> None:
        if config is None:
            config = MeshCoreConfig(adapter_id="fake_meshcore")
        self._config = config
        self.adapter_id = config.adapter_id
        self.ctx: AdapterContext | None = None
        self.delivered_payloads: list[RenderingResult] = []
        self.inbound_events: list[CanonicalEvent] = []
        self._started: bool = False
        self._codec = MeshCoreCodec(config.adapter_id, config)
        self._classifier = MeshCorePacketClassifier(config)
        self._fake_client = FakeMeshCoreClient()
        self._deliver_failure: bool = False

    @property
    def fake_client(self) -> FakeMeshCoreClient:
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
        ctx.logger.info("FakeMeshCoreAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "FakeMeshCoreAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=_FAKE_MESHCORE_CAPABILITIES,
            health="healthy" if self._started else "unknown",
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`TypeError`, enforcing
        the rendering boundary at the adapter level.

        Uses the internal :class:`FakeMeshCoreClient` to generate
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
        MeshCoreSendError
            If ``set_deliver_failure(True)`` was called.
        """
        if not isinstance(result, RenderingResult):
            raise TypeError(
                f"FakeMeshCoreAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        if self._deliver_failure:
            raise MeshCoreSendError("FakeMeshCoreAdapter: simulated send failure")

        self.delivered_payloads.append(result)

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
        """Simulate an inbound MeshCore event payload.

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
        self.inbound_events.append(canonical)

    # -- Test helpers -------------------------------------------------------

    def make_text_event(
        self,
        body: str = "hello",
        sender: str = "abc123",
        channel: int = 0,
        packet_id: int = 12345,
    ) -> CanonicalEvent:
        """Create a minimal :class:`CanonicalEvent` from MeshCore-like
        packet data by constructing a fake packet and decoding it.

        Parameters
        ----------
        body:
            Body text for the event payload.
        sender:
            Sender pubkey_prefix.
        channel:
            Channel index (None for DMs).
        packet_id:
            Sender timestamp.

        Returns
        -------
        CanonicalEvent
            A ready-to-publish canonical event.
        """
        packet: dict[str, Any] = {
            "text": body,
            "pubkey_prefix": sender,
            "sender_timestamp": packet_id,
            "type": "CHAN",
            "txt_type": 0,
        }
        if channel is not None:
            packet["channel_idx"] = channel
        return self._codec.decode(packet)

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
