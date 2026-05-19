"""Core adapter contract types and abstract base class.

This module owns the adapter runtime contracts used by the core engine.
It defines the value types, protocols, and the abstract
:class:`AdapterContract` that every concrete adapter must implement.

Definitions:

* :class:`AdapterSendError` – base error raised by adapters when delivery fails.
* :class:`AdapterPermanentError` – permanent delivery error.
* :class:`AdapterDeliveryResult` – immutable result returned after successful delivery.
* :class:`AdapterRole` – the functional role of an adapter.
* :class:`AdapterCapabilities` – feature flags describing what an adapter supports.
* :class:`AdapterInfo` – runtime metadata about a running adapter instance.
* :class:`AdapterContext` – the runtime context injected into every adapter on start-up.
* :class:`AdapterCodec` – optional encode/decode helper that adapters may expose.
* :class:`AdapterContract` – abstract base class that every adapter must implement.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent
    from medre.core.rendering.renderer import RenderingResult


# ---------------------------------------------------------------------------
# Adapter error hierarchy
# ---------------------------------------------------------------------------


class AdapterSendError(Exception):
    """Base error raised by adapters when delivery fails.

    Carries a ``transient`` flag so that the delivery planning layer can
    classify the failure without inspecting exception type names.

    Subclasses and adapters should set ``transient=True`` (the default)
    for network / transport / timeout errors that may succeed on retry,
    and ``transient=False`` (or use :class:`AdapterPermanentError`) for
    config / auth / malformed-payload errors that will not self-correct.

    Attributes
    ----------
    transient:
        ``True`` if the error is retryable; ``False`` if permanent.
    """

    transient: bool

    def __init__(self, *args: object, transient: bool = True) -> None:
        self.transient = transient
        super().__init__(*args)


class AdapterPermanentError(AdapterSendError):
    """Permanent delivery error — retrying will not help.

    Use for config errors, authentication failures, malformed payloads,
    business-logic rejections, and any condition that requires human
    intervention to resolve.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args, transient=False)


