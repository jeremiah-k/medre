"""Fake Meshtastic adapter for testing.

:class:`FakeMeshtasticAdapter` simulates a Meshtastic transport adapter
without any real radio or ``mtjk`` dependency.  It mirrors the real
:class:`~medre.adapters.meshtastic.adapter.MeshtasticAdapter` and is
intended solely for use in unit and integration tests.

Capabilities
------------
* text, reply, and reaction messaging
* no edits, deletes, attachments, or delivery receipts
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
import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
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
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.policies.startup_backlog_suppress import (
    should_suppress_startup_backlog,
)
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
        dest_id: str | None = None,
        reply_id: int | None = None,
        emoji: int | None = None,
    ) -> dict[str, Any]:
        """Send a text message and return a deterministic packet ID.

        Parameters
        ----------
        text:
            The text payload.
        channel_index:
            Target radio channel index.
        dest_id:
            Optional destination node ID for DMs.
        reply_id:
            Optional native reply/tapback target packet ID.
        emoji:
            Optional emoji flag (1 for tapback reactions).

        Returns
        -------
        dict
            ``{"packet_id": <int>}`` with a sequential ID.
        """
        packet_id = self._next_id
        self._next_id += 1
        record: dict[str, Any] = {
            "text": text,
            "channel_index": channel_index,
            "dest_id": dest_id,
            "packet_id": packet_id,
        }
        if reply_id is not None:
            record["reply_id"] = reply_id
        if emoji is not None:
            record["emoji"] = emoji
        self.sent_packets.append(record)
        _trim(self.sent_packets)
        self.sent_count += 1
        return {
            "packet_id": packet_id,
            "channel": channel_index,
            "reply_id": reply_id,
            "emoji": emoji,
        }


# Default capabilities for the fake Meshtastic adapter.
# Derives max_text_bytes from MeshtasticConfig to avoid duplicating the default.
_FAKE_MESHTASTIC_CAPABILITIES = AdapterCapabilities(
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
    max_text_bytes=MeshtasticConfig.max_text_bytes,
    max_text_chars=None,
)


class FakeMeshtasticAdapter(AdapterContract):
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
        super().__init__()
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
        self._adapter_start_epoch: float | None = None
        self._startup_backlog_packets_seen: int = 0
        self._startup_backlog_packets_suppressed: int = 0
        # Outbound gate suppression counter
        self._outbound_gate_suppressed: int = 0
        # Build per-config capabilities matching the real adapter pattern.
        # NOTE: uses config.max_text_bytes (per-instance), not the module-level
        # _FAKE_MESHTASTIC_CAPABILITIES constant (which uses the class default).
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
            max_text_bytes=config.max_text_bytes,
            max_text_chars=None,
        )

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
        self._mark_started(ctx)
        self._started = True
        self._adapter_start_epoch = time.time()
        self._startup_backlog_packets_seen = 0
        self._startup_backlog_packets_suppressed = 0
        ctx.logger.info("FakeMeshtasticAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info("FakeMeshtasticAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities,
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
            "delivered_count": len(self.delivered_payloads),
            "inbound_count": len(self.inbound_events),
            "startup_backlog_packets_seen": self._startup_backlog_packets_seen,
            "startup_backlog_packets_suppressed": self._startup_backlog_packets_suppressed,
            "startup_backlog_suppress_seconds": self._config.startup_backlog_suppress_seconds,
            "adapter_start_epoch": self._adapter_start_epoch,
            # Outbound gate
            "outbound_mode": self._config.outbound_mode,
            "outbound_gate_suppressed": self._outbound_gate_suppressed,
        }

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`AdapterPermanentError`, enforcing
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
        AdapterPermanentError
            If *result* is not a :class:`RenderingResult`, or if
            ``outbound_mode`` is ``"listen_only"``.
        AdapterSendError
            If ``set_deliver_failure(True)`` was called.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"FakeMeshtasticAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Outbound gate: suppress radio sends when listen_only.
        # Checked before _deliver_failure so listen_only always wins (mirrors
        # real adapter where listen_only is checked immediately after type
        # validation).
        if self._config.outbound_mode == "listen_only":
            self._outbound_gate_suppressed += 1
            raise AdapterPermanentError("outbound suppressed: listen_only mode")

        if self._deliver_failure:
            raise AdapterSendError(
                "FakeMeshtasticAdapter: simulated send failure", transient=True
            )

        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)

        text = str(result.payload.get("text", ""))
        channel_index = result.payload.get("channel_index", 0)
        if not isinstance(channel_index, int):
            channel_index = 0
        reply_id = result.payload.get("reply_id")
        if isinstance(reply_id, int):
            reply_id_val: int | None = reply_id
        else:
            reply_id_val = None
        emoji_val = result.payload.get("emoji")
        if not isinstance(emoji_val, int):
            emoji_val = None

        send_result = await self._fake_client.send_text(
            text=text,
            channel_index=channel_index,
            reply_id=reply_id_val,
            emoji=emoji_val,
        )
        packet_id = send_result["packet_id"]

        result_metadata: dict[str, object] = {}
        meshtastic_meta: dict[str, object] = {}
        if reply_id_val is not None:
            meshtastic_meta["reply_id"] = reply_id_val
        if emoji_val is not None:
            meshtastic_meta["emoji"] = emoji_val
        meshtastic_meta["packet_id"] = packet_id
        meshtastic_meta["channel"] = channel_index
        # Inner transport-namespaced dict stays a plain dict; the outer
        # MappingProxyType is the contract-level immutability boundary.
        # The pipeline's _normalize_mapping handles any nested
        # MappingProxyType at the persistence boundary, so nested
        # MappingProxyType is safe — but keeping the inner dict plain
        # avoids unnecessary wrapping here.
        result_metadata["meshtastic"] = meshtastic_meta

        return AdapterDeliveryResult(
            native_message_id=str(packet_id),
            native_channel_id=str(channel_index),
            metadata=MappingProxyType(result_metadata),
        )

    # -- Inbound simulation -------------------------------------------------

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound Meshtastic packet.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.  Applies startup backlog suppression
        using the same shared utilities as the real adapter.

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
        if not self._started:
            return

        classification = self._classifier.classify(packet, own_node_id=None)
        if classification.action != "relay":
            return

        # Startup backlog suppression gate (mirrors real adapter)
        self._startup_backlog_packets_seen += 1
        window = self._config.startup_backlog_suppress_seconds
        if window > 0 and self._adapter_start_epoch is not None:
            packet_time = extract_meshtastic_rx_time(packet)
            adapter_start = datetime.fromtimestamp(
                self._adapter_start_epoch, tz=timezone.utc
            )
            if should_suppress_startup_backlog(
                packet_time, adapter_start, float(window)
            ):
                self._startup_backlog_packets_suppressed += 1
                return

        canonical = self._codec.decode(packet)
        await self.publish_inbound(canonical)
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
