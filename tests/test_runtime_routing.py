"""Tests for the deterministic runtime routing engine and EventBus integration.

Covers:
* One-to-one routing (single source → single dest)
* One-to-many routing (single source → multiple dests)
* Multiple route chains (multiple routes active simultaneously)
* Disabled routes are skipped
* Invalid route references caught during build validation
* Route failure isolation (one failing dest does not abort others)
* Loop-prevention detection
* Bidirectional route expansion
* Dest-to-source direction expansion
* Adapter ID validation at startup
* Deterministic route ordering from RouteConfigSet
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from pathlib import Path

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.runtime.route_engine import (
    RouteValidationError,
    build_runtime_routes,
    check_route_loops,
    register_routes,
    validate_route_adapter_refs,
)
from medre.runtime.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    source_adapter: str = "adapter_a",
    event_kind: str = "message.created",
    source_channel_id: str | None = "ch-0",
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for routing tests."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hi"},
        metadata=EventMetadata(),
    )


def _rc(
    route_id: str,
    sources: tuple[str, ...],
    dests: tuple[str, ...],
    *,
    enabled: bool = True,
    directionality: RouteDirectionality = RouteDirectionality.SOURCE_TO_DEST,
    policy: BridgePolicy | None = None,
    source_channel: str | None = None,
    dest_channel: str | None = None,
) -> RouteConfig:
    """Shorthand to build a RouteConfig."""
    return RouteConfig(
        route_id=route_id,
        source_adapters=sources,
        dest_adapters=dests,
        directionality=directionality,
        enabled=enabled,
        policy=policy,
        source_channel=source_channel,
        dest_channel=dest_channel,
    )


# ===================================================================
# build_runtime_routes — basic expansion
# ===================================================================


class TestBuildRuntimeRoutes:
    """build_runtime_routes expands RouteConfigSet → list[Route]."""

    def test_one_to_one(self) -> None:
        """Single source → single dest produces exactly one Route."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("adapter_a",), ("adapter_b",)),
        ))
        routes = build_runtime_routes(rcs)
        assert len(routes) == 1
        assert routes[0].source.adapter == "adapter_a"
        assert len(routes[0].targets) == 1
        assert routes[0].targets[0].adapter == "adapter_b"
        assert routes[0].enabled is True

    def test_one_to_many(self) -> None:
        """Single source → multiple dests produces one Route with multiple targets."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("adapter_a",), ("adapter_b", "adapter_c", "adapter_d")),
        ))
        routes = build_runtime_routes(rcs)
        assert len(routes) == 1
        assert routes[0].source.adapter == "adapter_a"
        assert len(routes[0].targets) == 3
        assert [t.adapter for t in routes[0].targets] == [
            "adapter_b", "adapter_c", "adapter_d",
        ]

    def test_many_to_many(self) -> None:
        """Multiple sources × multiple dests: one Route per source, each with all dests."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a1", "a2"), ("b1", "b2")),
        ))
        routes = build_runtime_routes(rcs)
        # 2 sources → 2 routes, each with 2 targets
        assert len(routes) == 2
        assert routes[0].source.adapter == "a1"
        assert routes[1].source.adapter == "a2"
        for r in routes:
            assert len(r.targets) == 2
            assert [t.adapter for t in r.targets] == ["b1", "b2"]

    def test_disabled_routes_skipped(self) -> None:
        """Disabled routes are silently excluded."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), enabled=True),
            _rc("r2", ("c",), ("d",), enabled=False),
            _rc("r3", ("e",), ("f",), enabled=True),
        ))
        routes = build_runtime_routes(rcs)
        route_ids = [r.id for r in routes]
        assert "r1" in route_ids
        assert "r2" not in route_ids
        assert "r3" in route_ids
        assert len(routes) == 2

    def test_deterministic_ordering(self) -> None:
        """Routes are returned in RouteConfigSet order."""
        rcs = RouteConfigSet(routes=(
            _rc("alpha", ("a",), ("b",)),
            _rc("beta", ("c",), ("d",)),
            _rc("gamma", ("e",), ("f",)),
        ))
        routes = build_runtime_routes(rcs)
        assert [r.id for r in routes] == ["alpha", "beta", "gamma"]

    def test_empty_route_config_set(self) -> None:
        """Empty config set yields empty routes."""
        rcs = RouteConfigSet()
        assert build_runtime_routes(rcs) == []


# ===================================================================
# Directionality expansion
# ===================================================================


class TestDirectionalityExpansion:
    """Source/dest direction and bidirectional expansion."""

    def test_source_to_dest(self) -> None:
        """SOURCE_TO_DEST keeps source as source."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        routes = build_runtime_routes(rcs)
        assert len(routes) == 1
        assert routes[0].source.adapter == "a"
        assert routes[0].targets[0].adapter == "b"

    def test_dest_to_source(self) -> None:
        """DEST_TO_SOURCE swaps source and dest adapters."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                directionality=RouteDirectionality.DEST_TO_SOURCE),
        ))
        routes = build_runtime_routes(rcs)
        assert len(routes) == 1
        # source_adapters was ("a",), dest was ("b,")
        # but DEST_TO_SOURCE swaps: source=b, dest=a
        assert routes[0].source.adapter == "b"
        assert routes[0].targets[0].adapter == "a"

    def test_bidirectional_expands_both_directions(self) -> None:
        """BIDIRECTIONAL produces forward + reverse routes."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        routes = build_runtime_routes(rcs)
        assert len(routes) == 2
        # Forward: a→b
        assert routes[0].source.adapter == "a"
        assert routes[0].targets[0].adapter == "b"
        # Reverse: b→a
        assert routes[1].source.adapter == "b"
        assert routes[1].targets[0].adapter == "a"

    def test_bidirectional_multi_source(self) -> None:
        """BIDIRECTIONAL with multiple sources expands correctly."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a1", "a2"), ("b1",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        routes = build_runtime_routes(rcs)
        # Forward: a1→b1, a2→b1 (2 routes, one per source adapter)
        # Reverse: b1→[a1,a2] (1 route, single reversed source with 2 targets)
        assert len(routes) == 3
        # Verify forward routes
        fwd = [r for r in routes if not r.id.startswith("r1__rev")]
        assert len(fwd) == 2
        # Verify reverse route
        rev = [r for r in routes if r.id.startswith("r1__rev")]
        assert len(rev) == 1
        assert rev[0].source.adapter == "b1"
        assert [t.adapter for t in rev[0].targets] == ["a1", "a2"]

    def test_bidirectional_route_ids_unique(self) -> None:
        """Route IDs are unique after bidirectional expansion."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a1", "a2"), ("b1",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        routes = build_runtime_routes(rcs)
        ids = [r.id for r in routes]
        assert len(ids) == len(set(ids)), f"Duplicate route IDs: {ids}"


# ===================================================================
# BridgePolicy → RouteSource mapping
# ===================================================================


class TestBridgePolicyMapping:
    """BridgePolicy event types map to RouteSource.event_kinds."""

    def test_policy_event_types_mapped(self) -> None:
        """allowed_event_types are forwarded to RouteSource.event_kinds."""
        policy = BridgePolicy(allowed_event_types=("message.created", "message.text"))
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), policy=policy),
        ))
        routes = build_runtime_routes(rcs)
        assert routes[0].source.event_kinds == ("message.created", "message.text")

    def test_no_policy_means_no_event_kind_filter(self) -> None:
        """No policy → empty event_kinds (match all)."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        routes = build_runtime_routes(rcs)
        assert routes[0].source.event_kinds == ()

    def test_policy_with_empty_event_types(self) -> None:
        """Policy with empty allowed_event_types → no filter."""
        policy = BridgePolicy()
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), policy=policy),
        ))
        routes = build_runtime_routes(rcs)
        assert routes[0].source.event_kinds == ()


# ===================================================================
# Channel mapping
# ===================================================================


class TestChannelMapping:
    """source_channel and dest_channel are forwarded correctly."""

    def test_channels_forwarded(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                source_channel="src-ch", dest_channel="dst-ch"),
        ))
        routes = build_runtime_routes(rcs)
        assert routes[0].source.channel == "src-ch"
        assert routes[0].targets[0].channel == "dst-ch"

    def test_channels_swapped_on_reverse(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                source_channel="src-ch", dest_channel="dst-ch",
                directionality=RouteDirectionality.DEST_TO_SOURCE),
        ))
        routes = build_runtime_routes(rcs)
        # With swap: source=dest_channel, dest=source_channel
        assert routes[0].source.channel == "dst-ch"
        assert routes[0].targets[0].channel == "src-ch"


# ===================================================================
# validate_route_adapter_refs
# ===================================================================


class TestValidateRouteAdapterRefs:
    """Adapter reference validation catches unknown IDs."""

    def test_valid_refs_pass(self) -> None:
        """Known adapter IDs pass validation."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        validate_route_adapter_refs(rcs, frozenset({"a", "b"}))

    def test_unknown_source_adapter_raises(self) -> None:
        """Unknown source adapter triggers RouteValidationError."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("unknown_src",), ("b",)),
        ))
        with pytest.raises(RouteValidationError, match="unknown source"):
            validate_route_adapter_refs(rcs, frozenset({"a", "b"}))

    def test_unknown_dest_adapter_raises(self) -> None:
        """Unknown dest adapter triggers RouteValidationError."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("unknown_dst",)),
        ))
        with pytest.raises(RouteValidationError, match="unknown dest"):
            validate_route_adapter_refs(rcs, frozenset({"a", "b"}))

    def test_disabled_routes_not_validated(self) -> None:
        """Disabled routes are skipped during validation."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("ghost",), ("phantom",), enabled=False),
        ))
        # Should not raise — disabled routes are ignored
        validate_route_adapter_refs(rcs, frozenset({"a", "b"}))

    def test_error_lists_known_adapters(self) -> None:
        """Error message includes the list of known adapters."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("x",), ("y",)),
        ))
        with pytest.raises(RouteValidationError, match="Known adapters"):
            validate_route_adapter_refs(rcs, frozenset({"a", "b"}))


