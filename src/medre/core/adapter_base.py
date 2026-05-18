"""Abstract base class that every adapter must implement.

Extracted from ``medre.adapters.base`` in Tranche 1.  This module
contains the :class:`BaseAdapter` ABC with its Template Method
behavior (stale-event guard, start-time tracking, codec accessor).
It imports from other ``medre.core`` modules but never from
``medre.adapters``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from typing import TYPE_CHECKING

from medre.core.ports import AdapterCodec, AdapterContext, AdapterDeliveryResult, AdapterInfo, AdapterRole

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent
    from medre.core.rendering.renderer import RenderingResult


# ---------------------------------------------------------------------------
# BaseAdapter
# ---------------------------------------------------------------------------


class BaseAdapter(ABC):
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

        **Boundary note:** This method is not abstract — it has a default
        implementation returning ``None``.  Adapters that support codec
        operations override it.  Similarly, ``diagnostics()`` is not
        defined on ``BaseAdapter`` at all; individual adapters implement
        it voluntarily.  Making either method abstract is deferred to
        future adapter boundary hardening.

        Returns
        -------
        AdapterCodec | None
            The codec instance, or ``None`` if not supported.
        """
        return None
