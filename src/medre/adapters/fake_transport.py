"""Fake transport adapter for testing.

:class:`FakeTransportAdapter` simulates a radio/mesh transport adapter
without any real hardware or network dependency.  It is intended solely
for use in unit and integration tests.

Capabilities
------------
* text messaging (up to 200 characters)
* native replies
* fallback reactions
* no attachments, edits, deletes, or delivery receipts

Usage
-----
>>> adapter = FakeTransportAdapter("test_transport")
>>> await adapter.start(ctx)
>>> await adapter.simulate_inbound(event)
>>> assert event in adapter.delivered_events
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)

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


# Default capabilities for the fake transport.
_FAKE_TRANSPORT_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    reactions="fallback",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_chars=200,
)


class FakeTransportAdapter(AdapterContract):
    """Simulated transport adapter for testing.

    **Canonical Event Immutability**: this adapter must **not** mutate
    canonical events after creation.  :class:`CanonicalEvent` is a frozen
    ``msgspec.Struct``; any attempt to set fields will raise at runtime.
    The adapter stores a snapshot of every event at creation time in
    :attr:`event_snapshots` so tests can verify no mutation occurred.

    **Rendering Boundary**: this adapter receives :class:`RenderingResult`
    via :meth:`deliver` and must **not** contain event-kind-specific
    formatting logic.  All rendering is performed upstream by renderers;
    the adapter merely stores the pre-rendered payload.

    Stores every event delivered via :meth:`simulate_inbound` and
    every rendered payload received via :meth:`deliver` in public lists
    that test code can inspect.

    Parameters
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    channel:
        Default channel identifier used for inbound simulation
        (stored as ``source_channel_id`` on produced events).

    Attributes
    ----------
    delivered_events:
        Events that were published inbound via :meth:`simulate_inbound`.
    delivered_payloads:
        :class:`RenderingResult` payloads received via :meth:`deliver`.
        Tests can inspect this to verify the adapter received a rendered
        result (not a raw canonical event).
    received_events:
        Events delivered outbound to this adapter.
    event_snapshots:
        Frozen snapshots of events at creation time, keyed by
        ``event_id``, used to verify that canonical events are never
        mutated after creation.
    ctx:
        The :class:`AdapterContext` injected by :meth:`start`, or
        ``None`` if the adapter has not been started.
    """

    adapter_id: str
    platform: str = "fake_transport"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(
        self,
        adapter_id: str = "fake_transport",
        channel: str = "test_channel",
    ) -> None:
        super().__init__()
        self.adapter_id = adapter_id
        self._channel: str = channel
        self.ctx: AdapterContext | None = None
        self.delivered_events: list[CanonicalEvent] = []
        self.delivered_payloads: list[RenderingResult] = []
        self.received_events: list[CanonicalEvent] = []
        self.event_snapshots: dict[str, CanonicalEvent] = {}
        self._started: bool = False

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        self.ctx = ctx
        self._mark_started(ctx)
        self._started = True
        ctx.logger.info("FakeTransportAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info("FakeTransportAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=_FAKE_TRANSPORT_CAPABILITIES,
            health="healthy" if self._started else "unknown",
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept a pre-rendered payload for delivery.

        This adapter does **not** perform event-kind-specific formatting.
        The :class:`RenderingResult` is stored in :attr:`delivered_payloads`
        for test inspection, proving the rendering boundary is respected.
        Returns an :class:`AdapterDeliveryResult` with a deterministic
        native ID.

        Parameters
        ----------
        result:
            The rendered payload to deliver.

        Returns
        -------
        AdapterDeliveryResult | None
            Native delivery metadata.
        """
        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)
        return AdapterDeliveryResult(
            native_message_id=f"fake-transport-{result.event_id}",
            native_channel_id=result.target_channel,
        )

    # -- Test helpers -------------------------------------------------------

    async def simulate_inbound(self, event: CanonicalEvent) -> None:
        """Publish an event into the framework's inbound stream.

        This simulates the adapter receiving a message from the radio
        transport and converting it into a canonical event.

        The event is appended to :attr:`delivered_events` for later
        test inspection.

        Parameters
        ----------
        event:
            The canonical event to publish inbound.

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
        await self.publish_inbound(event)
        self.delivered_events.append(event)
        _trim(self.delivered_events)

    def make_event(
        self,
        text: str = "hello",
        event_kind: str = EventKind.MESSAGE_TEXT,
        channel: str | None = None,
        **extra_payload: object,
    ) -> CanonicalEvent:
        """Create a minimal :class:`CanonicalEvent` for testing.

        Parameters
        ----------
        text:
            Body text for the event payload.
        event_kind:
            The event kind string.
        channel:
            Override the default channel; defaults to the channel
            supplied at construction.
        **extra_payload:
            Additional keys merged into the payload dict.

        Returns
        -------
        CanonicalEvent
            A ready-to-publish canonical event.
        """
        from datetime import datetime, timezone

        from medre.core.events.metadata import EventMetadata

        ch = channel or self._channel
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=self.adapter_id,
            source_transport_id=self.adapter_id,
            source_channel_id=ch,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": text, **extra_payload},
            metadata=EventMetadata(),
        )
        # Snapshot at creation time for immutability verification.
        self.event_snapshots[event.event_id] = event
        # Trim snapshots dict if it exceeds the cap.
        if len(self.event_snapshots) > _MAX_FAKE_HISTORY:
            keys = list(self.event_snapshots.keys())
            excess = len(keys) - _MAX_FAKE_HISTORY
            for k in keys[:excess]:
                del self.event_snapshots[k]
            _logger.warning(
                "FakeTransportAdapter event_snapshots trimmed %d entries (cap=%d)",
                excess, _MAX_FAKE_HISTORY,
            )
        return event

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
