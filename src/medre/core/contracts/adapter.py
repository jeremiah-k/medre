"""Core adapter contract types and abstract base class.

This module owns the adapter runtime contracts used by the core engine.
It defines the value types, protocols, and the abstract
:class:`AdapterContract` that every concrete adapter must implement.

Definitions:

* :class:`AdapterSendError` – base error raised by adapters when delivery fails.
* :class:`AdapterPermanentError` – permanent delivery error.
* :class:`AdapterHandoffResult` – immutable fact returned after successful hand-off.
* :class:`AdapterRole` – the functional role of an adapter.
* :class:`AdapterCapabilities` – feature flags describing what an adapter supports.
* :class:`AdapterInfo` – runtime metadata about a running adapter instance.
* :class:`AdapterContext` – the runtime context injected into every adapter on start-up.
* :class:`AdapterCodec` – optional inbound decode helper that adapters may expose.
* :class:`AdapterContract` – abstract base class that every adapter must implement.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from medre.core.contracts.delivery import AdapterHandoffResult, DeliveryFeedback
from medre.core.events.canonical import CanonicalEvent

if TYPE_CHECKING:
    from medre.core.ingress import AdapterCheckpoint, AdmissionResult, IngressProvenance
    from medre.core.rendering.renderer import RenderingResult


# ---------------------------------------------------------------------------
# Adapter error hierarchy
# ---------------------------------------------------------------------------


# Maximum transport-provided retry minimum accepted by built-in scheduling
# and shared backpressure state.  This is a safety horizon, not a policy
# backoff cap: shorter route/plan policy delays still apply normally.
MAX_ADAPTER_RETRY_AFTER_SECONDS: float = 2_592_000.0  # 30 days


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
    retry_after_seconds:
        Optional minimum delay before the next durable retry attempt.
        Adapters should use this for authoritative transport hints such as
        server-directed rate-limit windows.  The core retry policy remains
        authoritative and uses the larger of its normal backoff and this hint.
        Built-in scheduling/backpressure clamps effective hints to
        :data:`MAX_ADAPTER_RETRY_AFTER_SECONDS`.
    """

    transient: bool
    retry_after_seconds: float | None

    def __init__(
        self,
        *args: object,
        transient: bool = True,
        retry_after_seconds: float | None = None,
    ) -> None:
        if retry_after_seconds is not None:
            if isinstance(retry_after_seconds, bool) or not isinstance(
                retry_after_seconds, (int, float)
            ):
                raise ValueError(
                    "retry_after_seconds must be a finite number >= 0 or None"
                )
            try:
                numeric_retry_after = float(retry_after_seconds)
            except (OverflowError, ValueError) as exc:
                raise ValueError(
                    "retry_after_seconds must be a finite number >= 0 or None"
                ) from exc
            if not math.isfinite(numeric_retry_after) or numeric_retry_after < 0:
                raise ValueError(
                    "retry_after_seconds must be a finite number >= 0 or None"
                )
            retry_after_seconds = numeric_retry_after
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds
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
    store_and_forward:
        Whether the adapter supports store-and-forward semantics.
    direct_messages:
        Whether the adapter supports direct (1-to-1) messages.
    channels:
        Whether the adapter supports channel, room, topic, or group-style
        destinations.
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
    threads:
        Thread support with the same three-level semantics as *replies*.
        Appended after the original capability fields so positional construction
        retains its historical argument mapping.
    """

    text: bool = True
    title: bool = False
    replies: str = "native"
    reactions: str = "native"
    edits: str = "native"
    deletes: str = "native"
    attachments: bool = False
    metadata_fields: bool = False
    store_and_forward: bool = False
    direct_messages: bool = True
    channels: bool = True
    identity_encryption: bool = False
    presence: bool = False
    topic_rooms: bool = False
    mesh_routing: bool = False
    priority_delivery: bool = False
    max_text_bytes: int | None = None
    max_text_chars: int | None = None
    threads: str = "unsupported"


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
        :data:`~medre.core.supervision.health.VALID_HEALTH_STRINGS`:
        ``"healthy"``, ``"degraded"``, ``"failed"``, ``"unknown"``,
        ``"starting"``, or ``"stopping"``.  Defaults to ``"unknown"``.
    """

    adapter_id: str
    platform: str
    role: AdapterRole
    version: str
    capabilities: AdapterCapabilities
    health: str = "unknown"


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
    publish_inbound:
        Async callable that publishes a :class:`CanonicalEvent` into
        the framework's inbound event stream.
    admit_inbound:
        Optional durable-admission callable. Protocol adapters with reliable
        cursor/provenance semantics may use this instead of ``publish_inbound``
        so external cursor advancement is decoupled from downstream routing.
    load_checkpoint / commit_checkpoint:
        Optional application-owned cursor persistence bound to this adapter
        instance. The stream name is supplied by the adapter; checkpoint
        metadata must already be JSON encoded and secret-free.
    logger:
        Pre-configured logger scoped to the adapter.
    clock:
        Callable returning the current UTC :class:`~datetime.datetime`.
        Use this instead of :func:`datetime.utcnow` for deterministic
        testing.
    shutdown_event:
        An :class:`asyncio.Event` that the framework sets when a
        graceful shutdown is requested.
    report_delivery_feedback:
        Optional async sink for the closed :class:`DeliveryFeedback` union.
        Adapters use this one boundary for deferred hand-off completion,
        deferred terminal failure, and post-hand-off observations. Core
        remains lifecycle authority.
    """

    adapter_id: str
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
    logger: logging.Logger
    clock: Callable[[], datetime]
    shutdown_event: Any  # asyncio.Event – avoided import to prevent hard dep
    admit_inbound: (
        Callable[[CanonicalEvent, IngressProvenance], Awaitable[AdmissionResult]] | None
    ) = None
    load_checkpoint: Callable[[str], Awaitable[AdapterCheckpoint | None]] | None = None
    commit_checkpoint: Callable[[str, str, str], Awaitable[None]] | None = None
    report_delivery_feedback: Callable[[DeliveryFeedback], Awaitable[None]] | None = (
        None
    )


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

    Outbound rendering is exclusively handled by
    :class:`~medre.core.rendering.renderer.Renderer` instances.
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
    and returns an :class:`AdapterHandoffResult` on every successful call.
    The pipeline renders
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

    _event_timestamp_granularity_us: int = 1
    """Timestamp granularity of the adapter's transport events, in microseconds.

    The stale-event guard floors the adapter start time down to this
    granularity before comparison so sub-granularity differences between a
    live event and the microsecond start clock are not treated as backlog.
    The default of 1 keeps an exact comparison for microsecond-resolution
    transports; adapters whose transports carry coarser timestamps (e.g.
    Matrix ``origin_server_ts`` milliseconds) override this.
    """

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
    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        """Deliver a pre-rendered payload to the external platform.

        The pipeline guarantees that *result* has already been rendered
        by a :class:`~medre.core.rendering.renderer.Renderer`.  The
        adapter must **not** re-render, reformat, or inspect the event
        kind to decide formatting.  It merely transports the payload.

        On success, adapters return an :class:`AdapterHandoffResult`. The
        ``disposition`` states whether transport hand-off completed during the
        call or remains deferred. Native IDs are optional transport facts.

        Parameters
        ----------
        result:
            The rendered payload ready for delivery.

        Returns
        -------
        AdapterHandoffResult
            Closed hand-off fact reported by the adapter.

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
        # Floor the start time to the transport's timestamp granularity so
        # sub-granularity differences between a live event created within
        # the startup instant and the microsecond start clock do not
        # register as backlog. Granularity 1 (microsecond-resolution
        # transports) keeps the exact comparison.
        start = self._start_time
        granularity = self._event_timestamp_granularity_us
        if granularity > 1:
            start = start.replace(
                microsecond=(start.microsecond // granularity) * granularity
            )
        return event.timestamp < start

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

    async def admit_inbound(
        self, event: CanonicalEvent, provenance: IngressProvenance
    ) -> AdmissionResult:
        """Durably admit an event using protocol-supplied provenance.

        Unlike :meth:`publish_inbound`, this path intentionally does not apply
        the generic adapter-start timestamp filter. Protocol evidence such as
        Matrix ``RECOVERED`` provenance is authoritative for continuity.
        """
        ctx = getattr(self, "ctx", None)
        if ctx is None or ctx.admit_inbound is None:
            raise RuntimeError("durable ingress admission is not wired")
        return await ctx.admit_inbound(event, provenance)

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
    "AdapterHandoffResult",
    "AdapterInfo",
    "AdapterPermanentError",
    "AdapterRole",
    "AdapterSendError",
    "DeliveryFeedback",
]