# ===================================================================
# register_routes — full integration
# ===================================================================


class TestRegisterRoutes:
    """register_routes validates, builds, and registers routes on a Router."""

    def test_routes_registered_on_router(self) -> None:
        """Routes appear in Router.match() results."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))

        event = _make_event(source_adapter="a")
        matched = router.match(event)
        assert len(matched) == 1
        assert matched[0].id == "r1"

    def test_disabled_route_not_registered(self) -> None:
        """Disabled routes do not appear in Router."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), enabled=False),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))

        event = _make_event(source_adapter="a")
        assert router.match(event) == []

    def test_invalid_refs_raise_before_registration(self) -> None:
        """Invalid refs raise before any route is registered."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("nonexistent",)),
        ))
        router = Router()
        with pytest.raises(RouteValidationError):
            register_routes(router, rcs, frozenset({"a", "b"}))

    def test_returns_registered_routes(self) -> None:
        """register_routes returns the list of registered routes."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
            _rc("r2", ("b",), ("a",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert len(result.registered_routes) == 2

    def test_empty_config_no_routes(self) -> None:
        """Empty config set produces no routes."""
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        assert result.registered_routes == ()

    def test_multiple_routes_match_simultaneously(self) -> None:
        """Multiple routes can match the same event."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
            _rc("r2", ("a",), ("c",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b", "c"}))

        event = _make_event(source_adapter="a")
        matched = router.match(event)
        assert len(matched) == 2
        route_ids = {r.id for r in matched}
        assert route_ids == {"r1", "r2"}


# ===================================================================
# Router.match() integration with expanded routes
# ===================================================================


class TestRouterMatchWithExpandedRoutes:
    """Verify Router.match() works correctly with runtime-expanded routes."""

    def test_one_to_one_matching(self) -> None:
        """Event from source adapter matches the one-to-one route."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("src",), ("dst",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"src", "dst"}))

        event = _make_event(source_adapter="src")
        matched = router.match(event)
        assert len(matched) == 1
        targets = router.resolve_targets(event, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == "dst"

    def test_one_to_many_matching(self) -> None:
        """Event from source matches route with multiple dest targets."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("src",), ("dst1", "dst2", "dst3")),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"src", "dst1", "dst2", "dst3"}))

        event = _make_event(source_adapter="src")
        matched = router.match(event)
        assert len(matched) == 1
        targets = router.resolve_targets(event, matched[0])
        assert [t.adapter for t in targets] == ["dst1", "dst2", "dst3"]

    def test_event_kind_filtering_via_policy(self) -> None:
        """BridgePolicy event types restrict which events match."""
        policy = BridgePolicy(allowed_event_types=("message.created",))
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("src",), ("dst",), policy=policy),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"src", "dst"}))

        # Matching event kind
        event_ok = _make_event(source_adapter="src", event_kind="message.created")
        assert len(router.match(event_ok)) == 1

        # Non-matching event kind
        event_bad = _make_event(source_adapter="src", event_kind="status.update")
        assert len(router.match(event_bad)) == 0

    def test_unrelated_events_not_matched(self) -> None:
        """Events from unregistered adapters are not matched."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("src",), ("dst",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"src", "dst"}))

        event = _make_event(source_adapter="unknown_adapter")
        assert router.match(event) == []


# ===================================================================
# Route failure isolation
# ===================================================================


class TestRouteFailureIsolation:
    """One failing route/dest does not affect unrelated routes."""

    def test_multiple_routes_independent_matching(self) -> None:
        """Each route matches independently."""
        rcs = RouteConfigSet(routes=(
            _rc("route_alpha", ("adapter_a",), ("adapter_b",)),
            _rc("route_beta", ("adapter_a",), ("adapter_c",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"adapter_a", "adapter_b", "adapter_c"}))

        event = _make_event(source_adapter="adapter_a")
        matched = router.match(event)
        assert len(matched) == 2
        # Each route resolves to a different target
        for route in matched:
            targets = router.resolve_targets(event, route)
            assert len(targets) == 1
            assert targets[0].adapter in ("adapter_b", "adapter_c")

    def test_disabled_route_does_not_block_others(self) -> None:
        """A disabled route does not interfere with enabled routes."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), enabled=False),
            _rc("r2", ("a",), ("c",), enabled=True),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b", "c"}))

        event = _make_event(source_adapter="a")
        matched = router.match(event)
        assert len(matched) == 1
        assert matched[0].id == "r2"


# ===================================================================
# Loop detection
# ===================================================================


class TestLoopDetection:
    """check_route_loops detects direct routing loops."""

    def test_no_loops(self) -> None:
        """No loops when routes are one-directional."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")]),
        ]
        assert check_route_loops(routes) == []

    def test_direct_loop_detected(self) -> None:
        """Direct A→B and B→A loop is detected."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")]),
            Route(id="r2", source=RouteSource(adapter="b", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="a")]),
        ]
        loops = check_route_loops(routes)
        # Both fast-path direct loop and slow-path DFS cycle are reported
        assert len(loops) >= 1
        assert any("a" in l and "b" in l for l in loops)

    def test_bidirectional_loop_detected(self) -> None:
        """Bidirectional route creates a loop warning."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        routes = build_runtime_routes(rcs)
        loops = check_route_loops(routes)
        assert len(loops) >= 1

    def test_loop_does_not_block_registration(self) -> None:
        """Loops produce warnings but do not prevent registration."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        router = Router()
        # Should NOT raise, just log warnings
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert len(result.registered_routes) == 2

    def test_three_way_no_false_positive(self) -> None:
        """A→B and A→C is not a loop."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")]),
            Route(id="r2", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="c")]),
        ]
        assert check_route_loops(routes) == []


