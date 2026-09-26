"""Deterministic runtime routing engine that bridges config routes to the core Router.

This module validates adapter references against the assembled runtime
adapter IDs, delegates config→core route expansion to the configuration
compiler (:mod:`medre.config.route_expansion`), and registers the
resulting core :class:`~medre.core.routing.models.Route` objects with
the :class:`~medre.core.routing.router.Router`.

It is deliberately transport-agnostic and SDK-free: it contains no
platform-specific expansion logic.  Route ordering is deterministic:
routes are registered in the same order they appear in the
:class:`RouteConfigSet`.

Public symbols
--------------
* :func:`build_runtime_routes` — expand config routes into core Route objects
* :func:`validate_route_adapter_refs` — validate adapter references
* :func:`register_routes` — build, validate, and register routes on a Router
* :func:`compute_startup_readiness` — derive startup route readiness from adapter states
* :class:`RouteValidationError` — raised on invalid route adapter references
* :class:`RouteOperationalState` — typed route readiness / state enum
* :class:`DegradedRoute` — a route registered with partial target loss
* :class:`SkippedRoute` — a route skipped due to adapter build failure
* :class:`UnavailableRoute` — a route unavailable due to unknown adapter refs
* :class:`RouteEligibility` — structured route readiness metadata
* :class:`RouteRegistrationResult` — frozen dataclass with routes and eligibility
* :class:`RouteStartupReadiness` — startup-derived route readiness from adapter states
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from medre.core.routing.models import Route
from medre.core.routing.router import Router
from medre.runtime.errors import RuntimeConfigError

if TYPE_CHECKING:
    from medre.config.routes import RouteConfigSet
    from medre.core.lifecycle.states import AdapterState

__all__ = [
    "DegradedRoute",
    "RouteEligibility",
    "RouteOperationalState",
    "RouteRegistrationResult",
    "RouteStartupReadiness",
    "RouteValidationError",
    "SKIPPED_REASON_SOURCE_START_FAILED",
    "SKIPPED_REASON_TARGETS_START_FAILED",
    "SkippedRoute",
    "UnavailableRoute",
    "build_runtime_routes",
    "compute_startup_readiness",
    "register_routes",
    "validate_route_adapter_refs",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route operational state enum
# ---------------------------------------------------------------------------


class RouteOperationalState(enum.Enum):
    """Typed route readiness / operational state.

    Semantics
    ---------
    CONFIGURED:
        Route is declared in configuration and enabled (not disabled).
        This is the initial state before adapter build outcomes are known.
    REGISTERED:
        Route was successfully registered on the :class:`Router` with
        all source and target adapters built.
    DEGRADED:
        Route is registered but one or more target adapters failed to
        build.  The route delivers only to surviving targets.
    SKIPPED:
        Route could not be registered because its source adapter failed
        to build, or all target adapters failed.
    UNAVAILABLE:
        Route references adapter IDs that are not present in the
        configured adapter set (config error).
    DISABLED:
        Route is explicitly disabled (``enabled=False``) in configuration.
    """

    CONFIGURED = "configured"
    REGISTERED = "registered"
    DEGRADED = "degraded"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Route eligibility metadata models
# ---------------------------------------------------------------------------

# Machine-readable startup-skip reasons (SkippedRoute.reason).  The
# distinction is load-bearing for enforcement: see
# compute_startup_readiness and MedreApp.start — routes skipped because
# every TARGET failed are removed from the router (planning into them
# dead-letters per event), while routes skipped because their SOURCE
# failed stay registered (stored canonical work from that source still
# routes; fresh live ingress cannot arrive from an adapter that never
# started).
SKIPPED_REASON_SOURCE_START_FAILED = "source_adapter_start_failed"
SKIPPED_REASON_TARGETS_START_FAILED = "no_surviving_targets_start_failed"


@dataclass(frozen=True)
class DegradedRoute:
    """A route registered with partial target loss due to adapter build failures.

    Attributes
    ----------
    route_id:
        The expanded route ID that was degraded.
    failed_adapter_ids:
        Adapter IDs of targets that failed to build (sorted).
    """

    route_id: str
    failed_adapter_ids: tuple[str, ...]


@dataclass(frozen=True)
class SkippedRoute:
    """An expanded route that was skipped due to adapter build failure.

    Attributes
    ----------
    route_id:
        The expanded route ID that was skipped.
    reason:
        Machine-readable skip reason: ``SKIPPED_REASON_SOURCE_START_FAILED``
        (startup only: the route's source adapter failed to start) or
        ``SKIPPED_REASON_TARGETS_START_FAILED`` (startup only: no target
        adapter survived startup), or a build-failure reason string.
    failed_adapter_ids:
        Adapter IDs that caused the skip (source or all dest adapters
        that failed to build).
    """

    route_id: str
    reason: str
    failed_adapter_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnavailableRoute:
    """A route that could not be registered due to missing adapter references.

    In normal operation this is always empty because unknown adapter IDs
    raise :class:`RouteValidationError` during validation.  This model
    exists for future orchestration layers that may choose to collect
    rather than raise.

    Attributes
    ----------
    route_id:
        The route ID that is unavailable.
    reason:
        Human-readable reason.
    missing_adapter_ids:
        Adapter IDs that are not present.
    """

    route_id: str
    reason: str
    missing_adapter_ids: tuple[str, ...]


@dataclass(frozen=True)
class RouteStartupReadiness:
    """Startup-derived route readiness based on adapter lifecycle states.

    Unlike :class:`RouteEligibility` (which reflects build-time adapter
    availability), this captures the route state **after** adapters have
    attempted to start.  An adapter that built successfully but failed
    during :meth:`~medre.runtime.app.MedreApp.start` will downgrade
    routes that depend on it.

    Attributes
    ----------
    route_states:
        Mapping from config route ID to :class:`RouteOperationalState`.
        Keys are deterministically sorted.  Covers all config route IDs.
    degraded:
        Expanded routes registered with partial target loss due to
        adapter start failures.
    skipped:
        Expanded routes skipped due to adapter start failures (source
        failed or all targets failed).
    """

    route_states: dict[str, RouteOperationalState]
    degraded: tuple[DegradedRoute, ...]
    skipped: tuple[SkippedRoute, ...]


@dataclass(frozen=True)
class RouteEligibility:
    """Structured route readiness metadata after registration.

    All ``tuple`` fields contain deterministically sorted route IDs.

    Attributes
    ----------
    configured:
        Sorted config route IDs that were enabled (not disabled).
    registered:
        Sorted expanded route IDs that were successfully registered
        on the :class:`Router`.
    disabled:
        Sorted config route IDs where ``enabled=False``.
    degraded:
        Expanded routes registered with partial target loss.
    skipped:
        Expanded routes skipped due to adapter build failures.
    unavailable:
        Routes that could not be registered (empty in normal operation;
        unknown refs raise before reaching this stage).
    route_states:
        Mapping from config route ID to :class:`RouteOperationalState`.
        Keys are deterministically sorted.  Covers all config route IDs:
        enabled routes are REGISTERED, DEGRADED, or SKIPPED; disabled
        routes are DISABLED.
    """

    configured: tuple[str, ...]
    registered: tuple[str, ...]
    disabled: tuple[str, ...]
    degraded: tuple[DegradedRoute, ...]
    skipped: tuple[SkippedRoute, ...]
    unavailable: tuple[UnavailableRoute, ...]
    route_states: dict[str, RouteOperationalState]


@dataclass(frozen=True)
class RouteRegistrationResult:
    """Frozen result of route registration carrying routes and eligibility.

    Attributes
    ----------
    registered_routes:
        Tuple of routes that were successfully registered on the Router.
    eligibility:
        Structured readiness metadata describing configured, registered,
        disabled, degraded, skipped, and unavailable routes.
    provenance:
        Explicit mapping from expanded route ID to config route ID.
        Used to avoid string-prefix inference when mapping expanded
        routes back to their config route origins.
    """

    registered_routes: tuple[Route, ...]
    eligibility: RouteEligibility
    provenance: dict[str, str]

    def __repr__(self) -> str:
        return (
            f"RouteRegistrationResult("
            f"registered_routes=({len(self.registered_routes)} routes), "
            f"eligibility={self.eligibility!r}, "
            f"provenance=({len(self.provenance)} mappings))"
        )


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
                    f"Route {rc.route_id!r} references unknown dest " f"adapter {aid!r}"
                )

    if unknown_refs:
        details = "; ".join(unknown_refs)
        raise RouteValidationError(
            f"Invalid route adapter references: {details}. "
            f"Known adapters: {sorted(adapter_ids)}"
        )


# ---------------------------------------------------------------------------
# Config → core Route expansion (delegated to the config compiler)
# ---------------------------------------------------------------------------


def build_runtime_routes(route_config_set: RouteConfigSet) -> list[Route]:
    """Convert a :class:`RouteConfigSet` into core :class:`Route` objects.

    Only **enabled** routes are included.  Disabled routes are silently
    skipped.  Expansion itself (including ``context_map`` mapping
    expansion) is owned by
    :func:`medre.config.route_expansion.expand_route_configs` — the single
    expansion authority; this function is a thin adapter that unwraps the
    expanded legs into the ordered :class:`Route` list the runtime consumes.

    Loop-prevention note
    --------------------
    A bidirectional route with overlapping source/dest adapters would
    create a direct loop.  This is already prevented at the config level
    by :class:`RouteConfig` validation (no source/dest overlap within a
    single route).  Cross-route loops (route A: X→Y and route B: Y→X)
    are detected by :func:`check_route_loops` and logged as informational
    messages.  Bidirectional bridges are an intentional topology.

    Parameters
    ----------
    route_config_set:
        The validated route configuration set.

    Returns
    -------
    list[Route]
        Ordered list of core route objects ready for registration.

    Raises
    ------
    ConfigValidationError
        If expansion fails (e.g. duplicate expanded route IDs across the
        set, or a direct-constructed route violating runtime-semantic
        boundary invariants).
    """
    from medre.config.route_expansion import expand_route_configs

    return [leg.route for leg in expand_route_configs(route_config_set)]


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


def check_route_loops(routes: list[Route]) -> list[str]:
    """Detect routing loops among the given routes.

    Two levels of detection:

    1. **Fast path** — direct two-adapter loops (A↔B).
    2. **Slow path** — multi-hop cycles via DFS on the directed
       adapter adjacency graph (X→Y→Z→X).

    Both levels return descriptive messages but do **not** block startup.
    The caller logs them at ``INFO`` level — bidirectional bridges are
    an intentional topology, not a misconfiguration.

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
    for src, dst in edges:
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
                loops.append(f"Route cycle detected: {cycle_str}")

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
    built_adapter_ids: frozenset[str] | None = None,
) -> RouteRegistrationResult:
    """Build, validate, and register runtime routes on a :class:`Router`.

    This is the primary entry point called by the runtime builder after
    adapters have been constructed.  It:

    1. Validates that all enabled route adapter references resolve to
       known adapter IDs.
    2. Expands :class:`RouteConfigSet` entries into core :class:`Route`
       objects via the config compiler, with explicit provenance.
    3. Degrades routes that reference adapters which failed to build:
       routes with a failed source adapter are skipped; routes with
       failed target adapters have those targets removed.  This only
       applies when *built_adapter_ids* is provided and differs from
       *adapter_ids*.
    4. Checks for direct routing loops and logs informational messages.
    5. Registers all surviving routes on the *router* in deterministic
       order.

    Parameters
    ----------
    router:
        The core routing engine to populate.
    route_config_set:
        The route configuration set from the runtime config.
    adapter_ids:
        Frozenset of adapter IDs that are configured and enabled.
        Used for config-correctness validation: references to IDs
        not in this set always raise.
    built_adapter_ids:
        Frozenset of adapter IDs that were **successfully** built.
        When ``None`` (the default), it falls back to *adapter_ids*
        for consistent behavior when build status is unavailable.  When provided, routes whose
        source or target adapters are in *adapter_ids* but not in
        *built_adapter_ids* are degraded rather than raising.

    Returns
    -------
    RouteRegistrationResult
        A frozen dataclass of registered routes and structured eligibility
        metadata.  Access ``result.registered_routes`` for the tuple of
        registered :class:`Route` objects and ``result.eligibility`` for
        readiness metadata including per-route operational states.

    Raises
    ------
    RouteValidationError
        If any enabled route references an adapter ID that is not in
        *adapter_ids* (i.e. a truly unknown / typo'd adapter ID).
    ConfigValidationError
        If config route expansion fails in the compiler (e.g. duplicate
        expanded route IDs across the set, or a direct-constructed route
        violating runtime-semantic boundary invariants).
    """
    if built_adapter_ids is None:
        built_adapter_ids = adapter_ids

    # Collect config-level route IDs for eligibility metadata.
    disabled_config_ids: list[str] = []
    enabled_config_ids: list[str] = []
    for rc in route_config_set.routes:
        if rc.enabled:
            enabled_config_ids.append(rc.route_id)
        else:
            disabled_config_ids.append(rc.route_id)

    # Step 1: Validate adapter references against configured IDs.
    validate_route_adapter_refs(route_config_set, adapter_ids)

    # Step 2: Expand core routes via the config compiler, deriving the
    # explicit provenance mapping (expanded route ID → config route ID)
    # from the returned legs — no string-prefix inference.
    from medre.config.route_expansion import expand_route_configs

    legs = expand_route_configs(route_config_set)
    routes = [leg.route for leg in legs]
    provenance = {leg.route.id: leg.config_route_id for leg in legs}

    if not routes:
        _logger.info("No enabled routes to register")
        # Build route_states for disabled routes only.
        route_states: dict[str, RouteOperationalState] = {}
        for rid in sorted(disabled_config_ids):
            route_states[rid] = RouteOperationalState.DISABLED
        eligibility = RouteEligibility(
            configured=tuple(sorted(enabled_config_ids)),
            registered=(),
            disabled=tuple(sorted(disabled_config_ids)),
            degraded=(),
            skipped=(),
            unavailable=(),
            route_states=route_states,
        )
        return RouteRegistrationResult((), eligibility, provenance)

    # Step 3: Degrade routes referencing adapters that failed to build.
    skipped_routes: list[SkippedRoute] = []
    degraded_routes: list[DegradedRoute] = []
    registered_routes: list[Route] = []
    for route in routes:
        src = route.source.adapter
        if src is not None and src not in built_adapter_ids:
            _logger.warning(
                "Degrading route %r: source adapter %r failed to build — "
                "skipping entire route",
                route.id,
                src,
            )
            skipped_routes.append(
                SkippedRoute(
                    route_id=route.id,
                    reason="source_adapter_failed",
                    failed_adapter_ids=(src,),
                )
            )
            continue

        surviving_targets = [
            t
            for t in route.targets
            if t.adapter is None or t.adapter in built_adapter_ids
        ]
        dropped = [t.adapter for t in route.targets if t not in surviving_targets]
        # Capture failed adapter IDs before potential route replacement.
        all_failed_target_ids = tuple(
            sorted(
                t.adapter
                for t in route.targets
                if t.adapter is not None and t.adapter not in built_adapter_ids
            )
        )
        if dropped:
            _logger.warning(
                "Degrading route %r: dest adapters %r failed to build — "
                "removed from targets",
                route.id,
                dropped,
            )
            from dataclasses import replace as _dc_replace

            route = _dc_replace(route, targets=surviving_targets)

        if not surviving_targets:
            _logger.warning(
                "Degrading route %r: no surviving target adapters — " "skipping route",
                route.id,
            )
            skipped_routes.append(
                SkippedRoute(
                    route_id=route.id,
                    reason="no_surviving_targets",
                    failed_adapter_ids=all_failed_target_ids,
                )
            )
            continue

        # Track degraded routes (registered with partial target loss).
        if all_failed_target_ids:
            degraded_routes.append(
                DegradedRoute(
                    route_id=route.id,
                    failed_adapter_ids=all_failed_target_ids,
                )
            )

        registered_routes.append(route)

    if skipped_routes:
        _logger.warning(
            "Degraded %d route(s) due to adapter build failures",
            len(skipped_routes),
        )

    # Step 4: Loop detection — informational only. Bidirectional bridges
    # intentionally create adapter cycles; runtime loop prevention handles
    # source echo and route-trace feedback at delivery time.
    loops = check_route_loops(registered_routes)
    for loop_msg in loops:
        _logger.debug("Route topology cycle: %s", loop_msg)

    # Step 5: Register routes in deterministic order.
    for route in registered_routes:
        router.add_route(route)
        _logger.info(
            "Registered route %r: %s → %s",
            route.id,
            route.source.adapter,
            [t.adapter for t in route.targets],
        )

    _logger.info("Registered %d route(s)", len(registered_routes))

    # Build per-route operational states.

    # Build a reverse provenance: config_route_id → set of expanded route IDs.
    config_to_expanded: dict[str, set[str]] = {}
    for expanded_id, config_id in provenance.items():
        config_to_expanded.setdefault(config_id, set()).add(expanded_id)

    route_states: dict[str, RouteOperationalState] = {}
    # Disabled routes.
    for rid in sorted(disabled_config_ids):
        route_states[rid] = RouteOperationalState.DISABLED
    # Enabled routes: map expanded route IDs to their operational state
    # using explicit provenance (no string-prefix inference).
    for rid in sorted(enabled_config_ids):
        expanded = config_to_expanded.get(rid, set())
        matching_skipped = [sr for sr in skipped_routes if sr.route_id in expanded]
        matching_degraded = [dr for dr in degraded_routes if dr.route_id in expanded]
        matching_registered = [r for r in registered_routes if r.id in expanded]

        if matching_skipped and not matching_registered:
            route_states[rid] = RouteOperationalState.SKIPPED
        elif matching_skipped and matching_registered:
            # Some expanded routes skipped, some registered — degraded.
            route_states[rid] = RouteOperationalState.DEGRADED
        elif matching_degraded:
            route_states[rid] = RouteOperationalState.DEGRADED
        elif matching_registered:
            route_states[rid] = RouteOperationalState.REGISTERED
        else:
            # Should not happen, but defensive.
            route_states[rid] = RouteOperationalState.SKIPPED

    eligibility = RouteEligibility(
        configured=tuple(sorted(enabled_config_ids)),
        registered=tuple(sorted(r.id for r in registered_routes)),
        disabled=tuple(sorted(disabled_config_ids)),
        degraded=tuple(degraded_routes),
        skipped=tuple(skipped_routes),
        unavailable=(),
        route_states=route_states,
    )
    return RouteRegistrationResult(tuple(registered_routes), eligibility, provenance)


