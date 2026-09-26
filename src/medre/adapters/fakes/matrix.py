"""Fake Matrix adapter for testing.

:class:`FakeMatrixAdapter` simulates a Matrix presentation adapter
without any real network or ``mindroom-nio`` dependency.  It mirrors
:class:`~medre.adapters.matrix.adapter.MatrixAdapter` delivery semantics
closely enough for unit and integration tests.

Capabilities
------------
* text messaging
* native replies, reactions, threads, edits, and deletes
* delivery receipts
* no attachments

Outbound operations
-------------------
``deliver`` consumes the same closed ``_matrix_operation`` envelope as
the real adapter (``send_event`` / ``redact_event``; including
redaction support).  Envelope-bearing payloads are strictly validated
and recorded in :attr:`sent_operations`; a redaction produces its own
deterministic ``$fake_redact_*`` native event id so its native ref
records to the mutation event, never to the redacted message.
Plain content payloads without an envelope (hand-authored test results
and generic renderer outputs) are accepted as simple ``m.room.message``
sends, mirroring the pre-envelope fake contract downstream tests rely
on.

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

import logging
import uuid
from typing import Any

from medre.adapters.matrix.outbound import (
    MatrixOutboundEnvelopeError,
    MatrixOutboundOperation,
)
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterHandoffResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
)
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
            excess,
            maxsize,
        )


# Default capabilities for the fake Matrix adapter.
_FAKE_MATRIX_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    threads="native",
    reactions="native",
    edits="native",
    deletes="native",
    attachments=False,
    metadata_fields=False,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    topic_rooms=True,
)


class FakeMatrixAdapter(AdapterContract):
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
        (canonical-event path).
    delivered_payloads:
        :class:`RenderingResult` payloads stored for test inspection.
    sent_operations:
        Parsed :class:`MatrixOutboundOperation` envelopes delivered via
        :meth:`deliver`, in order (empty for plain-content payloads).
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
        super().__init__()
        self.adapter_id = adapter_id
        self._channel: str = channel
        self.ctx: AdapterContext | None = None
        self.received_events: list[CanonicalEvent] = []
        self.delivered_payloads: list[RenderingResult] = []
        self.sent_operations: list[MatrixOutboundOperation] = []
        self.inbound_events: list[CanonicalEvent] = []
        self._started: bool = False

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Store the context and mark the adapter as started."""
        self.ctx = ctx
        self._mark_started(ctx)
        self._started = True
        ctx.logger.info("FakeMatrixAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Mark the adapter as stopped."""
        self._started = False
        if self.ctx is not None:
            self.ctx.logger.info("FakeMatrixAdapter %s stopped", self.adapter_id)

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
        }

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        """Accept an outbound rendered operation for delivery.

        This adapter consumes :class:`RenderingResult` objects.  Passing a
        raw :class:`CanonicalEvent` raises :class:`AdapterPermanentError`, enforcing
        the rendering boundary at the adapter level.

        Envelope-bearing payloads (``_matrix_operation``) are strictly
        validated exactly like the real adapter — a malformed envelope
        raises :class:`AdapterPermanentError`.  ``send_event`` yields the
        classic ``$fake_<event_id>`` native id; ``redact_event`` yields
        ``$fake_redact_<event_id>`` (the redaction event's own id, never
        the redacted message's).  Every parsed operation is recorded in
        :attr:`sent_operations`.

        Plain content payloads without an envelope are accepted as
        simple sends so hand-authored downstream tests keep working.

        Parameters
        ----------
        result:
            The rendering result to deliver.

        Returns
        -------
        AdapterHandoffResult
            Native delivery metadata with a deterministic Matrix-like
            event ID derived from the rendering result's ``event_id``.

        Raises
        ------
        AdapterPermanentError
            If *result* is not a :class:`RenderingResult`, or the payload
            carries a malformed ``_matrix_operation`` envelope.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"FakeMatrixAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )
        try:
            operation = MatrixOutboundOperation.from_payload(result.payload)
        except MatrixOutboundEnvelopeError as exc:
            raise AdapterPermanentError(
                f"invalid Matrix outbound operation envelope: {exc}"
            ) from exc

        if operation is not None:
            self.sent_operations.append(operation)
            _trim(self.sent_operations)

        self.delivered_payloads.append(result)
        _trim(self.delivered_payloads)

        if operation is not None and operation.kind == "redact_event":
            # A redaction's native ref records to its own canonical
            # mutation event — never to the redacted message.
            fake_event_id = f"$fake_redact_{result.event_id}"
        else:
            # Deterministic Matrix-like event ID for test verification.
            fake_event_id = f"$fake_{result.event_id}"
        channel_id = result.target_channel or None
        return AdapterHandoffResult(
            native_message_id=fake_event_id,
            native_channel_id=channel_id,
            confirmation_level="remote_service",
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
        if not self._started:
            return
        await self.publish_inbound(event)
        self.inbound_events.append(event)
        _trim(self.inbound_events)

    def make_event(
        self,
        text: str = "hello",
        event_kind: str = EventKind.MESSAGE_CREATED,
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