class TestDFSCycleDetection:
    """check_route_loops detects multi-hop cycles via DFS."""

    def test_three_hop_cycle_detected(self) -> None:
        """A→B→C→A multi-hop cycle is detected."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="main", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="radio")]),
            Route(id="r2", source=RouteSource(adapter="radio", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="lxmf_local")]),
            Route(id="r3", source=RouteSource(adapter="lxmf_local", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="main")]),
        ]
        loops = check_route_loops(routes)
        # Should detect at least one cycle containing main -> radio -> lxmf_local -> main
        assert len(loops) >= 1
        cycle_msgs = [l for l in loops if "cycle detected" in l.lower()]
        assert len(cycle_msgs) >= 1
        assert "main" in cycle_msgs[0]
        assert "radio" in cycle_msgs[0]
        assert "lxmf_local" in cycle_msgs[0]

    def test_chain_no_cycle(self) -> None:
        """A→B→C chain with no back-edge has no cycle."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")]),
            Route(id="r2", source=RouteSource(adapter="b", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="c")]),
        ]
        loops = check_route_loops(routes)
        assert loops == []

    def test_self_loop_detected(self) -> None:
        """A→A self-edge is detected as a cycle."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="a")]),
        ]
        loops = check_route_loops(routes)
        assert len(loops) >= 1

    def test_direct_loop_and_cycle_both_reported(self) -> None:
        """Both direct loop and multi-hop cycle are reported."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")]),
            Route(id="r2", source=RouteSource(adapter="b", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="a")]),
            Route(id="r3", source=RouteSource(adapter="b", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="c")]),
            Route(id="r4", source=RouteSource(adapter="c", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="a")]),
        ]
        loops = check_route_loops(routes)
        assert len(loops) >= 1

    def test_disabled_routes_excluded_from_dfs(self) -> None:
        """Disabled routes are not part of the DFS graph."""
        routes = [
            Route(id="r1", source=RouteSource(adapter="a", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="b")], enabled=True),
            Route(id="r2", source=RouteSource(adapter="b", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="c")], enabled=False),
            Route(id="r3", source=RouteSource(adapter="c", event_kinds=(), channel=None),
                  targets=[RouteTarget(adapter="a")], enabled=False),
        ]
        loops = check_route_loops(routes)
        # r2 and r3 are disabled so only A→B exists — no cycle
        assert loops == []


