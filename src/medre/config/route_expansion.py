"""Config-owned route expansion: compile ``RouteConfigSet`` into core routes.

This module is the single expansion authority for the configuration
seam: it converts :class:`~medre.config.routes.RouteConfig` entries
into neutral core :class:`~medre.core.routing.models.Route` legs with
deterministic IDs, threading policy event kinds and structured
destinations through unchanged.

Expansion shapes
----------------
* Standard routes — N source adapters produce N forward legs
  (``route_id`` / ``route_id__<N>``) and, when the directionality
  creates them, N reverse legs (``route_id__rev_<N>``) whose source and
  dest sides are swapped.
* ``context_map`` routes — one forward leg per mapped context using a
  stable content-derived token (``route_id__map<token>__fwd``) and, when
  the directionality creates them, one reverse leg
  (``route_id__map<token>__rev``) with adapters,
  contexts, and label sides swapped.

It is deliberately transport-agnostic and SDK-free: contexts are
opaque strings end-to-end and routing matches them by plain string
equality.  This module must not import :mod:`medre.runtime`.

Public symbols
--------------
* :class:`ExpandedRouteLeg` — one compiled core Route plus config provenance
* :func:`expand_route_config` — expand ONE route config
* :func:`expand_route_configs` — expand every enabled route in a set
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from medre.config.errors import ConfigValidationError
from medre.config.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDestinationConfig,
    RouteDirectionality,
)
from medre.core.policies.route_policy import RoutePolicy
from medre.core.routing.models import Route, RouteDestination, RouteSource, RouteTarget

__all__ = [
    "EXPANSION_ID_PATTERNS_HINT",
    "ExpandedRouteLeg",
    "expand_route_config",
    "expand_route_configs",
]

#: Reserved expansion-ID suffix patterns, named once so the compiler's
#: collision error and the runtime route plan's cross-route collision
#: report stay word-for-word in sync.
EXPANSION_ID_PATTERNS_HINT = (
    "'<id>__<N>', '<id>__rev_<N>', '<id>__map<token>__fwd', or "
    "'<id>__map<token>__rev'"
)

_CONTEXT_MAP_TOKEN_HEX_LENGTH = 32

_logger = logging.getLogger(__name__)

#: Config-relative direction labels carried on every expanded leg.
_DIRECTION_FORWARD = RouteDirectionality.SOURCE_TO_DEST.value
_DIRECTION_REVERSE = RouteDirectionality.DEST_TO_SOURCE.value


# ---------------------------------------------------------------------------
# Expanded leg model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpandedRouteLeg:
    """One compiled core :class:`Route` plus its config provenance.

    Attributes
    ----------
    route:
        The fully built core :class:`Route` (one delivery leg).
    config_route_id:
        The ``routes.<id>`` section key this leg was expanded from.
    direction:
        Config-relative flow of this leg: ``"source_to_dest"`` for
        forward legs, ``"dest_to_source"`` for reverse legs.
    mapping_source_context:
        The ``context_map`` source-side key this leg was expanded from,
        or ``None`` for non-mapping (standard) legs.
    mapping_dest_context:
        The entry's ``dest_context`` when present, else ``None``
        (structured-destination entries and standard legs carry
        ``None``).
    """

    route: Route
    config_route_id: str
    direction: str
    mapping_source_context: str | None
    mapping_dest_context: str | None


# ---------------------------------------------------------------------------
# Policy / destination conversion
# ---------------------------------------------------------------------------


def _convert_bridge_policy(bp: BridgePolicy) -> RoutePolicy | None:
    """Convert a config :class:`BridgePolicy` to a core :class:`RoutePolicy`.

    Excludes ``allowed_event_types`` (already enforced structurally via
    :attr:`RouteSource.event_kinds`).  Returns ``None`` when all
    remaining allowlist fields are empty (no policy to enforce).
    """
    if not (
        bp.allowed_source_adapters
        or bp.allowed_dest_adapters
        or bp.room_allowlist
        or bp.channel_allowlist
        or bp.sender_allowlist
    ):
        return None

    return RoutePolicy(
        allowed_source_adapters=bp.allowed_source_adapters,
        allowed_dest_adapters=bp.allowed_dest_adapters,
        room_allowlist=bp.room_allowlist,
        channel_allowlist=bp.channel_allowlist,
        sender_allowlist=bp.sender_allowlist,
    )


def _convert_destination(
    config_destination: RouteDestinationConfig | None,
) -> RouteDestination | None:
    """Convert a config :class:`RouteDestinationConfig` to the core model.

    The metadata dict is copied so the core destination owns its own
    (recursively frozen) copy.
    """
    if config_destination is None:
        return None
    return RouteDestination(
        kind=config_destination.kind,
        destination_hash=config_destination.destination_hash,
        destination_name=config_destination.destination_name,
        metadata=dict(config_destination.metadata),
    )


def _route_policy_parts(
    rc: RouteConfig,
) -> tuple[tuple[str, ...], RoutePolicy | None]:
    """Thread route policy into (source event_kinds, core RoutePolicy)."""
    event_kinds: tuple[str, ...] = ()
    if rc.policy is not None and rc.policy.allowed_event_types:
        event_kinds = rc.policy.allowed_event_types
    route_policy: RoutePolicy | None = None
    if rc.policy is not None:
        route_policy = _convert_bridge_policy(rc.policy)
    return event_kinds, route_policy


# ---------------------------------------------------------------------------
# Runtime-semantic boundary checks (direct-construction safety net)
# ---------------------------------------------------------------------------


def _check_route_boundaries(rc: RouteConfig) -> None:
    """Validate runtime-semantic invariants on a (possibly direct-built) route.

    ``RouteConfig.from_dict`` rejects these shapes during config
    parsing and ``RouteConfig.__post_init__`` re-checks most of them,
    but direct/programmatic construction of :class:`RouteConfig` is a
    public surface; these checks are the final boundary so an enabled
    route can never disappear into zero expansions or produce a target
    with two competing addressing authorities.

    Raises
    ------
    ConfigValidationError
        If the route violates an expansion precondition.
    """
    if not rc.source_adapters:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: source_adapters must not be empty",
            section_path=f"routes.{rc.route_id}",
        )
    if not rc.dest_adapters:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: dest_adapters must not be empty",
            section_path=f"routes.{rc.route_id}",
        )
    if rc.dest_destination is not None:
        if len(rc.dest_adapters) != 1:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: dest_destination addresses one "
                f"transport-specific entity and requires exactly one dest "
                f"adapter, got {len(rc.dest_adapters)}",
                section_path=f"routes.{rc.route_id}",
            )
        if rc.dest_channel is not None:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: dest_destination is mutually "
                "exclusive with dest_channel/dest_room",
                section_path=f"routes.{rc.route_id}",
            )
    if rc.context_map is not None:
        if len(rc.source_adapters) != 1 or len(rc.dest_adapters) != 1:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map requires exactly "
                "one source adapter and one dest adapter",
                section_path=f"routes.{rc.route_id}",
            )
        conflicts = [
            name
            for name, value in (
                ("source_channel/source_room", rc.source_channel),
                ("dest_channel/dest_room", rc.dest_channel),
                ("dest_destination", rc.dest_destination),
            )
            if value is not None
        ]
        if conflicts:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map is mutually "
                f"exclusive with {conflicts}",
                section_path=f"routes.{rc.route_id}",
            )


# ---------------------------------------------------------------------------
# Standard (non-mapping) expansion
# ---------------------------------------------------------------------------


def _expand_standard_route(
    rc: RouteConfig,
    *,
    swap_direction: bool,
) -> list[Route]:
    """Expand a standard route into forward and/or reverse core routes.

    Expansion rules:

    * One ``RouteConfig`` with N source adapters produces N ``Route``
      objects per direction — one per source adapter.
    * Each expanded route gets all dest adapters as :class:`RouteTarget`
      entries, sharing one structured destination (if any).
    * If *swap_direction* is ``True``, source and dest adapters are
      swapped (used for ``dest_to_source`` and the reverse leg of
      ``bidirectional`` routes); reverse legs deliver to the configured
      source side, which carries no structured destination.
    * The :class:`BridgePolicy` ``allowed_event_types`` are mapped to
      :attr:`RouteSource.event_kinds` when non-empty.
    * Route IDs are suffixed to ensure uniqueness when expanding:
      a single forward source keeps the plain ID, multi-source forward
      legs use ``<id>__<idx>``, and reverse legs use ``<id>__rev_<idx>``.
    """
    if swap_direction:
        source_ids = rc.dest_adapters
        dest_ids = rc.source_adapters
        source_channel = rc.dest_channel
        dest_channel = rc.source_channel
        origin_label = rc.dest_origin_label
        # Reverse legs deliver to the configured source side, which has no
        # structured destination: ``dest_destination`` addresses the route's
        # configured dest side only.
        destination = None
    else:
        source_ids = rc.source_adapters
        dest_ids = rc.dest_adapters
        source_channel = rc.source_channel
        dest_channel = rc.dest_channel
        origin_label = rc.source_origin_label
        destination = _convert_destination(rc.dest_destination)

    event_kinds, route_policy = _route_policy_parts(rc)

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
            RouteTarget(adapter=did, channel=dest_channel, destination=destination)
            for did in dest_ids
        ]

        source = RouteSource(
            adapter=src_id,
            event_kinds=event_kinds,
            channel=source_channel,
            origin_label=origin_label,
        )

        route = Route(
            id=route_id,
            source=source,
            targets=targets,
            enabled=rc.enabled,
            policy=route_policy,
        )
        routes.append(route)

    return routes


# ---------------------------------------------------------------------------
# context_map expansion
# ---------------------------------------------------------------------------


def _context_map_token(source_context: str) -> str:
    """Return the stable route-ID token for one mapped source context.

    Mapping-leg IDs participate in durable route/plan provenance.  They must
    therefore depend on the mapped context itself, not its ordinal position in
    the current map: adding an unrelated key must not rename existing legs.
    A short SHA-256 token keeps arbitrary opaque contexts out of route IDs while
    remaining deterministic across processes and configuration reloads.
    """
    digest = hashlib.sha256(source_context.encode("utf-8")).hexdigest()[
        :_CONTEXT_MAP_TOKEN_HEX_LENGTH
    ]
    return f"h{digest}"


def _expand_context_map_route(rc: RouteConfig) -> list[ExpandedRouteLeg]:
    """Expand a ``context_map`` route into per-context core route legs.

    Entries are iterated in sorted-key order for deterministic output.
    Their IDs use a stable token derived from the source context, so adding or
    removing an unrelated mapping does not rename existing durable route
    identities.  Forward legs are ``<route_id>__map<token>__fwd`` and reverse
    legs are ``<route_id>__map<token>__rev``.  The forward leg carries the source
    side as ``RouteSource.channel`` and the entry's dest side on the
    target; the reverse leg (only for entries with ``dest_context``)
    swaps adapters, contexts, and label sides and never carries a
    structured destination.
    """
    assert rc.context_map is not None  # guarded by caller

    src_id = rc.source_adapters[0]
    dst_id = rc.dest_adapters[0]
    event_kinds, route_policy = _route_policy_parts(rc)

    direction = rc.directionality
    create_fwd = direction in (
        RouteDirectionality.SOURCE_TO_DEST,
        RouteDirectionality.BIDIRECTIONAL,
    )
    create_rev = direction in (
        RouteDirectionality.DEST_TO_SOURCE,
        RouteDirectionality.BIDIRECTIONAL,
    )

    legs: list[ExpandedRouteLeg] = []

    tokens: dict[str, str] = {}
    for key, entry in sorted(rc.context_map.items()):
        token = _context_map_token(key)
        prior_key = tokens.get(token)
        if prior_key is not None and prior_key != key:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map source contexts "
                f"{prior_key!r} and {key!r} collide on expansion token "
                f"{token!r}; split the mappings into separate routes",
                section_path=f"routes.{rc.route_id}",
            )
        tokens[token] = key
        # Resolve effective per-entry labels: entry label takes precedence
        # over route-level label.  Use 'is not None' so that an explicit
        # empty string ("") is preserved (sentinel for suppress fallback).
        effective_source_label = (
            entry.source_origin_label
            if entry.source_origin_label is not None
            else rc.source_origin_label
        )
        effective_dest_label = (
            entry.dest_origin_label
            if entry.dest_origin_label is not None
            else rc.dest_origin_label
        )

        if create_fwd:
            legs.append(
                ExpandedRouteLeg(
                    route=Route(
                        id=f"{rc.route_id}__map{token}__fwd",
                        source=RouteSource(
                            adapter=src_id,
                            event_kinds=event_kinds,
                            channel=key,
                            origin_label=effective_source_label,
                        ),
                        targets=[
                            RouteTarget(
                                adapter=dst_id,
                                channel=entry.dest_context,
                                destination=_convert_destination(
                                    entry.dest_destination
                                ),
                            )
                        ],
                        enabled=rc.enabled,
                        policy=route_policy,
                    ),
                    config_route_id=rc.route_id,
                    direction=_DIRECTION_FORWARD,
                    mapping_source_context=key,
                    mapping_dest_context=entry.dest_context,
                )
            )

        if create_rev and entry.dest_context is not None:
            legs.append(
                ExpandedRouteLeg(
                    route=Route(
                        id=f"{rc.route_id}__map{token}__rev",
                        source=RouteSource(
                            adapter=dst_id,
                            event_kinds=event_kinds,
                            channel=entry.dest_context,
                            origin_label=effective_dest_label,
                        ),
                        targets=[
                            RouteTarget(adapter=src_id, channel=key, destination=None)
                        ],
                        enabled=rc.enabled,
                        policy=route_policy,
                    ),
                    config_route_id=rc.route_id,
                    direction=_DIRECTION_REVERSE,
                    mapping_source_context=key,
                    mapping_dest_context=entry.dest_context,
                )
            )

    return legs


# ---------------------------------------------------------------------------
# Public expansion API
# ---------------------------------------------------------------------------


def expand_route_config(rc: RouteConfig) -> list[ExpandedRouteLeg]:
    """Expand ONE route config into its core route legs.

    The route is expanded regardless of its ``enabled`` flag — callers
    that must skip disabled routes filter them (or use
    :func:`expand_route_configs`, which does).  No cross-route ID
    collision check is performed here; :func:`expand_route_configs`
    owns the full-set check.  Expansion order is deterministic.

    Returns
    -------
    list[ExpandedRouteLeg]
        Forward and (when the directionality creates them) reverse
        legs, in deterministic order.

    Raises
    ------
    ConfigValidationError
        If the route violates an expansion precondition (see
        :func:`_check_route_boundaries`) or carries an unrecognized
        directionality.
    """
    _check_route_boundaries(rc)

    direction = rc.directionality
    if direction not in (
        RouteDirectionality.SOURCE_TO_DEST,
        RouteDirectionality.DEST_TO_SOURCE,
        RouteDirectionality.BIDIRECTIONAL,
    ):
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: unrecognized directionality "
            f"{direction!r}; expected one of "
            f"{', '.join(d.value for d in RouteDirectionality)}",
            section_path=f"routes.{rc.route_id}",
        )

    if rc.context_map is not None:
        return _expand_context_map_route(rc)

    legs: list[ExpandedRouteLeg] = []
    if direction == RouteDirectionality.SOURCE_TO_DEST:
        legs.extend(
            ExpandedRouteLeg(
                route=route,
                config_route_id=rc.route_id,
                direction=_DIRECTION_FORWARD,
                mapping_source_context=None,
                mapping_dest_context=None,
            )
            for route in _expand_standard_route(rc, swap_direction=False)
        )
    elif direction == RouteDirectionality.DEST_TO_SOURCE:
        legs.extend(
            ExpandedRouteLeg(
                route=route,
                config_route_id=rc.route_id,
                direction=_DIRECTION_REVERSE,
                mapping_source_context=None,
                mapping_dest_context=None,
            )
            for route in _expand_standard_route(rc, swap_direction=True)
        )
    elif direction == RouteDirectionality.BIDIRECTIONAL:
        legs.extend(
            ExpandedRouteLeg(
                route=route,
                config_route_id=rc.route_id,
                direction=_DIRECTION_FORWARD,
                mapping_source_context=None,
                mapping_dest_context=None,
            )
            for route in _expand_standard_route(rc, swap_direction=False)
        )
        legs.extend(
            ExpandedRouteLeg(
                route=route,
                config_route_id=rc.route_id,
                direction=_DIRECTION_REVERSE,
                mapping_source_context=None,
                mapping_dest_context=None,
            )
            for route in _expand_standard_route(rc, swap_direction=True)
        )
    return legs


def expand_route_configs(route_config_set: RouteConfigSet) -> list[ExpandedRouteLeg]:
    """Expand every ENABLED route config in config order.

    Disabled routes are skipped (debug-logged).  Expanded route IDs are
    checked for collisions across the full set so a mis-cased config
    route ID can never shadow another route's expansion.

    Returns
    -------
    list[ExpandedRouteLeg]
        The expanded legs in config order; derive the
        ``expanded_id -> config_route_id`` provenance mapping from
        :attr:`ExpandedRouteLeg.route.id` / :attr:`.config_route_id`.

    Raises
    ------
    ConfigValidationError
        If a route violates an expansion precondition or two expanded
        route IDs collide.
    """
    all_legs: list[ExpandedRouteLeg] = []
    provenance: dict[str, str] = {}  # expanded_id → config_route_id

    for rc in route_config_set.routes:
        if not rc.enabled:
            _logger.debug("Skipping disabled route %r", rc.route_id)
            continue

        new_legs = expand_route_config(rc)

        for leg in new_legs:
            if leg.route.id in provenance:
                raise ConfigValidationError(
                    f"Expanded route ID collision: {leg.route.id!r} from route "
                    f"{rc.route_id!r} conflicts with route "
                    f"{provenance[leg.route.id]!r}. Route IDs must be unique and "
                    f"must not match the expansion patterns "
                    f"{EXPANSION_ID_PATTERNS_HINT}.",
                    section_path=f"routes.{rc.route_id}",
                )
            provenance[leg.route.id] = rc.route_id

        all_legs.extend(new_legs)

    return all_legs