# ---------------------------------------------------------------------------
# Adapter delivery result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterDeliveryResult:
    """Immutable result returned by adapters after successful delivery.

    Adapters populate this with platform-native IDs obtained from the
    external system.  The pipeline uses these IDs to store
    :class:`~medre.core.events.canonical.NativeMessageRef` mappings.
    The pipeline owns receipts and storage; adapters only report what
    the platform returned.

    ``sent`` (status ``"sent"``) means the adapter accepted / handoff
    succeeded.  For queue-based transports (e.g. Meshtastic) the message
    may still be in-flight; check ``delivery_note`` for context.

    Attributes
    ----------
    native_message_id:
        Platform-native message ID (e.g. a Matrix ``event_id``).
        ``None`` when the platform did not return one, or for queue-based
        sends where the adapter accepted locally but a native ID is not
        yet available.
    native_channel_id:
        Platform-native channel / room / conversation ID.
    native_thread_id:
        Platform-native thread or parent message ID, if applicable.
    native_relation_id:
        Platform-native ID of the related entity (e.g. the message
        being replied to), if applicable.  **Reserved** — no adapter
        currently populates this field.
    delivery_note:
        Human-readable context about the delivery.  Used by queue-based
        adapters to explain local-acceptance without a native ACK.
    metadata:
        Adapter-specific immutable metadata about the delivery.
    """

    native_message_id: str | None = None
    native_channel_id: str | None = None
    native_thread_id: str | None = None
    native_relation_id: str | None = None
    delivery_note: str = ""
    metadata: MappingProxyType[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AdapterRole(Enum):
    """Functional role an adapter plays in the framework.

    Attributes
    ----------
    TRANSPORT:
        A low-level radio or mesh transport (Meshtastic, MeshCore, LXMF).
    PRESENTATION:
        A chat or presentation platform (Matrix, Discord, Telegram).
    HYBRID:
        An adapter that fulfils both roles simultaneously.
    """

    TRANSPORT = "transport"
    PRESENTATION = "presentation"
    HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterCapabilities:
    """Immutable feature flags describing what an adapter supports.

    All fields default to the most conservative (least capable) value
    so that a new adapter is implicitly honest about what it cannot do.

    Attributes
    ----------
    text:
        Whether the adapter can send / receive plain-text payloads.
    title:
        Whether the adapter supports an explicit title / subject field.
    replies:
        Reply support: ``"native"`` (first-class), ``"fallback"``
        (simulated), or ``"unsupported"``.
    reactions:
        Reaction / emoji support (same semantics as *replies*).
    edits:
        Edit support (same semantics as *replies*).
    deletes:
        Delete support (same semantics as *replies*).
    attachments:
        Whether the adapter can carry file attachments.
    metadata_fields:
        Whether the adapter can transmit structured metadata fields.
    delivery_receipts:
        Whether the adapter can confirm delivery back to the framework.
    store_and_forward:
        Whether the adapter supports store-and-forward semantics.
    direct_messages:
        Whether the adapter supports direct (1-to-1) messages.
    channels:
        Whether the adapter supports channel, room, topic, or group-style
        destinations.
    ack_tracking:
        Whether the adapter exposes transport-level acknowledgement tracking
        to MEDRE.  This is descriptive only; it does not install retries.
    async_delivery:
        Whether delivery can complete asynchronously after MEDRE hands off a
        payload to the adapter.
    identity_encryption:
        Whether the adapter's transport identity model includes native
        identity-level encryption semantics that MEDRE may report.
    presence:
        Whether the adapter exposes presence/online state semantics.
    topic_rooms:
        Whether the adapter supports named topic/room destinations.
    mesh_routing:
        Whether the adapter participates in mesh/radio routing semantics.
    priority_delivery:
        Whether the adapter supports transport-level priority handling.
    max_text_bytes:
        Maximum text payload size in bytes, or ``None`` for unlimited.
    max_text_chars:
        Maximum text payload size in characters, or ``None`` for unlimited.
    """

    text: bool = True
    title: bool = False
    replies: str = "native"
    reactions: str = "native"
    edits: str = "native"
    deletes: str = "native"
    attachments: bool = False
    metadata_fields: bool = False
    delivery_receipts: bool = False
    store_and_forward: bool = False
    direct_messages: bool = True
    channels: bool = True
    ack_tracking: bool = False
    async_delivery: bool = False
    identity_encryption: bool = False
    presence: bool = False
    topic_rooms: bool = False
    mesh_routing: bool = False
    priority_delivery: bool = False
    max_text_bytes: int | None = None
    max_text_chars: int | None = None


@dataclass(frozen=True)
class AdapterInfo:
    """Runtime metadata about a running adapter instance.

    Attributes
    ----------
    adapter_id:
        Unique identifier of the adapter instance.
    platform:
        Human-readable platform name (e.g. ``"meshtastic"``, ``"matrix"``).
    role:
        The functional role of this adapter.
    version:
        Semantic version string of the adapter implementation.
    capabilities:
        The adapter's declared capabilities.
    health:
        Current health status.  Adapters should use one of the six
        protocol-neutral strings defined in
        :data:`~medre.core.runtime.health.VALID_HEALTH_STRINGS`:
        ``"healthy"``, ``"degraded"``, ``"failed"``, ``"unknown"``,
        ``"starting"``, or ``"stopping"``.  Defaults to ``"unknown"``.
    """

    adapter_id: str
    platform: str
    role: AdapterRole
    version: str
    capabilities: AdapterCapabilities
    health: str = "unknown"


@dataclass(frozen=True)
class OutboundNativeRefRecord:
    """Immutable record describing a delayed outbound native reference.

    Queue-based adapters (e.g. Meshtastic) cannot return a native message ID
    synchronously from :meth:`AdapterContract.deliver`.  When the queue later
    obtains a real native ID, the adapter constructs an
    :class:`OutboundNativeRefRecord` and passes it to the
    ``record_outbound_native_ref`` callback supplied by
    :class:`AdapterContext`.  The pipeline runner persists the mapping as a
    :class:`~medre.core.events.canonical.NativeMessageRef` with
    ``direction="outbound"``.

    Adapters must **not** fabricate IDs — only record IDs returned by the
    external platform.

    .. note::

       When the adapter is shut down before the queue drain records the native
       ref, the mapping is permanently lost.  The queue does not flush or retry
       on shutdown.

    Attributes
    ----------
    event_id:
        The canonical event ID that originated the outbound send.
    adapter:
        The adapter ID that owns the native namespace.
    native_channel_id:
        Channel / conversation ID in the adapter's native format.
    native_message_id:
        Message ID in the adapter's native format.  Must be a real ID
        from the external platform — never empty or fabricated.
    native_thread_id:
        Thread ID in the adapter's native format, if applicable.
    native_relation_id:
        ID of the related native entity, if applicable.
    metadata:
        Adapter-specific metadata about this mapping.  Must contain only
        JSON-safe, simple values.
    """

    event_id: str
    adapter: str
    native_channel_id: str | None
    native_message_id: str
    native_thread_id: str | None = None
    native_relation_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen = dict(self.metadata)
        try:
            import json

            json.dumps(frozen)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "OutboundNativeRefRecord.metadata must contain only JSON-safe values"
            ) from exc
        object.__setattr__(self, "metadata", MappingProxyType(frozen))