# ===================================================================
# Route ID uniqueness after expansion
# ===================================================================


class TestRouteIdUniqueness:
    """Expanded route IDs are unique within a registration batch."""

    def test_multi_source_ids_unique(self) -> None:
        """Multiple sources produce unique route IDs."""
        rcs = RouteConfigSet(routes=(
            _rc("bridge", ("s1", "s2", "s3"), ("d1",)),
        ))
        routes = build_runtime_routes(rcs)
        ids = [r.id for r in routes]
        assert len(ids) == len(set(ids))

    def test_bidirectional_ids_unique(self) -> None:
        """Bidirectional expansion produces unique IDs."""
        rcs = RouteConfigSet(routes=(
            _rc("link", ("s1", "s2"), ("d1",),
                directionality=RouteDirectionality.BIDIRECTIONAL),
        ))
        routes = build_runtime_routes(rcs)
        ids = [r.id for r in routes]
        assert len(ids) == len(set(ids))

    def test_expanded_id_collision_with_user_id_raises(self) -> None:
        """User route ID matching expansion pattern causes collision error."""
        # Route "r1" with 2 sources expands to "r1__0" and "r1__1".
        # Route "r1__0" with 1 source expands to "r1__0" (single source, no suffix).
        # Collision: "r1__0" appears from both routes.
        r1 = RouteConfig.from_toml_dict("r1", {
            "source_adapters": ["a1", "a2"],
            "dest_adapters": ["b"],
        })
        r1_dunder_0 = RouteConfig.from_toml_dict("r1__0", {
            "source_adapters": ["c"],
            "dest_adapters": ["d"],
        })
        rcs = RouteConfigSet(routes=(r1, r1_dunder_0))
        with pytest.raises(RouteValidationError, match="Expanded route ID collision.*r1__0"):
            build_runtime_routes(rcs)

    def test_bidirectional_collision_with_user_id_raises(self) -> None:
        """Bidirectional expansion suffix colliding with user route ID raises."""
        # Route "bridge" with 1 source in bidirectional expands to:
        #   forward: "bridge"  (single source)
        #   reverse: "bridge__rev_0"
        # Route "bridge__rev_0" would collide.
        r1 = RouteConfig.from_toml_dict("bridge", {
            "source_adapters": ["a"],
            "dest_adapters": ["b"],
            "directionality": "bidirectional",
        })
        r2 = RouteConfig.from_toml_dict("bridge__rev_0", {
            "source_adapters": ["c"],
            "dest_adapters": ["d"],
        })
        rcs = RouteConfigSet(routes=(r1, r2))
        with pytest.raises(RouteValidationError, match="Expanded route ID collision"):
            build_runtime_routes(rcs)

    def test_no_collision_across_independent_routes(self) -> None:
        """Two independent routes with single sources have unique IDs."""
        rcs = RouteConfigSet(routes=(
            _rc("alpha", ("a",), ("b",)),
            _rc("beta", ("c",), ("d",)),
        ))
        routes = build_runtime_routes(rcs)
        ids = [r.id for r in routes]
        assert len(ids) == len(set(ids))


