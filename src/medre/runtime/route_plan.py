"""Offline route-plan model for pre-flight topology preview.

Produces a deterministic, JSON-safe expansion of route configs without
starting any adapter or performing network/hardware I/O.

The plan reuses the pure expansion functions in
:mod:`medre.runtime.route_engine` (:func:`_expand_route_config`,
:func:`_expand_channel_room_map_route`, :func:`check_route_loops`) and adds
two things the engine does not surface on its own:

* per-leg origin-label provenance (per-entry → route → adapter → unset), and
* a config-level walk that includes disabled routes (the engine skips them).

No adapter SDK is imported and no adapter is started.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from medre.config.routes import ChannelRoomMapEntry, RouteDirectionality
from medre.runtime.route_engine import (
    RouteValidationError,
    _expand_channel_room_map_route,
    _expand_route_config,
    check_route_loops,
)

if TYPE_CHECKING:
    from medre.config.model import RuntimeConfig

__all__ = [
    "AdapterSummary",
    "RoutePlan",
    "RoutePlanEntry",
    "RoutePlanLeg",
    "build_route_plan",
]


# ---------------------------------------------------------------------------
# Plan models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterSummary:
    """One configured adapter summarized for plan output."""

    adapter_id: str
    transport: str  # "matrix", "meshtastic", "meshcore", "lxmf"
    enabled: bool
    origin_label: str  # the adapter's configured origin_label (fallback value)


@dataclass(frozen=True)
class RoutePlanLeg:
    """One expanded route leg in the plan.

    Attributes
    ----------
    expanded_route_id:
        The full ID assigned by expansion (may include ``__ch<N>__`` /
        ``__rev_<N>`` / ``__<N>`` suffixes).
    config_route_id:
        Provenance: the config-level route ID this leg was produced from.
    enabled:
        Whether this leg is enabled (mirrors the config route).
    direction:
        ``"source_to_dest"`` or ``"dest_to_source"`` relative to the
        config route's declared source/dest adapters.
    source_adapter_id / dest_adapter_id:
        Physical source and destination adapter IDs for this leg.
    source_platform / dest_platform:
        Transport platform string for each side (``"matrix"``,
        ``"meshtastic"``, ...), or ``None`` if unknown.
    source_channel / dest_channel:
        Resolved channel/room values carried on the expanded leg.
    channel_room_map_key:
        The ``channel_room_map`` key (e.g. ``"0"``) when this leg was
        produced by channel_room_map expansion, else ``None``.
    channel_room_map_room:
        The Matrix room ID for that key, else ``None``.
    source_origin_label:
        The resolved origin label value carried on the expanded leg
        (may be ``None`` or ``""``).
    source_origin_label_source:
        Where the resolved label came from: ``"per_entry"``, ``"route"``,
        ``"adapter"``, or ``"unset"``.  When *source_origin_label* is
        ``""`` the label is an explicit suppression regardless of source.
    """

    expanded_route_id: str
    config_route_id: str
    enabled: bool
    direction: str
    source_adapter_id: str
    dest_adapter_id: str
    source_platform: str | None
    dest_platform: str | None
    source_channel: str | None
    dest_channel: str | None
    channel_room_map_key: str | None
    channel_room_map_room: str | None
    source_origin_label: str | None
    source_origin_label_source: str


@dataclass(frozen=True)
class RoutePlanEntry:
    """One config-level route and its expansion outcome.

    Attributes
    ----------
    route_id:
        The config-level route ID.
    enabled:
        Whether the route is enabled in config.
    directionality:
        The config route's directionality value (``"source_to_dest"``,
        ``"dest_to_source"``, ``"bidirectional"``).
    legs:
        Expanded legs produced from this route.  Empty when disabled or
        when expansion failed.
    warnings:
        Non-blocking notes, e.g. fan-in annotations.
    error:
        Non-``None`` when expansion failed for this route (e.g.
        duplicate-room ambiguity, platform mismatch, ID collision).
    """

    route_id: str
    enabled: bool
    directionality: str
    legs: list[RoutePlanLeg]
    warnings: list[str]
    error: str | None


@dataclass(frozen=True)
class RoutePlan:
    """Full route-plan output."""

    adapters: list[AdapterSummary]
    routes: list[RoutePlanEntry]
    total_legs: int
    loops: list[str]  # from check_route_loops


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_route_plan(config: RuntimeConfig) -> RoutePlan:
    """Build a route plan from a loaded config without starting adapters.

    Walks every config route (including disabled ones), expands each
    enabled route in isolation so failures are attributed precisely,
    resolves origin-label provenance per leg, detects duplicate-room
    fan-in, and runs loop detection over the aggregate expansion.

    Parameters
    ----------
    config:
        A loaded :class:`~medre.config.model.RuntimeConfig`.

    Returns
    -------
    RoutePlan
        Deterministic, JSON-safe plan.  Per-route expansion errors are
        captured on the offending :class:`RoutePlanEntry`.``error`` field
    rather than raised.
    """
    # -- Adapter inventory + lookup maps ------------------------------------
    adapter_platforms: dict[str, str] = {}
    adapter_origin_labels: dict[str, str] = {}
    adapters: list[AdapterSummary] = []
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapter_platforms[adapter_id] = transport
        origin = ""
        if rtc.config is not None:
            origin = getattr(rtc.config, "origin_label", "") or ""
        adapter_origin_labels[adapter_id] = origin
        adapters.append(
            AdapterSummary(
                adapter_id=adapter_id,
                transport=transport,
                enabled=rtc.enabled,
                origin_label=origin,
            )
        )

    # -- Per-route expansion ------------------------------------------------
    route_entries: list[RoutePlanEntry] = []
    all_expanded_routes: list = []  # list[Route] for loop detection
    seen_expanded_ids: dict[str, str] = {}  # expanded_id -> config_route_id

    for rc in config.routes.routes:
        if not rc.enabled:
            route_entries.append(
                RoutePlanEntry(
                    route_id=rc.route_id,
                    enabled=False,
                    directionality=rc.directionality.value,
                    legs=[],
                    warnings=["disabled"],
                    error=None,
                )
            )
            continue

        # Expand just this route so a failure is attributable to it.
        try:
            expanded = _expand_single_route(rc, adapter_platforms)
        except RouteValidationError as exc:
            route_entries.append(
                RoutePlanEntry(
                    route_id=rc.route_id,
                    enabled=True,
                    directionality=rc.directionality.value,
                    legs=[],
                    warnings=[],
                    error=str(exc),
                )
            )
            continue

        # Cross-route expanded-ID uniqueness (engine checks within a single
        # call; here we check across routes).
        collision_error = None
        for r in expanded:
            if r.id in seen_expanded_ids:
                collision_error = (
                    f"Expanded route ID collision: {r.id!r} from route "
                    f"{rc.route_id!r} conflicts with route "
                    f"{seen_expanded_ids[r.id]!r}. Route IDs must be unique "
                    f"and must not match the expansion pattern "
                    f"'<id>__<N>', '<id>__rev_<N>', or "
                    f"'<id>__ch<channel>__<direction>'."
                )
                break
        if collision_error is not None:
            route_entries.append(
                RoutePlanEntry(
                    route_id=rc.route_id,
                    enabled=True,
                    directionality=rc.directionality.value,
                    legs=[],
                    warnings=[],
                    error=collision_error,
                )
            )
            continue

        for r in expanded:
            seen_expanded_ids[r.id] = rc.route_id

        legs = [
            _build_leg(
                r,
                rc,
                adapter_platforms=adapter_platforms,
                adapter_origin_labels=adapter_origin_labels,
            )
            for r in expanded
        ]
        warnings = _route_warnings(rc)

        route_entries.append(
            RoutePlanEntry(
                route_id=rc.route_id,
                enabled=True,
                directionality=rc.directionality.value,
                legs=legs,
                warnings=warnings,
                error=None,
            )
        )
        all_expanded_routes.extend(expanded)

    # -- Loop detection over the aggregate expansion ------------------------
    loops = check_route_loops(all_expanded_routes)

    total_legs = sum(len(e.legs) for e in route_entries)

    return RoutePlan(
        adapters=adapters,
        routes=route_entries,
        total_legs=total_legs,
        loops=loops,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _expand_single_route(rc, adapter_platforms: dict[str, str]) -> list:
    """Dispatch a single RouteConfig to the right expander.

    Mirrors the per-route branch of ``_expand_all_routes`` without the
    provenance bookkeeping, so a failure is attributable to *rc* alone.
    """
    if rc.channel_room_map is not None:
        return _expand_channel_room_map_route(rc, adapter_platforms)
    direction = rc.directionality
    if direction == RouteDirectionality.SOURCE_TO_DEST:
        return _expand_route_config(rc)
    if direction == RouteDirectionality.DEST_TO_SOURCE:
        return _expand_route_config(rc, swap_direction=True)
    if direction == RouteDirectionality.BIDIRECTIONAL:
        return _expand_route_config(rc) + _expand_route_config(rc, swap_direction=True)
    return []


def _channel_room_map_key(expanded_id: str, route_id: str) -> str | None:
    """Extract the ``channel_room_map`` key from an expanded route ID.

    Expanded IDs for channel_room_map legs look like
    ``"{route_id}__ch{ch}__matrix_to_meshtastic"``.  Returns ``None`` for
    IDs that do not carry the ``__ch`` marker.
    """
    marker = f"{route_id}__ch"
    if not expanded_id.startswith(marker):
        return None
    rest = expanded_id[len(marker) :]
    # rest == "{ch}__matrix_to_meshtastic" (or meshtastic_to_matrix)
    return rest.split("__", 1)[0]


def _build_leg(
    route,
    rc,
    *,
    adapter_platforms: dict[str, str],
    adapter_origin_labels: dict[str, str],
) -> RoutePlanLeg:
    """Build a :class:`RoutePlanLeg` from an expanded Route and its config."""
    source_adapter = route.source.adapter or ""
    # targets always has at least one entry for valid routes.
    dest_adapter = ""
    if route.targets:
        dest_adapter = route.targets[0].adapter or ""

    # Direction relative to the config's declared source/dest adapters.
    is_forward = source_adapter in rc.source_adapters
    direction = "source_to_dest" if is_forward else "dest_to_source"

    # channel_room_map key/room (None for non-channel_room_map legs).
    crm_key = _channel_room_map_key(route.id, rc.route_id)
    crm_room: str | None = None
    if crm_key is not None and rc.channel_room_map is not None:
        entry = rc.channel_room_map.get(crm_key)
        if isinstance(entry, ChannelRoomMapEntry):
            crm_room = entry.room
        elif isinstance(entry, str):
            crm_room = entry

    resolved_label = route.source.origin_label
    label_source = _resolve_origin_label_source(
        route=route,
        rc=rc,
        is_forward=is_forward,
        resolved=resolved_label,
        adapter_platforms=adapter_platforms,
        adapter_origin_labels=adapter_origin_labels,
    )

    return RoutePlanLeg(
        expanded_route_id=route.id,
        config_route_id=rc.route_id,
        enabled=route.enabled,
        direction=direction,
        source_adapter_id=source_adapter,
        dest_adapter_id=dest_adapter,
        source_platform=adapter_platforms.get(source_adapter),
        dest_platform=adapter_platforms.get(dest_adapter),
        source_channel=route.source.channel,
        dest_channel=route.targets[0].channel if route.targets else None,
        channel_room_map_key=crm_key,
        channel_room_map_room=crm_room,
        source_origin_label=resolved_label,
        source_origin_label_source=label_source,
    )


def _resolve_origin_label_source(
    *,
    route,
    rc,
    is_forward: bool,
    resolved: str | None,
    adapter_platforms: dict[str, str],
    adapter_origin_labels: dict[str, str],
) -> str:
    """Determine where an expanded leg's origin label came from.

    Precedence mirrors the expansion code: per-entry → route-level →
    (adapter fallback, only as a display attribution).  Returns one of
    ``"per_entry"``, ``"route"``, ``"adapter"``, ``"unset"``.
    """
    # Which config-level label side applies to this physical leg.
    # channel_room_map legs select source/dest side based on physical
    # direction and which config adapter is Matrix.
    if rc.channel_room_map is not None and rc.source_adapters:
        fwd_is_matrix_to_mesh = adapter_platforms.get(rc.source_adapters[0]) == "matrix"
        leg_is_matrix_to_mesh = adapter_platforms.get(route.source.adapter) == "matrix"
        if leg_is_matrix_to_mesh:
            side_is_source = fwd_is_matrix_to_mesh
        else:
            side_is_source = not fwd_is_matrix_to_mesh
    else:
        # Non-channel_room_map: forward leg uses source side, reverse dest.
        side_is_source = is_forward

    # Per-entry label (channel_room_map only).
    entry_label: str | None = None
    crm_key = _channel_room_map_key(route.id, rc.route_id)
    if crm_key is not None and rc.channel_room_map is not None:
        entry = rc.channel_room_map.get(crm_key)
        if isinstance(entry, ChannelRoomMapEntry):
            entry_label = (
                entry.source_origin_label if side_is_source else entry.dest_origin_label
            )

    route_label = rc.source_origin_label if side_is_source else rc.dest_origin_label

    if entry_label is not None:
        return "per_entry"
    if route_label is not None:
        return "route"
    if resolved is None:
        return "unset"
    # resolved is a non-None string with no entry/route attribution:
    # attribute to the adapter fallback when it matches.
    src_adapter = route.source.adapter
    if (
        src_adapter is not None
        and resolved == adapter_origin_labels.get(src_adapter, "")
        and resolved != ""
    ):
        return "adapter"
    return "unset"


def _route_warnings(rc) -> list[str]:
    """Non-blocking warnings for a config route (e.g. fan-in annotation)."""
    warnings: list[str] = []
    if rc.channel_room_map is None or len(rc.channel_room_map) < 2:
        return warnings
    room_to_channels: dict[str, list[str]] = {}
    for ch, entry in rc.channel_room_map.items():
        room = entry.room if isinstance(entry, ChannelRoomMapEntry) else entry
        room_to_channels.setdefault(room, []).append(ch)
    for room, channels in sorted(room_to_channels.items()):
        if len(channels) > 1:
            warnings.append(
                f"fan-in: same room {room} for channels "
                f"{', '.join(sorted(channels))}"
            )
    return warnings
