"""Route data model definitions for the medre.

This module defines the data structures that describe how events flow from
sources to targets:

* :class:`RouteSource` – what events a route matches (adapter, kinds, channel).
* :class:`RouteDestination` – identity-based addressing for non-channel targets.
* :class:`RouteTarget` – where matched events should be delivered.
* :class:`Route` – a complete source-to-target mapping rule.

All value types are immutable (``frozen=True``) except :class:`Route`,
which is mutable to allow runtime enable/disable toggling.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Route source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteSource:
    """Specification of what events a route matches.

    Each field acts as a filter.  A ``None`` value means "match any";
    an empty ``event_kinds`` tuple also means "match any event kind".

    Attributes
    ----------
    adapter:
        Name of the source adapter, or ``None`` to match events from any
        adapter.
    event_kinds:
        Tuple of event kind strings to match.  An empty tuple matches
        all event kinds.
    channel:
        Source channel / conversation ID, or ``None`` to match any
        channel.
    """

    adapter: str | None
    event_kinds: tuple[str, ...]
    channel: str | None


# ---------------------------------------------------------------------------
# Route destination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDestination:
    """Identity-based addressing for non-channel delivery targets.

    Used when the target is identified by a network address, hash, or
    name rather than a logical channel.

    Attributes
    ----------
    kind:
        The addressing scheme – e.g. ``"channel"``, ``"matrix_room"``,
        ``"lxmf_destination"``.
    destination_hash:
        Hash-based identifier for the destination, if applicable.
    destination_name:
        Human-readable name for the destination, if applicable.
    metadata:
        Arbitrary key-value metadata for adapter-specific addressing
        parameters.
    """

    kind: str
    destination_hash: str | None
    destination_name: str | None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Route target
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteTarget:
    """A single delivery destination for matched events.

    A target specifies *where* to send an event.  It can address a
    channel on a specific adapter, or use identity-based addressing via
    a :class:`RouteDestination`.

    Attributes
    ----------
    adapter:
        Name of the target adapter, or ``None`` for any adapter.
    channel:
        Logical channel name for channel-addressed adapters, or ``None``.
    destination:
        Identity-based destination, used when channel addressing does
        not apply.
    """

    adapter: str | None = None
    channel: str | None = None
    destination: RouteDestination | None = None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@dataclass
class Route:
    """A complete source-to-target mapping rule.

    A route connects a :class:`RouteSource` filter to one or more
    :class:`RouteTarget` destinations.  The :class:`Router` evaluates
    all registered routes to determine where an event should be
    delivered.

    Attributes
    ----------
    id:
        Unique identifier for this route.
    source:
        The event filter that must match for this route to activate.
    targets:
        Ordered list of delivery targets for matched events.
    fanout_strategy:
        How events are distributed across targets.  Only ``"broadcast"``
        (deliver to all targets) is supported in Phase 1.
    ownership:
        Whether this route is ``"exclusive"`` (no other exclusive route
        may overlap) or ``"shared"`` (overlaps allowed).
    enabled:
        Whether this route is currently active.
    """

    id: str
    source: RouteSource
    targets: list[RouteTarget]
    fanout_strategy: str = "broadcast"
    ownership: str = "shared"
    enabled: bool = True