@dataclass
class AdapterContext:
    """Runtime context injected into an adapter on start-up.

    The framework constructs an :class:`AdapterContext` and passes it
    to :meth:`AdapterContract.start`.  The adapter *must* store it for the
    duration of its lifetime.

    Attributes
    ----------
    adapter_id:
        Unique identifier of the adapter instance.
    event_bus:
        Opaque reference to the framework's internal event bus.
        Adapters should prefer using *publish_inbound* rather than
        interacting with the bus directly.
    publish_inbound:
        Async callable that publishes a :class:`CanonicalEvent` into
        the framework's inbound event stream.
    logger:
        Pre-configured logger scoped to the adapter.
    clock:
        Callable returning the current UTC :class:`~datetime.datetime`.
        Use this instead of :func:`datetime.utcnow` for deterministic
        testing.
    shutdown_event:
        An :class:`asyncio.Event` that the framework sets when a
        graceful shutdown is requested.
    record_outbound_native_ref:
        Optional async callback that records a delayed outbound
        :class:`OutboundNativeRefRecord`.  Queue-based adapters call
        this after a queued send returns a real native message ID.  When
        ``None``, the adapter has no callback wired and delayed refs are
        silently discarded (e.g. in test or standalone mode).
    """

    adapter_id: str
    event_bus: Any
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
    logger: logging.Logger
    clock: Callable[[], datetime]
    shutdown_event: Any  # asyncio.Event – avoided import to prevent hard dep
    record_outbound_native_ref: (
        Callable[[OutboundNativeRefRecord], Awaitable[None]] | None
    ) = None


# ---------------------------------------------------------------------------
# AdapterCodec
# ---------------------------------------------------------------------------


class AdapterCodec(ABC):
    """Decode helper for converting between native and canonical
    representations.

    Adapters that follow the codec pattern can expose a codec instance
    via :meth:`AdapterContract.get_codec`.  The framework may use the codec
    for batch transformations, testing, or payload inspection without
    coupling to a specific adapter class.

    Outbound rendering is handled by :class:`~medre.core.rendering.renderer.Renderer`
    instances, not by the codec's ``encode`` method.
    """

    @abstractmethod
    def decode(self, native_event: Any) -> CanonicalEvent:
        """Convert a native (adapter-specific) event into a canonical event.

        Parameters
        ----------
        native_event:
            The adapter-specific event object to decode.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.
        """

    def encode(self, event: CanonicalEvent, target: Any) -> Any:
        """Encode a canonical event into an adapter-specific representation.

        **Default**: raises :class:`NotImplementedError`.  Outbound rendering
        is handled by renderers registered with the
        :class:`~medre.core.rendering.renderer.RenderingPipeline`.
        Subclasses should not override this.

        Parameters
        ----------
        event:
            The canonical event to encode.
        target:
            Adapter-specific target descriptor (e.g. a channel reference).

        Returns
        -------
        Any
            The native representation suitable for the target adapter.

        Raises
        ------
        NotImplementedError
            Always, unless overridden by a subclass.
        """
        raise NotImplementedError(
            "AdapterCodec.encode() is not used for runtime outbound rendering. "
            "Use a Renderer registered with the RenderingPipeline."
        )


# ---------------------------------------------------------------------------
# AdapterContract
# ---------------------------------------------------------------------------