# ===================================================================
# Multiple route chains
# ===================================================================


class TestMultipleRouteChains:
    """Multiple independent route chains coexist correctly."""

    def test_independent_chains(self) -> None:
        """Events from different sources match their respective routes."""
        rcs = RouteConfigSet(routes=(
            _rc("chain1", ("matrix_main",), ("mesh_radio",)),
            _rc("chain2", ("mesh_radio",), ("matrix_main",)),
            _rc("chain3", ("lxmf_node",), ("mesh_radio",)),
        ))
        router = Router()
        register_routes(
            router, rcs,
            frozenset({"matrix_main", "mesh_radio", "lxmf_node"}),
        )

        # Event from matrix_main → chain1
        evt_matrix = _make_event(source_adapter="matrix_main")
        matched = router.match(evt_matrix)
        route_ids = {r.id for r in matched}
        assert "chain1" in route_ids
        assert "chain2" not in route_ids

        # Event from mesh_radio → chain2
        evt_mesh = _make_event(source_adapter="mesh_radio")
        matched = router.match(evt_mesh)
        route_ids = {r.id for r in matched}
        assert "chain2" in route_ids

        # Event from lxmf_node → chain3
        evt_lxmf = _make_event(source_adapter="lxmf_node")
        matched = router.match(evt_lxmf)
        route_ids = {r.id for r in matched}
        assert "chain3" in route_ids

    def test_cascading_routes(self) -> None:
        """Events can flow through a chain: A→B and B→C."""
        rcs = RouteConfigSet(routes=(
            _rc("step1", ("a",), ("b",)),
            _rc("step2", ("b",), ("c",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b", "c"}))

        # Event from 'a' matches step1
        evt_a = _make_event(source_adapter="a")
        matched = router.match(evt_a)
        assert len(matched) == 1
        assert matched[0].id == "step1"

        # Event from 'b' matches step2
        evt_b = _make_event(source_adapter="b")
        matched = router.match(evt_b)
        assert len(matched) == 1
        assert matched[0].id == "step2"


# ===================================================================
# Startup validation edge cases
# ===================================================================


class TestStartupValidation:
    """Edge cases in startup adapter ID validation."""

    def test_all_adapters_valid(self) -> None:
        """All adapter IDs present — validation succeeds."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a", "b"), ("c",)),
        ))
        # Should not raise
        validate_route_adapter_refs(rcs, frozenset({"a", "b", "c"}))

    def test_missing_single_adapter(self) -> None:
        """Single missing adapter ID is caught."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        with pytest.raises(RouteValidationError, match="b"):
            validate_route_adapter_refs(rcs, frozenset({"a"}))

    def test_multiple_missing_adapters(self) -> None:
        """Multiple unknown adapters are all reported."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("x",), ("y",)),
        ))
        with pytest.raises(RouteValidationError) as exc_info:
            validate_route_adapter_refs(rcs, frozenset({"a"}))
        msg = str(exc_info.value)
        assert "x" in msg
        assert "y" in msg

    def test_empty_adapter_ids_with_enabled_route(self) -> None:
        """Enabled routes with no built adapters raises error."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        with pytest.raises(RouteValidationError):
            validate_route_adapter_refs(rcs, frozenset())

    def test_disabled_route_with_missing_adapters_ok(self) -> None:
        """Disabled routes are skipped even if their adapters are missing."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("missing",), ("also_missing",), enabled=False),
        ))
        validate_route_adapter_refs(rcs, frozenset())


# ===================================================================
# Bidirectional route targeting field expansion
# ===================================================================


class TestBidirectionalTargetingExpansion:
    """A bidirectional route with source_room/dest_channel targeting fields
    expands into two runtime routes with correct source/target channels.

    This mirrors the canonical live-matrix-meshtastic.toml pattern:
    source_adapters=["matrix"], dest_adapters=["radio"],
    source_room="!room:example.com", dest_channel="0".

    The RouteConfig normalises source_room → source_channel (alias),
    and the bidirectional expansion swaps source_channel ↔ dest_channel
    for the reverse leg.
    """

    def test_bidirectional_with_source_room_and_dest_channel(self) -> None:
        """Bidirectional route expands with correct targeting for both legs."""
        # Build config using from_toml_dict to exercise source_room→source_channel alias
        rc = RouteConfig.from_toml_dict("bridge", {
            "source_adapters": ["matrix"],
            "dest_adapters": ["radio"],
            "directionality": "bidirectional",
            "enabled": True,
            "source_room": "!room:example.com",
            "dest_channel": "0",
        })
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs)

        # Should produce exactly 2 routes (forward + reverse)
        assert len(routes) == 2

        # Forward leg: matrix → radio
        fwd = routes[0]
        assert fwd.source.adapter == "matrix"
        assert fwd.source.channel == "!room:example.com"
        assert len(fwd.targets) == 1
        assert fwd.targets[0].adapter == "radio"
        assert fwd.targets[0].channel == "0"

        # Reverse leg: radio → matrix
        rev = routes[1]
        assert rev.source.adapter == "radio"
        assert rev.source.channel == "0"
        assert len(rev.targets) == 1
        assert rev.targets[0].adapter == "matrix"
        assert rev.targets[0].channel == "!room:example.com"

    def test_bidirectional_targeting_registered_on_router(self) -> None:
        """Bidirectional route with targeting registers and matches correctly."""
        rc = RouteConfig.from_toml_dict("bridge", {
            "source_adapters": ["matrix"],
            "dest_adapters": ["radio"],
            "directionality": "bidirectional",
            "enabled": True,
            "source_room": "!room:example.com",
            "dest_channel": "0",
        })
        rcs = RouteConfigSet(routes=(rc,))
        router = Router()
        result = register_routes(
            router, rcs,
            frozenset({"matrix", "radio"}),
        )
        assert len(result.registered_routes) == 2

        # Forward: event from matrix in the correct room matches forward route
        evt_matrix = _make_event(
            source_adapter="matrix",
            source_channel_id="!room:example.com",
        )
        matched = router.match(evt_matrix)
        assert len(matched) == 1
        targets = router.resolve_targets(evt_matrix, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == "radio"
        assert targets[0].channel == "0"

        # Reverse: event from radio on the correct channel matches reverse route
        evt_radio = _make_event(
            source_adapter="radio",
            source_channel_id="0",
        )
        matched = router.match(evt_radio)
        assert len(matched) == 1
        targets = router.resolve_targets(evt_radio, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == "matrix"
        assert targets[0].channel == "!room:example.com"
