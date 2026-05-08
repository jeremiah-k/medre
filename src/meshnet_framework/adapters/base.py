"""Abstract base classes and value types for the adapter framework.

Adapters are the bridge between the meshnet framework and external
systems (radio transports, chat platforms, etc.).  Every adapter
inherits from :class:`BaseAdapter` and is driven by an
:class:`AdapterContext` supplied at start-up.

This module defines:

* :class:`AdapterRole` – the functional role of an adapter.
* :class:`AdapterCapabilities` – feature flags describing what an
  adapter supports.
* :class:`AdapterInfo` – runtime metadata about a running adapter.
* :class:`AdapterContext` – the runtime context injected into every
  adapter on start-up.
* :class:`BaseAdapter` – the abstract contract all adapters must
  implement.
* :class:`AdapterCodec` – optional encode/decode helper that adapters
  may expose.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable

from meshnet_framework.core.events.canonical import CanonicalEvent


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
        Current health status: ``"healthy"``, ``"degraded"``, or
        ``"failed"``.
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
    to :meth:`BaseAdapter.start`.  The adapter *must* store it for the
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
    """

    adapter_id: str
    event_bus: Any
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
    logger: logging.Logger
    clock: Callable[[], datetime]
    shutdown_event: Any  # asyncio.Event – avoided import to prevent hard dep


# ---------------------------------------------------------------------------
# AdapterCodec
# ---------------------------------------------------------------------------


class AdapterCodec(ABC):
    """Optional encode/decode helper for converting between native and
    canonical representations.

    Adapters that follow the codec pattern can expose a codec instance
    via :meth:`BaseAdapter.get_codec`.  The framework may use the codec
    for batch transformations, testing, or payload inspection without
    coupling to a specific adapter class.
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

    @abstractmethod
    def encode(self, event: CanonicalEvent, target: Any) -> Any:
        """Encode a canonical event into an adapter-specific representation.

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
        """


# ---------------------------------------------------------------------------
# BaseAdapter
# ---------------------------------------------------------------------------


class BaseAdapter(ABC):
    """Abstract base class that every adapter must implement.

    Subclasses declare their identity (``adapter_id``, ``platform``,
    ``role``) as class attributes and implement the lifecycle methods
    (:meth:`start`, :meth:`stop`, :meth:`health_check`).

    Optionally, adapters can expose an :class:`AdapterCodec` via
    :meth:`get_codec` to support the codec pattern.

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