class AdapterContract(ABC):
    """Abstract base class that every adapter must implement.

    Subclasses declare their identity (``adapter_id``, ``platform``,
    ``role``) as class attributes and implement the lifecycle methods
    (:meth:`start`, :meth:`stop`, :meth:`health_check`) and the
    delivery method (:meth:`deliver`).

    **Delivery contract**: every adapter must implement :meth:`deliver`
    which accepts a :class:`~medre.core.rendering.renderer.RenderingResult`
    and returns an :class:`AdapterDeliveryResult` on success (or ``None``
    when the adapter has no native ID to report).  The pipeline renders
    canonical events into adapter-ready payloads *before* calling
    ``deliver``.  Adapters must **not** perform event-kind-specific
    formatting inside ``deliver``; they merely transport the pre-rendered
    payload to the external platform and report native delivery metadata.

    Optionally, adapters can expose an :class:`AdapterCodec` via
    :meth:`get_codec` to support the codec pattern.

    **Stale event filtering**: adapters should call
    :meth:`publish_inbound` (not ``ctx.publish_inbound`` directly) so
    that events with a timestamp predating the adapter's start time are
    silently dropped.  This prevents historical / replayed events from
    previous sessions from entering the inbound pipeline.

    Attributes
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    platform:
        Human-readable platform name.
    role:
        The functional role of this adapter.
    """

    adapter_id: str
    platform: str
    role: AdapterRole

    _start_time: datetime | None
    _stale_events_dropped: int

    def __init__(self) -> None:
        self._start_time: datetime | None = None
        self._stale_events_dropped: int = 0

    def _mark_started(self, ctx: AdapterContext) -> None:
        """Record the adapter's start time from the context clock.

        Subclasses **must** call this (typically right after storing
        ``self.ctx = ctx`` in :meth:`start`) so that the stale-event
        filter knows when the adapter became active.

        Parameters
        ----------
        ctx:
            The runtime context whose ``clock`` provides the current UTC
            time.
        """
        self._start_time = ctx.clock()

    @abstractmethod
    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Deliver a pre-rendered payload to the external platform.

        The pipeline guarantees that *result* has already been rendered
        by a :class:`~medre.core.rendering.renderer.Renderer`.  The
        adapter must **not** re-render, reformat, or inspect the event
        kind to decide formatting.  It merely transports the payload.

        On success, adapters return an :class:`AdapterDeliveryResult`
        populated with platform-native IDs (message ID, channel ID, etc.)
        so that the pipeline can store native message mappings.  Return
        ``None`` when the adapter has no native ID to report.

        Parameters
        ----------
        result:
            The rendered payload ready for delivery.

        Returns
        -------
        AdapterDeliveryResult | None
            Native delivery metadata from the platform, or ``None``.

        Raises
        ------
        Exception
            If delivery fails.  The pipeline records a failed receipt
            and does **not** store a native outbound ref for failures.
        """

    @abstractmethod
    async def start(self, ctx: AdapterContext) -> None:
        """Start the adapter and wire it into the framework.

        The adapter receives its :class:`AdapterContext` here and should
        begin whatever background work it needs (polling, listening on
        sockets, etc.).

        Parameters
        ----------
        ctx:
            The runtime context provided by the framework.
        """

    @abstractmethod
    async def stop(self, timeout: float) -> None:
        """Gracefully stop the adapter.

        The adapter should finish in-flight work within *timeout* seconds.
        After this method returns the adapter must not publish any more
        events.

        Parameters
        ----------
        timeout:
            Maximum number of seconds to wait for a clean shutdown.
        """

    @abstractmethod
    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health and identity.

        Returns
        -------
        AdapterInfo
            Fresh metadata describing the adapter's state.
        """

    def _is_stale_event(self, event: CanonicalEvent) -> bool:
        """Return ``True`` if *event* predates the adapter's start time.

        Events whose :attr:`~CanonicalEvent.timestamp` is strictly before
        the moment the adapter started are considered stale and should be
        silently dropped.  This mirrors the mmrelay pattern where
        ``message_timestamp < facade.bot_start_time`` events are
        discarded.

        Returns ``False`` before :meth:`start` has been called (i.e.
        when ``_start_time`` is ``None``), allowing events through until
        the adapter is fully initialised.

        Parameters
        ----------
        event:
            The canonical event to check.

        Returns
        -------
        bool
            ``True`` if the event should be dropped; ``False`` otherwise.
        """
        if self._start_time is None:
            return False
        return event.timestamp < self._start_time

    async def publish_inbound(self, event: CanonicalEvent) -> None:
        """Publish a canonical event into the inbound pipeline.

        Wraps the framework-provided ``ctx.publish_inbound`` with a
        stale-event guard: events whose timestamp predates the adapter's
        start time are silently dropped (not forwarded, not stored).

        Subclasses **must** call this method instead of
        ``self.ctx.publish_inbound(event)`` so the guard is applied
        uniformly.

        Parameters
        ----------
        event:
            The canonical event to publish.
        """
        if self._is_stale_event(event):
            self._stale_events_dropped += 1
            return
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            await ctx.publish_inbound(event)

    def get_codec(self) -> AdapterCodec | None:
        """Return the adapter's codec, if it supports the codec pattern.

        The default implementation returns ``None``.  Subclasses that
        implement the codec pattern should override this method.

        Returns
        -------
        AdapterCodec | None
            The codec instance, or ``None`` if not supported.
        """
        return None


__all__ = [
    "AdapterCapabilities",
    "AdapterCodec",
    "AdapterContext",
    "AdapterContract",
    "AdapterDeliveryResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "OutboundNativeRefRecord",
]
