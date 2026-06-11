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

import logging
from types import MappingProxyType
from typing import Any

from medre.adapters.meshcore.adapter import increment_classifier_counters
from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
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
from medre.core.events.canonical import CanonicalEvent
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
            excess,
            maxsize,
        )


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
        self.sent_packets.append(
            {
                "text": text,
                "channel_index": channel_index,
                "meshnet_name": meshnet_name,
                "dest_id": dest_id,
                "packet_id": packet_id,
            }
        )
        _trim(self.sent_packets)
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
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_bytes=512,
    max_text_chars=None,
)


class FakeMeshCoreAdapter(AdapterContract):
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
        *,
        adapter_id: str | None = None,
    ) -> None:
        super().__init__()
        if config is None:
            if adapter_id is None:
                adapter_id = "fake_meshcore"
            config = MeshCoreConfig(adapter_id=adapter_id)
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

        # Aggregate in-memory classifier counters (reset on restart).
        self._classifier_packets_seen: int = 0
        self._classifier_packets_relayed: int = 0
        self._classifier_packets_ignored: int = 0
        self._classifier_packets_dropped: int = 0
        self._classifier_packets_deferred: int = 0
        self._classifier_packets_ack_ignored: int = 0
        self._classifier_packets_empty_text_ignored: int = 0
        self._classifier_packets_unknown_deferred: int = 0
        self._classifier_packets_dm_relayed: int = 0
        self._classifier_packets_malformed: int = 0
        self._inbound_published: int = 0

        # Health lifecycle epoch (mirrors real adapter).
        self._health_lifecycle_epoch: int = 0

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
        self._classifier_packets_ack_ignored = 0
        self._classifier_packets_empty_text_ignored = 0
        self._classifier_packets_unknown_deferred = 0
        self._classifier_packets_dm_relayed = 0
        self._classifier_packets_malformed = 0
        self._inbound_published = 0

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        if self._started:
            return
        self._reset_inbound_counters()
        self._health_lifecycle_epoch += 1
        self.ctx = ctx
        self._mark_started(ctx)
        self._started = True
        ctx.logger.info("FakeMeshCoreAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        if not self._started:
            return
        self._started = False
        self._health_lifecycle_epoch += 1
        if self.ctx is not None:
            self.ctx.logger.info("FakeMeshCoreAdapter %s stopped", self.adapter_id)

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

    def diagnostics(self) -> dict[str, Any]:
        """Return a diagnostics snapshot mirroring real adapter shape.

        All values are JSON-safe primitives.  No SDK objects are exposed.
        """
        return {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "mode": "fake",
            "health_lifecycle_epoch": self._health_lifecycle_epoch,
            "delivered_count": len(self.delivered_payloads),
            "inbound_count": len(self.inbound_events),
            "classifier_packets_seen": self._classifier_packets_seen,
            "classifier_packets_relayed": self._classifier_packets_relayed,
            "classifier_packets_ignored": self._classifier_packets_ignored,
            "classifier_packets_dropped": self._classifier_packets_dropped,
            "classifier_packets_deferred": self._classifier_packets_deferred,
            "classifier_packets_ack_ignored": self._classifier_packets_ack_ignored,
            "classifier_packets_empty_text_ignored": self._classifier_packets_empty_text_ignored,
            "classifier_packets_unknown_deferred": self._classifier_packets_unknown_deferred,
            "classifier_packets_dm_relayed": self._classifier_packets_dm_relayed,
            "classifier_packets_malformed": self._classifier_packets_malformed,
            "inbound_published": self._inbound_published,
        }

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`AdapterPermanentError`, enforcing
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
        AdapterPermanentError
            If *result* is not a :class:`RenderingResult`.
        AdapterSendError
            If ``set_deliver_failure(True)`` was called.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"FakeMeshCoreAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        if self._deliver_failure:
            raise AdapterSendError(
                "FakeMeshCoreAdapter: simulated send failure", transient=True
            )

        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)

        text = str(result.payload.get("text", ""))
        channel_index = result.payload.get("channel_index", 0)
        if not isinstance(channel_index, int):
            channel_index = 0
        meshnet_name = str(result.payload.get("meshnet_name", ""))
        dest_id = result.payload.get("dest_id")
        if dest_id is not None:
            dest_id = str(dest_id)

        send_result = await self._fake_client.send_text(
            text=text,
            channel_index=channel_index,
            meshnet_name=meshnet_name,
            dest_id=dest_id,
        )
        packet_id = send_result["packet_id"]

        return AdapterDeliveryResult(
            native_message_id=str(packet_id),
            native_channel_id=str(channel_index),
            delivery_note="fake adapter — simulated local acceptance",
            metadata=MappingProxyType(
                {
                    # Nested MappingProxyType matches real adapter shape.
                    # NOTE: MappingProxyType is not directly JSON-serializable;
                    # consumers that persist metadata must cast via dict() first.
                    "meshcore": MappingProxyType({"local_acceptance": True}),
                }
            ),
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
        increment_classifier_counters(self, classification)

        # Gate: only relay action packets enter the codec pipeline
        if classification.action != "relay":
            return

        canonical = self._codec.decode(packet)
        await self.publish_inbound(canonical)
        self._inbound_published += 1
        self.inbound_events.append(canonical)
        _trim(self.inbound_events)

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
