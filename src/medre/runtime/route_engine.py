"""Deterministic runtime routing engine that bridges config routes to the core Router.

This module converts :class:`RouteConfigSet` entries into core
:class:`~medre.core.routing.models.Route` objects, validates adapter
references against the assembled runtime adapter IDs, and registers
them with the :class:`~medre.core.routing.router.Router`.

It is deliberately transport-agnostic and SDK-free.  Route ordering is
deterministic: routes are registered in the same order they appear in
the :class:`RouteConfigSet`.

Public symbols
--------------
* :func:`build_runtime_routes` — expand config routes into core Route objects
* :func:`validate_route_adapter_refs` — validate adapter references
* :func:`register_routes` — build, validate, and register routes on a Router
* :class:`RouteValidationError` — raised on invalid route adapter references
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.runtime.errors import RuntimeConfigError

if TYPE_CHECKING:
    from medre.runtime.routes import RouteConfig, RouteConfigSet

__all__ = [
    "RouteValidationError",
    "build_runtime_routes",
    "validate_route_adapter_refs",
    "register_routes",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RouteValidationError(RuntimeConfigError):
    """Raised when route config references unknown adapter IDs."""


# ---------------------------------------------------------------------------
# Adapter reference validation
# ---------------------------------------------------------------------------


def validate_route_adapter_refs(
    route_config_set: RouteConfigSet,
    adapter_ids: frozenset[str],
) -> None:
    """Validate that all adapter references in routes resolve to known adapters.

    Checks both ``source_adapters`` and ``dest_adapters`` for every enabled
    route against the set of adapter IDs assembled by the builder.

    Parameters
    ----------
    route_config_set:
        The route configuration set to validate.
    adapter_ids:
        The frozenset of adapter IDs that were successfully built.

    Raises
    ------
    RouteValidationError
        If any enabled route references an adapter ID that is not in
        *adapter_ids*.
    """
    unknown_refs: list[str] = []

    for rc in route_config_set.routes:
        if not rc.enabled:
            continue
        for aid in rc.source_adapters:
            if aid not in adapter_ids:
                unknown_refs.append(
                    f"Route {rc.route_id!r} references unknown source "
                    f"adapter {aid!r}"
                )
        for aid in rc.dest_adapters:
            if aid not in adapter_ids:
                unknown_refs.append(
                    f"Route {rc.route_id!r} references unknown dest "
                    f"adapter {aid!r}"
                )

    if unknown_refs:
        details = "; ".join(unknown_refs)
        raise RouteValidationError(
            f"Invalid route adapter references: {details}. "
            f"Known adapters: {sorted(adapter_ids)}"
        )


# ---------------------------------------------------------------------------
# Config → core Route conversion
# ---------------------------------------------------------------------------


def _expand_route_config(
    rc: RouteConfig,
    *,
    swap_direction: bool = False,
) -> list[Route]:
    """Expand a single :class:`RouteConfig` into one or more core :class:`Route` objects.

    Expansion rules:

    * One ``RouteConfig`` with N source adapters produces N ``Route``
      objects — one per source adapter.
    * Each expanded route gets all dest adapters as :class:`RouteTarget`
      entries.
    * If *swap_direction* is ``True``, source and dest adapters are
      swapped (used for ``dest_to_source`` and the reverse leg of
      ``bidirectional`` routes).
    * The :class:`BridgePolicy` ``allowed_event_types`` are mapped to
      :attr:`RouteSource.event_kinds` when non-empty.
    * Route IDs are suffixed to ensure uniqueness when expanding.

    Parameters
    ----------
    rc:
        The route configuration to expand.
    swap_direction:
        If ``True``, treat dest_adapters as source and source_adapters
        as dest.

    Returns
    -------
    list[Route]
        Expanded core route objects, all sharing the ``enabled`` flag
        from *rc*.
    """
    from medre.runtime.routes import RouteDirectionality

    if swap_direction:
        source_ids = rc.dest_adapters
        dest_ids = rc.source_adapters
        source_channel = rc.dest_channel
        dest_channel = rc.source_channel
    else:
        source_ids = rc.source_adapters
        dest_ids = rc.dest_adapters
        source_channel = rc.source_channel
        dest_channel = rc.dest_channel

    # BridgePolicy event types → RouteSource event_kinds
    event_kinds: tuple[str, ...] = ()
    if rc.policy is not None and rc.policy.allowed_event_types:
        event_kinds = rc.policy.allowed_event_types

    routes: list[Route] = []

    for src_idx, src_id in enumerate(source_ids):
        # Build a deterministic route ID that is unique across expansions.
        # For a single source adapter, use the original route_id.
        # For multiple, append an index suffix.
        if len(source_ids) == 1 and not swap_direction:
            route_id = rc.route_id
        elif swap_direction:
            route_id = f"{rc.route_id}__rev_{src_idx}"
        else:
            route_id = f"{rc.route_id}__{src_idx}"

        targets = [
            RouteTarget(adapter=did, channel=dest_channel)
            for did in dest_ids
        ]

        source = RouteSource(
            adapter=src_id,
            event_kinds=event_kinds,
            channel=source_channel,
        )

        route = Route(
            id=route_id,
            source=source,
            targets=targets,
            enabled=rc.enabled,
        )
        routes.append(route)

    return routes


def build_runtime_routes(
    route_config_set: RouteConfigSet,
) -> list[Route]:
    """Convert a :class:`RouteConfigSet` into core :class:`Route` objects.

    Only **enabled** routes are included.  Disabled routes are silently
    skipped.  Route expansion handles:

    * ``source_to_dest`` — forward direction only.
    * ``dest_to_source`` — reverse direction only (sources become dests).
    * ``bidirectional`` — both forward and reverse legs.

    Loop-prevention note
    --------------------
    A bidirectional route with overlapping source/dest adapters would
    create a direct loop.  This is already prevented at the config level
    by :class:`RouteConfig` validation (no source/dest overlap within a
    single route).  Cross-route loops (route A: X→Y and route B: Y→X)
    are detected by :func:`check_route_loops` and logged as warnings.

    Parameters
    ----------
    route_config_set:
        The validated route configuration set.

    Returns
    -------
    list[Route]
        Ordered list of core route objects ready for registration.
    """
    from medre.runtime.routes import RouteDirectionality

    all_routes: list[Route] = []
    expanded_ids: dict[str, str] = {}  # expanded_id → config route_id

    for rc in route_config_set.routes:
        if not rc.enabled:
            _logger.debug("Skipping disabled route %r", rc.route_id)
            continue

        direction = rc.directionality

        new_routes: list[Route] = []
        if direction == RouteDirectionality.SOURCE_TO_DEST:
            new_routes = _expand_route_config(rc)
        elif direction == RouteDirectionality.DEST_TO_SOURCE:
            new_routes = _expand_route_config(rc, swap_direction=True)
        elif direction == RouteDirectionality.BIDIRECTIONAL:
            new_routes = _expand_route_config(rc)
            new_routes.extend(_expand_route_config(rc, swap_direction=True))

        # Validate expanded route IDs are unique before accumulating.
        for r in new_routes:
            if r.id in expanded_ids:
                raise RouteValidationError(
                    f"Expanded route ID collision: {r.id!r} from route "
                    f"{rc.route_id!r} conflicts with route "
                    f"{expanded_ids[r.id]!r}. Route IDs must be unique and "
                    f"must not match the expansion pattern "
                    f"'<id>__<N>' or '<id>__rev_<N>'."
                )
            expanded_ids[r.id] = rc.route_id

        all_routes.extend(new_routes)

    return all_routes


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


def check_route_loops(routes: list[Route]) -> list[str]:
    """Detect routing loops among the given routes.

    Two levels of detection:

    1. **Fast path** — direct two-adapter loops (A↔B).
    2. **Slow path** — multi-hop cycles via DFS on the directed
       adapter adjacency graph (X→Y→Z→X).

    Both levels log warnings but do **not** block startup.

    Returns
    -------
    list[str]
        Human-readable descriptions of detected loops.  Empty if no
        loops found.
    """
    # Build a set of (source_adapter, dest_adapter) pairs from all enabled routes.
    edges: dict[tuple[str, str], list[str]] = {}
    for route in routes:
        if not route.enabled:
            continue
        src = route.source.adapter
        if src is None:
            continue
        for target in route.targets:
            dst = target.adapter
            if dst is None:
                continue
            key = (src, dst)
            edges.setdefault(key, []).append(route.id)

    loops: list[str] = []

    # -- Fast path: direct A↔B loops ---------------------------------------
    checked: set[tuple[str, str]] = set()
    for (src, dst), route_ids in edges.items():
        if (dst, src) in edges and (src, dst) not in checked:
            reverse_ids = edges[(dst, src)]
            loops.append(
                f"Direct routing loop detected between adapters "
                f"{src!r} and {dst!r}: routes {route_ids} and "
                f"{reverse_ids}"
            )
            checked.add((src, dst))
            checked.add((dst, src))

    # -- Slow path: multi-hop cycle detection via DFS ----------------------
    # Build adjacency list: source_adapter → [dest_adapter, ...]
    adj: dict[str, list[str]] = {}
    for (src, dst) in edges:
        adj.setdefault(src, []).append(dst)

    visited: set[str] = set()
    rec_stack: set[str] = set()
    path: list[str] = []

    def _dfs(node: str) -> None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbour in adj.get(node, []):
            if neighbour not in visited:
                _dfs(neighbour)
            elif neighbour in rec_stack:
                # Found a cycle — extract the cycle portion from path.
                cycle_start = path.index(neighbour)
                cycle = path[cycle_start:] + [neighbour]
                cycle_str = " -> ".join(cycle)
                loops.append(
                    f"Route cycle detected: {cycle_str}"
                )

        path.pop()
        rec_stack.remove(node)

    for node in sorted(adj):
        if node not in visited:
            _dfs(node)

    return loops


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------


def register_routes(
    router: Router,
    route_config_set: RouteConfigSet,
    adapter_ids: frozenset[str],
) -> list[Route]:
    """Build, validate, and register runtime routes on a :class:`Router`.

    This is the primary entry point called by the runtime builder after
    adapters have been constructed.  It:

    1. Validates that all enabled route adapter references resolve to
       known adapter IDs.
    2. Converts :class:`RouteConfigSet` entries into core :class:`Route`
       objects.
    3. Checks for direct routing loops and logs warnings.
    4. Registers all routes on the *router* in deterministic order.

    Parameters
    ----------
    router:
        The core routing engine to populate.
    route_config_set:
        The route configuration set from the runtime config.
    adapter_ids:
        Frozenset of adapter IDs that were successfully built.

    Returns
    -------
    list[Route]
        The list of registered routes.

    Raises
    ------
    RouteValidationError
        If any enabled route references an unknown adapter ID.
    """
    # Step 1: Validate adapter references.
    validate_route_adapter_refs(route_config_set, adapter_ids)

    # Step 2: Build core routes.
    routes = build_runtime_routes(route_config_set)

    if not routes:
        _logger.info("No enabled routes to register")
        return routes

    # Step 3: Loop detection — log warnings but do not block startup.
    loops = check_route_loops(routes)
    for loop_msg in loops:
        _logger.warning("Route loop warning: %s", loop_msg)

    # Step 4: Register routes in deterministic order.
    for route in routes:
        router.add_route(route)
        _logger.info(
            "Registered route %r: %s → %s",
            route.id,
            route.source.adapter,
            [t.adapter for t in route.targets],
        )

    _logger.info("Registered %d route(s)", len(routes))
    return routes
