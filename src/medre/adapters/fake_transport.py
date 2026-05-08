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
import uuid
from typing import Any

from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
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
    max_text_chars=200,
)


class FakeTransportAdapter(BaseAdapter):
    """Simulated transport adapter for testing.

    Stores every event delivered via :meth:`simulate_inbound` and
    every event received via outbound delivery in public lists that
    test code can inspect.

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
    received_events:
        Events delivered outbound to this adapter (for future outbound
        delivery support).
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
        self.adapter_id = adapter_id
        self._channel: str = channel
        self.ctx: AdapterContext | None = None
        self.delivered_events: list[CanonicalEvent] = []
        self.received_events: list[CanonicalEvent] = []
        self._started: bool = False

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        self.ctx = ctx
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
        await self.ctx.publish_inbound(event)
        self.delivered_events.append(event)

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
        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=self.adapter_id,
            source_transport_id=self.adapter_id,
            source_channel_id=ch,
            parent_event_id=None,
            lineage=[],
            relations=[],
            payload={"body": text, **extra_payload},
            metadata=EventMetadata(),
        )

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
