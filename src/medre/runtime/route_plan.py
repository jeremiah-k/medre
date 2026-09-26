"""Offline route-plan model for pre-flight topology preview.

Produces a deterministic, JSON-safe expansion of route configs without
starting any adapter or performing network/hardware I/O.

The plan reuses the config compiler's pure expansion function
(:func:`medre.config.route_expansion.expand_route_config`) plus the
engine's :func:`~medre.runtime.route_engine.check_route_loops`, and adds
two things the expansion alone does not surface:

* per-leg origin-label provenance including adapter fallback
  (per-entry → route → adapter → unset), so the plan shows the
  *effective* label the renderer would use rather than only what the
  expansion copied onto the Route object; and
* a config-level walk that includes disabled routes (the compiler's
  ``expand_route_configs`` skips them).

Expansion failures are attributed per route via the leg provenance
fields — the plan never parses expanded route IDs.

No adapter SDK is imported and no adapter is started.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from medre.config.errors import ConfigValidationError
from medre.config.route_expansion import EXPANSION_ID_PATTERNS_HINT, expand_route_config
from medre.config.routes import RouteDirectionality
from medre.runtime.route_engine import RouteValidationError, check_route_loops

if TYPE_CHECKING:
    from medre.config.model import RuntimeConfig
    from medre.config.route_expansion import ExpandedRouteLeg
    from medre.config.routes import RouteConfig

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
    transport: str  # registered built-in transport name
    enabled: bool
    origin_label: str  # the adapter's configured origin_label (fallback value)


@dataclass(frozen=True)
class RoutePlanLeg:
    """One expanded route leg in the plan.

    Attributes
    ----------
    expanded_route_id:
        The full ID assigned by expansion (may include ``__<N>`` /
        ``__rev_<N>`` / ``__map<token>__{fwd,rev}`` suffixes).
    config_route_id:
        Provenance: the config-level route ID this leg was produced from.
    enabled:
        Whether this leg is enabled (mirrors the config route).
    direction:
        ``"source_to_dest"`` or ``"dest_to_source"`` relative to the
        config route's declared source/dest adapters.
    source_adapter_id / dest_adapter_id:
        Physical source and destination adapter IDs for this leg.
    source_transport / dest_transport:
        Registered transport name for each side (the adapter inventory's
        transport label), or ``None`` if unknown.
    source_channel / dest_channel:
        Resolved context values carried on the expanded leg.
    mapping_source_context / mapping_dest_context:
        The ``context_map`` source key and destination context when this
        leg was produced by ``context_map`` expansion, else ``None``.
    dest_destination_kind / dest_destination_hash / dest_destination_name:
        Operator-visible display fields for structured destinations
        carried on the expanded leg's target, else ``None``.
    source_origin_label:
        The effective origin label value for this leg, including
        adapter-level ``origin_label`` fallback.  May be ``None`` (no
        label resolved anywhere) or ``""`` (explicit suppression set at
        the per-entry or route level).
    source_origin_label_source:
        Live provenance category describing where *source_origin_label*
        came from: ``"per_entry"`` (a ``context_map`` entry's label),
        ``"route"`` (route-level ``source_origin_label`` /
        ``dest_origin_label``), ``"adapter"`` (source adapter's
        ``origin_label`` fallback applied at plan time), or ``"unset"``
        (no label resolved at any level).  When *source_origin_label*
        is ``""`` the label is an explicit suppression regardless of
        source.
    """

    expanded_route_id: str
    config_route_id: str
    enabled: bool
    direction: str
    source_adapter_id: str
    dest_adapter_id: str
    source_transport: str | None
    dest_transport: str | None
    source_channel: str | None
    dest_channel: str | None
    mapping_source_context: str | None
    mapping_dest_context: str | None
    dest_destination_kind: str | None
    dest_destination_hash: str | None
    dest_destination_name: str | None
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
        ambiguous duplicate contexts, ID collision).
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
    resolves origin-label provenance per leg, annotates forward-only
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
    adapter_transports: dict[str, str] = {}
    adapter_origin_labels: dict[str, str] = {}
    adapters: list[AdapterSummary] = []
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapter_transports[adapter_id] = transport
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

        # Validate adapter references before expansion: a route that points
        # at adapters not in config would otherwise expand with
        # source_transport=None and a misleadingly clean plan. Captured as a
        # per-route error (like other expansion failures), not raised.
        missing = [aid for aid in rc.source_adapters if aid not in adapter_transports]
        missing += [aid for aid in rc.dest_adapters if aid not in adapter_transports]
        if missing:
            route_entries.append(
                RoutePlanEntry(
                    route_id=rc.route_id,
                    enabled=True,
                    directionality=rc.directionality.value,
                    legs=[],
                    warnings=[],
                    error=f"references unknown adapter(s): {sorted(set(missing))}. "
                    f"Configured adapters: {sorted(adapter_transports.keys())}",
                )
            )
            continue

        # Expand just this route so a failure is attributable to it.
        try:
            expanded_legs = expand_route_config(rc)
        except (RouteValidationError, ConfigValidationError) as exc:
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

        # Cross-route expanded-ID uniqueness (the compiler checks within a
        # full-set expansion; here we check across per-route expansions).
        collision_error = None
        for leg in expanded_legs:
            if leg.route.id in seen_expanded_ids:
                collision_error = (
                    f"Expanded route ID collision: {leg.route.id!r} from route "
                    f"{rc.route_id!r} conflicts with route "
                    f"{seen_expanded_ids[leg.route.id]!r}. Route IDs must be "
                    f"unique and must not match the expansion patterns "
                    f"{EXPANSION_ID_PATTERNS_HINT}."
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

        for leg in expanded_legs:
            seen_expanded_ids[leg.route.id] = rc.route_id

        legs = [
            _build_leg(
                leg,
                rc,
                adapter_transports=adapter_transports,
                adapter_origin_labels=adapter_origin_labels,
            )
            for leg in expanded_legs
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
        all_expanded_routes.extend(leg.route for leg in expanded_legs)

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


def _build_leg(
    leg: ExpandedRouteLeg,
    rc: RouteConfig,
    *,
    adapter_transports: dict[str, str],
    adapter_origin_labels: dict[str, str],
) -> RoutePlanLeg:
    """Build a :class:`RoutePlanLeg` from an expanded leg and its config."""
    route = leg.route
    source_adapter = route.source.adapter or ""
    # targets always has at least one entry for valid routes.
    dest_adapter = ""
    destination = None
    if route.targets:
        dest_adapter = route.targets[0].adapter or ""
        destination = route.targets[0].destination

    # Direction relative to the config's declared source/dest adapters.
    is_forward = leg.direction == "source_to_dest"

    effective_label, label_source = _resolve_effective_origin_label(
        leg=leg,
        rc=rc,
        side_is_source=is_forward,
        adapter_origin_labels=adapter_origin_labels,
    )

    return RoutePlanLeg(
        expanded_route_id=route.id,
        config_route_id=leg.config_route_id,
        enabled=route.enabled,
        direction=leg.direction,
        source_adapter_id=source_adapter,
        dest_adapter_id=dest_adapter,
        source_transport=adapter_transports.get(source_adapter),
        dest_transport=adapter_transports.get(dest_adapter),
        source_channel=route.source.channel,
        dest_channel=route.targets[0].channel if route.targets else None,
        mapping_source_context=leg.mapping_source_context,
        mapping_dest_context=leg.mapping_dest_context,
        dest_destination_kind=None if destination is None else destination.kind,
        dest_destination_hash=(
            None if destination is None else destination.destination_hash
        ),
        dest_destination_name=(
            None if destination is None else destination.destination_name
        ),
        source_origin_label=effective_label,
        source_origin_label_source=label_source,
    )


def _resolve_effective_origin_label(
    *,
    leg: ExpandedRouteLeg,
    rc: RouteConfig,
    side_is_source: bool,
    adapter_origin_labels: dict[str, str],
) -> tuple[str | None, str]:
    """Resolve the effective origin label and its provenance source.

    Returns ``(effective_label, source)`` where *source* is one of
    ``"per_entry"``, ``"route"``, ``"adapter"``, ``"unset"``.

    Precedence mirrors render-time attribution:

    1. Per-entry label (explicit ``""`` suppresses further fallback).
    2. Route-level label (explicit ``""`` suppresses further fallback).
    3. Source adapter ``origin_label`` fallback (when non-empty).
    4. ``None`` (unset).

    The expansion copies the effective per-entry/route-level label onto
    the expanded :class:`Route` object; the adapter fallback is applied
    here so the plan reports the effective label the renderer would use.
    The label side is purely direction-relative: forward legs use the
    config route's source side, reverse legs its dest side.
    """
    # Per-entry label (context_map legs only, keyed by leg provenance).
    entry_label: str | None = None
    if leg.mapping_source_context is not None and rc.context_map is not None:
        entry = rc.context_map.get(leg.mapping_source_context)
        if entry is not None:
            entry_label = (
                entry.source_origin_label if side_is_source else entry.dest_origin_label
            )

    route_label = rc.source_origin_label if side_is_source else rc.dest_origin_label

    if entry_label is not None:
        # Per-entry label wins (includes explicit "" suppression).
        return (entry_label, "per_entry")
    if route_label is not None:
        # Route-level label wins (includes explicit "" suppression).
        return (route_label, "route")
    # No per-entry or route-level label: apply adapter fallback.
    src_adapter = leg.route.source.adapter
    adapter_label = adapter_origin_labels.get(src_adapter, "") if src_adapter else ""
    if adapter_label:
        return (adapter_label, "adapter")
    return (None, "unset")


def _route_warnings(rc: RouteConfig) -> list[str]:
    """Non-blocking warnings for a config route (e.g. fan-in annotation).

    Fan-in — several source contexts sharing one ``dest_context`` — is
    only meaningful when the route creates forward legs exclusively
    (``source_to_dest``); with reverse legs present the config rejects
    duplicate ``dest_context`` values outright.  Entries carrying a
    structured ``dest_destination`` never participate (each addresses a
    unique destination entity).
    """
    warnings: list[str] = []
    context_map = rc.context_map
    if not context_map or len(context_map) < 2:
        return warnings
    if rc.directionality != RouteDirectionality.SOURCE_TO_DEST:
        return warnings
    dest_to_sources: dict[str, list[str]] = {}
    for key, entry in context_map.items():
        if entry.dest_context is None:
            continue
        dest_to_sources.setdefault(entry.dest_context, []).append(key)
    for dest_context, source_keys in sorted(dest_to_sources.items()):
        if len(source_keys) > 1:
            warnings.append(
                f"fan-in: same dest_context {dest_context} for source contexts "
                f"{', '.join(sorted(source_keys))}"
            )
    return warnings
