"""Fake mode LXMF adapter for testing.

:class:`FakeLxmfAdapter` simulates an LXMF transport adapter
without any real Reticulum or LXMF dependency.  It mirrors the real
:class:`~medre.adapters.lxmf.adapter.LxmfAdapter` and is
intended solely for use in unit and integration tests.

Capabilities
------------
* text messaging with title support
* metadata fields via LXMF fields dict
* no replies, reactions, edits, deletes, attachments, or delivery receipts
* packet classification and codec decoding (via real classifier + codec)

Usage
-----
>>> config = LxmfConfig(adapter_id="test_lxmf")
>>> adapter = FakeLxmfAdapter(config)
>>> await adapter.start(ctx)
>>> # Simulate an inbound LXMF text message
>>> event = adapter.make_text_event("Hello from LXMF!")
>>> await adapter.simulate_inbound(packet_dict)
>>> # Deliver an outbound rendered payload
>>> delivery = await adapter.deliver(result)
>>> assert adapter.delivered_payloads
"""
from __future__ import annotations

import hashlib
import logging
from types import MappingProxyType
from typing import Any

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
from medre.adapters.lxmf.codec import LxmfCodec
from medre.config.adapters.lxmf import LxmfConfig
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.core.events.canonical import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult

_logger = logging.getLogger(__name__)

# Maximum history size for fake adapter tracking lists.
# Prevents unbounded growth in long-running soak tests.
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


class FakeLxmfClient:
    """Deterministic fake LXMF client for testing outbound delivery.

    Tracks every ``send_text`` call and returns sequential message IDs
    (SHA-256 hex of a counter) so that tests can assert on deterministic
    native IDs.

    Attributes
    ----------
    sent_messages:
        List of dicts for each sent message.
    sent_count:
        Number of messages sent.
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_count: int = 0

    async def send_text(
        self,
        text: str,
        title: str = "",
        fields: dict | None = None,
        destination_hash: str = "",
    ) -> dict[str, Any]:
        """Send a text message and return a deterministic message ID.

        Parameters
        ----------
        text:
            The text payload.
        title:
            Optional message title.
        fields:
            Optional fields dict.
        destination_hash:
            Destination address hash.

        Returns
        -------
        dict
            ``{"message_id": <hex string>}`` with a deterministic SHA-256
            based ID derived from the sequential counter.
        """
        counter = self._next_id
        self._next_id += 1
        # Generate deterministic hex ID that looks like a real SHA-256 hash
        raw = f"lxmf-fake-{counter}".encode()
        message_id = hashlib.sha256(raw).hexdigest()

        self.sent_messages.append({
            "text": text,
            "title": title,
            "fields": fields,
            "destination_hash": destination_hash,
            "message_id": message_id,
            "counter": counter,
        })
        _trim(self.sent_messages)
        self.sent_count += 1
        return {"message_id": message_id}


# Default capabilities for the fake LXMF adapter.
_FAKE_LXMF_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=True,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    delivery_receipts=False,
    store_and_forward=True,
    direct_messages=True,
    channels=False,
    async_delivery=True,
    identity_encryption=True,
    mesh_routing=True,
    max_text_bytes=None,
    max_text_chars=16384,
)


class FakeLxmfAdapter(AdapterContract):
    """Simulated LXMF transport adapter for testing.

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
        A :class:`LxmfConfig` instance.  Defaults to a fake config.

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
    platform: str = "lxmf"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(
        self,
        config: LxmfConfig | None = None,
        *,
        adapter_id: str | None = None,
    ) -> None:
        super().__init__()
        if config is None:
            if adapter_id is None:
                adapter_id = "fake_lxmf"
            config = LxmfConfig(adapter_id=adapter_id)
        self._config = config
        self.adapter_id = config.adapter_id
        self.ctx: AdapterContext | None = None
        self.delivered_payloads: list[RenderingResult] = []
        self.inbound_events: list[CanonicalEvent] = []
        self._started: bool = False
        self._codec = LxmfCodec(config.adapter_id, config)
        self._classifier = LxmfPacketClassifier(config)
        self._fake_client = FakeLxmfClient()
        self._deliver_failure: bool = False

    @property
    def fake_client(self) -> FakeLxmfClient:
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
        ctx.logger.info("FakeLxmfAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "FakeLxmfAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=_FAKE_LXMF_CAPABILITIES,
            health="healthy" if self._started else "unknown",
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return adapter-level diagnostics.

        No secrets, private keys, identity material, or raw RNS/LXMF
        objects are exposed.  Parity with
        :meth:`~medre.adapters.lxmf.adapter.LxmfAdapter.diagnostics`.
        """
        return {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "mode": "fake",
            "sent_count": self._fake_client.sent_count,
            "delivered_count": len(self.delivered_payloads),
            "inbound_count": len(self.inbound_events),
        }

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`AdapterPermanentError`, enforcing
        the rendering boundary at the adapter level.

        Uses the internal :class:`FakeLxmfClient` to generate
        deterministic message IDs.

        Parameters
        ----------
        result:
            The rendering result to deliver.

        Returns
        -------
        AdapterDeliveryResult
            Contains the deterministic native_message_id from the fake
            client.

        Raises
        ------
        AdapterPermanentError
            If *result* is not a :class:`RenderingResult`.
        AdapterSendError
            If ``set_deliver_failure(True)`` was called.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"FakeLxmfAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        if self._deliver_failure:
            raise AdapterSendError("FakeLxmfAdapter: simulated send failure", transient=True)

        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)

        text = str(result.payload.get("content", ""))
        title = str(result.payload.get("title", ""))
        fields = result.payload.get("fields")
        dest_hash = str(result.payload.get("destination_hash", ""))

        send_result = await self._fake_client.send_text(
            text=text,
            title=title,
            fields=fields if isinstance(fields, dict) else None,
            destination_hash=dest_hash,
        )
        message_id = send_result["message_id"]

        delivery_method = result.payload.get("delivery_method")
        if not isinstance(delivery_method, str):
            delivery_method = self._config.default_delivery_method

        return AdapterDeliveryResult(
            native_message_id=message_id,
            native_channel_id=None,
            metadata=MappingProxyType({
                "lxmf": {
                    "delivery_state": "outbound",
                    "delivery_method": delivery_method,
                },
            }),
        )

    # -- Inbound simulation -------------------------------------------------

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound LXMF message payload.

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
        await self.publish_inbound(canonical)
        self.inbound_events.append(canonical)
        _trim(self.inbound_events)

    # -- Test helpers -------------------------------------------------------

    def make_text_event(
        self,
        body: str = "hello",
        source_hash: str = "ab" * 16,
        msg_id: str | None = None,
        title: str = "",
    ) -> CanonicalEvent:
        """Create a minimal :class:`CanonicalEvent` from LXMF-like
        packet data by constructing a fake packet and decoding it.

        Parameters
        ----------
        body:
            Body text for the event payload.
        source_hash:
            Sender source_hash hex string.
        msg_id:
            Message ID hex string.
        title:
            Optional message title.

        Returns
        -------
        CanonicalEvent
            A ready-to-publish canonical event.
        """
        packet: dict[str, Any] = {
            "content": body,
            "source_hash": source_hash,
            "destination_hash": "00" * 16,
            "message_id": msg_id or "ff" * 32,
            "timestamp": 1700000000.0,
            "title": title,
            "fields": {},
            "signature_validated": True,
            "has_fields": False,
        }
        return self._codec.decode(packet)

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
