"""Fake Matrix adapter for testing.

:class:`FakeMatrixAdapter` simulates a Matrix presentation adapter
without any real network or ``mindroom-nio`` dependency.  It mirrors
:class:`~medre.adapters.fake_presentation.FakePresentationAdapter`
precisely and is intended solely for use in unit and integration tests.

Capabilities
------------
* text messaging
* native replies and reactions
* delivery receipts
* no attachments, edits, or deletes

Usage
-----
>>> adapter = FakeMatrixAdapter("test_matrix")
>>> await adapter.start(ctx)
>>> # Deliver an outbound rendered payload
>>> await adapter.deliver(result)
>>> assert result in adapter.delivered_payloads
>>> # Simulate a user typing a message in a Matrix room
>>> event = adapter.make_event("Hello from Matrix!")
>>> await adapter.simulate_inbound(event)
"""
from __future__ import annotations

import uuid
from typing import Any

from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
)
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)

# Default capabilities for the fake Matrix adapter.
_FAKE_MATRIX_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=True,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    async_delivery=True,
    topic_rooms=True,
)


class FakeMatrixAdapter(BaseAdapter):
    """Simulated Matrix presentation adapter for testing.

    **Rendering Boundary**: this adapter consumes :class:`RenderingResult`
    objects and must **not** contain event-kind-specific formatting logic.
    All rendering is performed upstream by renderers; the adapter merely
    stores and delivers the pre-rendered payload.

    Stores every outbound event delivered via :meth:`deliver` and every
    inbound event published via :meth:`simulate_inbound` in public lists
    that test code can inspect.

    Parameters
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    channel:
        Default channel / room identifier used for inbound simulation.

    Attributes
    ----------
    received_events:
        Events delivered outbound to this adapter via :meth:`deliver`
        (legacy canonical-event path).
    delivered_payloads:
        :class:`RenderingResult` payloads stored for test inspection.
    inbound_events:
        Events published inbound via :meth:`simulate_inbound`.
    ctx:
        The :class:`AdapterContext` injected by :meth:`start`, or
        ``None`` if the adapter has not been started.
    """

    adapter_id: str
    platform: str = "matrix"
    role: AdapterRole = AdapterRole.PRESENTATION

    def __init__(
        self,
        adapter_id: str = "fake_matrix",
        channel: str = "test_matrix_room",
    ) -> None:
        self.adapter_id = adapter_id
        self._channel: str = channel
        self.ctx: AdapterContext | None = None
        self.received_events: list[CanonicalEvent] = []
        self.delivered_payloads: list[RenderingResult] = []
        self.inbound_events: list[CanonicalEvent] = []
        self._started: bool = False

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        self.ctx = ctx
        self._started = True
        ctx.logger.info("FakeMatrixAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info(
                "FakeMatrixAdapter %s stopped", self.adapter_id
            )

    async def health_check(self) -> AdapterInfo:
        """Return a healthy :class:`AdapterInfo` snapshot."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=_FAKE_MATRIX_CAPABILITIES,
            health="healthy" if self._started else "unknown",
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Accept an outbound rendered payload for delivery.

        This adapter consumes :class:`RenderingResult` only.  Passing a
        raw :class:`CanonicalEvent` raises :class:`TypeError`, enforcing
        the rendering boundary at the adapter level.

        Parameters
        ----------
        result:
            The rendering result to deliver.

        Returns
        -------
        AdapterDeliveryResult
            Native delivery metadata with a deterministic Matrix-like
            event ID derived from the rendering result's ``event_id``.

        Raises
        ------
        TypeError
            If *result* is not a :class:`RenderingResult`.
        """
        if not isinstance(result, RenderingResult):
            raise TypeError(
                f"FakeMatrixAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )
        self.delivered_payloads.append(result)
        # Deterministic Matrix-like event ID for test verification.
        fake_event_id = f"$fake_{result.event_id}"
        channel_id = result.target_channel or ""
        return AdapterDeliveryResult(
            native_message_id=fake_event_id,
            native_channel_id=channel_id,
        )

    # -- Test helpers -------------------------------------------------------

    async def simulate_inbound(self, event: CanonicalEvent) -> None:
        """Publish an event into the framework's inbound stream.

        Simulates a user sending a message in a Matrix room.

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
        self.inbound_events.append(event)

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
            Override the default channel.
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
            lineage=(),
            relations=(),
            payload={"body": text, **extra_payload},
            metadata=EventMetadata(),
        )

    def make_reply_event(
        self,
        target: CanonicalEvent,
        text: str = "reply",
        channel: str | None = None,
    ) -> CanonicalEvent:
        """Create a :class:`CanonicalEvent` that replies to *target*.

        Parameters
        ----------
        target:
            The event to reply to.
        text:
            Body text for the reply.
        channel:
            Override the default channel.

        Returns
        -------
        CanonicalEvent
            A canonical event with a reply relation.
        """
        ch = channel or self._channel
        reply = self.make_event(text=text, channel=ch)
        relation = EventRelation(
            relation_type="reply",
            target_event_id=target.event_id,
            target_native_ref=NativeRef(
                adapter=self.adapter_id,
                native_channel_id=target.source_channel_id,
                native_message_id=target.event_id,
            ),
            key=None,
            fallback_text=None,
        )
        return CanonicalEvent(
            event_id=reply.event_id,
            event_kind=reply.event_kind,
            schema_version=reply.schema_version,
            timestamp=reply.timestamp,
            source_adapter=reply.source_adapter,
            source_transport_id=reply.source_transport_id,
            source_channel_id=reply.source_channel_id,
            parent_event_id=reply.parent_event_id,
            lineage=reply.lineage,
            relations=(relation,),
            payload=reply.payload,
            metadata=reply.metadata,
        )

    def make_reaction_event(
        self,
        target: CanonicalEvent,
        emoji: str = "👍",
        channel: str | None = None,
    ) -> CanonicalEvent:
        """Create a :class:`CanonicalEvent` that reacts to *target*.

        Parameters
        ----------
        target:
            The event to react to.
        emoji:
            The emoji to react with.
        channel:
            Override the default channel.

        Returns
        -------
        CanonicalEvent
            A canonical event with a reaction relation.
        """
        ch = channel or self._channel
        reaction = self.make_event(
            text=emoji, event_kind=EventKind.MESSAGE_REACTED, channel=ch
        )
        relation = EventRelation(
            relation_type="reaction",
            target_event_id=target.event_id,
            target_native_ref=NativeRef(
                adapter=self.adapter_id,
                native_channel_id=target.source_channel_id,
                native_message_id=target.event_id,
            ),
            key=emoji,
            fallback_text=None,
        )
        return CanonicalEvent(
            event_id=reaction.event_id,
            event_kind=reaction.event_kind,
            schema_version=reaction.schema_version,
            timestamp=reaction.timestamp,
            source_adapter=reaction.source_adapter,
            source_transport_id=reaction.source_transport_id,
            source_channel_id=reaction.source_channel_id,
            parent_event_id=reaction.parent_event_id,
            lineage=reaction.lineage,
            relations=(relation,),
            payload=reaction.payload,
            metadata=reaction.metadata,
        )

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has been called without a corresponding
        :meth:`stop`."""
        return self._started
