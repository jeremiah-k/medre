"""Router engine for event-to-route matching and target resolution.

This module provides:

* :class:`Router` – the central routing engine that matches incoming
  events against registered routes and resolves delivery targets.
* :class:`RouteConflictError` – raised when exclusive routes overlap.

The router performs no I/O.  It is a pure in-memory matching engine
intended to be called from the framework's event processing pipeline.
"""

from __future__ import annotations

from medre.core.events.canonical import CanonicalEvent
from medre.core.routing.models import Route, RouteSource, RouteTarget


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RouteConflictError(Exception):
    """Raised when two exclusive routes have overlapping source specs.

    Attributes
    ----------
    route_a_id:
        Identifier of the first conflicting route.
    route_b_id:
        Identifier of the second conflicting route.
    """

    def __init__(self, route_a_id: str, route_b_id: str) -> None:
        self.route_a_id = route_a_id
        self.route_b_id = route_b_id
        super().__init__(
            f"Exclusive routes {route_a_id!r} and {route_b_id!r} "
            f"have overlapping source specifications"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _source_matches(source: RouteSource, event: CanonicalEvent) -> bool:
    """Return ``True`` if *source* matches *event*.

    A source matches when **all** non-``None`` filters pass:

    * ``adapter`` – must equal ``event.source_adapter`` if not ``None``.
    * ``event_kinds`` – must contain ``event.event_kind`` if non-empty.
    * ``channel`` – must equal ``event.source_channel_id`` if not ``None``.
    """
    if source.adapter is not None and source.adapter != event.source_adapter:
        return False
    if source.event_kinds and event.event_kind not in source.event_kinds:
        return False
    if source.channel is not None and source.channel != event.source_channel_id:
        return False
    return True


def _sources_overlap(a: RouteSource, b: RouteSource) -> bool:
    """Return ``True`` if two source specs can match the same event.

    Two sources overlap when there exists at least one concrete event
    that satisfies both simultaneously.  A ``None`` field acts as a
    wildcard.
    """
    # Adapter overlap
    if a.adapter is not None and b.adapter is not None and a.adapter != b.adapter:
        return False
    # Event-kinds overlap
    if a.event_kinds and b.event_kinds:
        if not set(a.event_kinds).intersection(b.event_kinds):
            return False
    # Channel overlap
    if a.channel is not None and b.channel is not None and a.channel != b.channel:
        return False
    return True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Central routing engine that matches events to routes.

    The router maintains an ordered list of :class:`Route` objects and
    provides methods to add/remove routes, match events, resolve targets,
    and validate that no exclusive routes conflict.

    Parameters
    ----------
    routes:
        Initial list of routes to register.  Defaults to an empty list.

    Example
    -------
    >>> from medre.core.routing.models import (
    ...     Route, RouteSource, RouteTarget,
    ... )
    >>> route = Route(
    ...     id="r1",
    ...     source=RouteSource(adapter=None, event_kinds=("message.text",), channel=None),
    ...     targets=[RouteTarget(adapter="discord", channel="general", destination=None)],
    ... )
    >>> router = Router(routes=[route])
    >>> matched = router.match(some_event)
    """

    def __init__(self, routes: list[Route] | None = None) -> None:
        self._routes: dict[str, Route] = {}
        if routes is not None:
            for route in routes:
                self._routes[route.id] = route

    # -- Mutation ---------------------------------------------------------

    def add_route(self, route: Route) -> None:
        """Register a new route.

        If a route with the same ``id`` already exists it is replaced.

        Parameters
        ----------
        route:
            The route to register.
        """
        self._routes[route.id] = route

    def remove_route(self, route_id: str) -> None:
        """Remove a route by its identifier.

        Parameters
        ----------
        route_id:
            The ``id`` of the route to remove.

        Raises
        ------
        KeyError
            If no route with the given *route_id* exists.
        """
        del self._routes[route_id]

    # -- Query ------------------------------------------------------------

    def match(self, event: CanonicalEvent) -> list[Route]:
        """Return all routes whose source filter matches *event*.

        Only enabled routes are considered.  Routes are returned in
        insertion order.

        Parameters
        ----------
        event:
            The canonical event to match against.

        Returns
        -------
        list[Route]
            All enabled routes whose :attr:`Route.source` matches the
            event.
        """
        return [
            route
            for route in self._routes.values()
            if route.enabled and _source_matches(route.source, event)
        ]

    def resolve_targets(
        self,
        event: CanonicalEvent,
        route: Route,
    ) -> list[RouteTarget]:
        """Resolve the effective targets for *event* on *route*.

        In Phase 1 the target list is returned as-is.  Future phases
        may filter or reorder targets based on the event's source
        channel, adapter capabilities, or fanout strategy.

        Parameters
        ----------
        event:
            The canonical event being routed.
        route:
            The matched route whose targets to resolve.

        Returns
        -------
        list[RouteTarget]
            The resolved delivery targets.
        """
        _ = event  # reserved for future channel-mapping logic
        return list(route.targets)

    # -- Validation -------------------------------------------------------

    def validate_no_conflicts(self) -> None:
        """Validate that no two exclusive routes have overlapping sources.

        Raises
        ------
        RouteConflictError
            If two routes with ``ownership="exclusive"`` have source
            specifications that can match the same event.
        """
        exclusive = [
            route for route in self._routes.values()
            if route.ownership == "exclusive"
        ]
        for i, route_a in enumerate(exclusive):
            for route_b in exclusive[i + 1:]:
                if _sources_overlap(route_a.source, route_b.source):
                    raise RouteConflictError(route_a.id, route_b.id)