# ---------------------------------------------------------------------------
# Startup readiness
# ---------------------------------------------------------------------------


def compute_startup_readiness(
    eligibility: RouteEligibility,
    adapter_states: dict[str, AdapterState],
    provenance: dict[str, str],
    registered_routes: tuple[Route, ...],
    config_routes: RouteConfigSet,
) -> RouteStartupReadiness:
    """Derive startup route readiness from adapter lifecycle states.

    This function is called **after** :meth:`~medre.runtime.app.MedreApp.start`
    has completed adapter startup.  It examines the per-adapter lifecycle
    states (which reflect startup outcomes) and produces a startup readiness
    assessment that is independent of build-time eligibility.

    Rules:

    * **Disabled** routes remain DISABLED.
    * Routes already **SKIPPED** at build time remain SKIPPED (build
      eligibility is the source of truth for build failures).
    * For routes that were REGISTERED or DEGRADED at build time:
      - If all target adapters have state ``FAILED`` → SKIPPED
        (reason: ``no_surviving_targets_start_failed``).  This target-safety
        condition takes precedence when the source also failed.
      - Otherwise, if the source adapter has state ``FAILED`` → SKIPPED
        (reason: ``source_adapter_start_failed``).
      - If some target adapters have state ``FAILED`` but others are
        ``READY`` → DEGRADED.
      - If source and all targets are ``READY`` → REGISTERED.
    * Adapters in states other than ``FAILED`` or ``READY`` (e.g.
      ``DEGRADED``, ``BACKPRESSURED``) are treated as surviving for
      routing purposes.

    Parameters
    ----------
    eligibility:
        Build-time route eligibility from :func:`register_routes`.
    adapter_states:
        Per-adapter lifecycle states populated during startup.
    provenance:
        Explicit mapping from expanded route ID to config route ID
        (from :class:`RouteRegistrationResult`).
    registered_routes:
        Tuple of routes that were registered on the router at build time.
    config_routes:
        The original route configuration set (to inspect disabled routes).

    Returns
    -------
    RouteStartupReadiness
        Startup-derived readiness assessment with per-route states.

    Enforcement contract (see :meth:`~medre.runtime.app.MedreApp.start`):
    only ``SKIPPED_REASON_TARGETS_START_FAILED`` routes are removed from
    the router, and only for LIVE scope.  Routes skipped because their
    SOURCE failed stay registered: routing a stored canonical event keys
    off the event's recorded source adapter, not a live connection, and
    an adapter that never started cannot deliver fresh live ingress
    anyway.  Pruning source-failed routes would silently re-route stored
    work into a false ``no-route`` outcome.
    """
    from medre.core.lifecycle.states import AdapterState

    # Build reverse provenance: config_route_id → set of expanded route IDs.
    config_to_expanded: dict[str, set[str]] = {}
    for expanded_id, config_id in provenance.items():
        config_to_expanded.setdefault(config_id, set()).add(expanded_id)

    # Index registered routes by expanded ID for lookup.
    route_by_id: dict[str, Route] = {r.id: r for r in registered_routes}

    startup_degraded: list[DegradedRoute] = []
    startup_skipped: list[SkippedRoute] = []
    startup_route_states: dict[str, RouteOperationalState] = {}

    # Collect all config route IDs.
    all_config_ids: set[str] = set()
    for rc in config_routes.routes:
        all_config_ids.add(rc.route_id)

    for config_id in sorted(all_config_ids):
        # Check if disabled.
        if config_id in {rid for rid in eligibility.disabled}:
            startup_route_states[config_id] = RouteOperationalState.DISABLED
            continue

        # Check if already skipped at build time.
        expanded = config_to_expanded.get(config_id, set())
        build_skipped = [sr for sr in eligibility.skipped if sr.route_id in expanded]
        if build_skipped and not any(r.id in expanded for r in registered_routes):
            startup_route_states[config_id] = RouteOperationalState.SKIPPED
            continue

        # Routes that were registered (or degraded) at build time.
        # Check each expanded route's adapter states.
        any_registered = False
        any_degraded = False
        any_skipped = False

        for expanded_id in sorted(expanded):
            route = route_by_id.get(expanded_id)
            if route is None:
                continue

            # Target viability is the safety gate and therefore takes
            # precedence over the source-state reason.  If the source and
            # every target both fail startup, the route still has no possible
            # delivery path and must be classified as an all-target failure so
            # LIVE startup enforcement removes it.
            failed_target_ids: list[str] = []
            surviving_count = 0
            for t in route.targets:
                if t.adapter is None:
                    surviving_count += 1
                    continue
                t_state = adapter_states.get(t.adapter)
                if t_state is AdapterState.FAILED:
                    failed_target_ids.append(t.adapter)
                else:
                    surviving_count += 1

            if surviving_count == 0:
                _logger.warning(
                    "Startup readiness: route %r has no surviving targets "
                    "after startup — skipping",
                    expanded_id,
                )
                startup_skipped.append(
                    SkippedRoute(
                        route_id=expanded_id,
                        reason=SKIPPED_REASON_TARGETS_START_FAILED,
                        failed_adapter_ids=tuple(sorted(failed_target_ids)),
                    )
                )
                any_skipped = True
                continue

            src = route.source.adapter
            if src is not None:
                src_state = adapter_states.get(src)
                if src_state is AdapterState.FAILED:
                    _logger.warning(
                        "Startup readiness: route %r source adapter %r "
                        "failed to start — skipping",
                        expanded_id,
                        src,
                    )
                    startup_skipped.append(
                        SkippedRoute(
                            route_id=expanded_id,
                            reason=SKIPPED_REASON_SOURCE_START_FAILED,
                            failed_adapter_ids=(src,),
                        )
                    )
                    any_skipped = True
                    continue

            if failed_target_ids:
                _logger.warning(
                    "Startup readiness: route %r degraded — target "
                    "adapters %r failed to start",
                    expanded_id,
                    failed_target_ids,
                )
                startup_degraded.append(
                    DegradedRoute(
                        route_id=expanded_id,
                        failed_adapter_ids=tuple(sorted(failed_target_ids)),
                    )
                )
                any_degraded = True
                any_registered = True
            else:
                any_registered = True

        if any_skipped and not any_registered:
            startup_route_states[config_id] = RouteOperationalState.SKIPPED
        elif any_degraded:
            startup_route_states[config_id] = RouteOperationalState.DEGRADED
        elif any_registered:
            startup_route_states[config_id] = RouteOperationalState.REGISTERED
        else:
            # Config route was configured but had no expanded routes
            # (shouldn't happen normally).
            startup_route_states[config_id] = RouteOperationalState.SKIPPED

    return RouteStartupReadiness(
        route_states=startup_route_states,
        degraded=tuple(startup_degraded),
        skipped=tuple(startup_skipped),
    )
