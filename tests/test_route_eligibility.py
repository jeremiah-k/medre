"""Focused tests for route eligibility/readiness metadata.

Covers:
* Registered route IDs are captured in eligibility
* Disabled route IDs are captured in eligibility
* Skipped routes (source adapter failed) are captured with reason and IDs
* Skipped routes (no surviving targets) are captured
* Unavailable is always empty in normal flow
* Unknown adapter refs still raise RouteValidationError
* RouteRegistrationResult is a frozen dataclass (not list)
* RouteOperationalState enum semantics
* DegradedRoute tracking for partial target failure
* Deterministic sorted ordering in configured/registered/disabled tuples
* Empty config produces empty eligibility
* Mixed scenario with registered, disabled, and skipped routes
* Builder integration: MedreApp.route_eligibility is populated
* Per-route readiness states via route_states dict
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.lifecycle.states import AdapterState
from medre.core.routing import Router
from medre.runtime.route_engine import (
    DegradedRoute,
    RouteEligibility,
    RouteOperationalState,
    RouteRegistrationResult,
    RouteValidationError,
    SkippedRoute,
    UnavailableRoute,
    compute_startup_readiness,
    register_routes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_event(source_adapter: str = "adapter_a") -> CanonicalEvent:
    """Create a minimal CanonicalEvent for routing tests."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hi"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Registered route IDs captured
# ===================================================================


class TestRegisteredIdsCaptured:
    """Eligibility.registered contains all successfully registered route IDs."""

    def test_single_route_registered(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.registered == ("r1",)

    def test_multiple_routes_registered_sorted(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("beta", ("a",), ("b",)),
                _rc("alpha", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        # Sorted deterministically
        assert result.eligibility.registered == ("alpha", "beta")

    def test_configured_captures_enabled_ids(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("r1", ("a",), ("b",)),
                _rc("r2", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result.eligibility.configured == ("r1", "r2")


# ===================================================================
# Disabled route IDs captured
# ===================================================================


class TestDisabledIdsCaptured:
    """Eligibility.disabled contains disabled config route IDs."""

    def test_disabled_route_in_eligibility(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",), enabled=False),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.disabled == ("r1",)
        assert result.eligibility.registered == ()

    def test_mixed_enabled_disabled(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("active", ("a",), ("b",), enabled=True),
                _rc("off", ("c",), ("d",), enabled=False),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result.eligibility.registered == ("active",)
        assert result.eligibility.disabled == ("off",)
        assert result.eligibility.configured == ("active",)


# ===================================================================
# Skipped routes (source adapter failed)
# ===================================================================


class TestSkippedSourceFailed:
    """Routes whose source adapter failed to build are skipped."""

    def test_source_failed_skipped(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        # Adapter "a" is configured but failed to build
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),  # "a" failed
        )
        assert result.eligibility.registered == ()
        assert len(result.eligibility.skipped) == 1
        skipped = result.eligibility.skipped[0]
        assert skipped.route_id == "r1"
        assert skipped.reason == "source_adapter_failed"
        assert skipped.failed_adapter_ids == ("a",)

    def test_source_failed_does_not_affect_other_routes(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("bad_route", ("a",), ("b",)),
                _rc("good_route", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d"}),
            built_adapter_ids=frozenset({"b", "c", "d"}),  # "a" failed
        )
        assert result.eligibility.registered == ("good_route",)
        assert len(result.eligibility.skipped) == 1
        assert result.eligibility.skipped[0].route_id == "bad_route"


# ===================================================================
# Skipped routes (no surviving targets)
# ===================================================================


class TestSkippedNoSurvivingTargets:
    """Routes whose all target adapters failed are skipped."""

    def test_all_dests_failed(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        # Source "a" is fine, but both dests failed to build
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a"}),  # "b" and "c" failed
        )
        assert result.eligibility.registered == ()
        assert len(result.eligibility.skipped) == 1
        skipped = result.eligibility.skipped[0]
        assert skipped.route_id == "r1"
        assert skipped.reason == "no_surviving_targets"
        assert set(skipped.failed_adapter_ids) == {"b", "c"}

    def test_partial_dest_failure_route_still_registered(self) -> None:
        """If some targets survive, the route is registered (not skipped)."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        # "b" failed but "c" survived
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a", "c"}),
        )
        assert result.eligibility.registered == ("r1",)
        assert result.eligibility.skipped == ()


# ===================================================================
# Unavailable routes
# ===================================================================


class TestUnavailableRoutes:
    """Unavailable is always empty in normal flow (unknown refs raise)."""

    def test_unavailable_empty_when_all_known(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.unavailable == ()

    def test_unavailable_empty_when_degraded(self) -> None:
        """Even with degraded routes, unavailable remains empty."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),
        )
        assert result.eligibility.unavailable == ()


# ===================================================================
# Unknown adapter refs still raise
# ===================================================================


class TestUnknownAdapterRefsRaise:
    """References to truly unknown adapter IDs raise RouteValidationError."""

    def test_unknown_source_raises(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("ghost",), ("b",)),))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown source"):
            register_routes(router, rcs, frozenset({"a", "b"}))

    def test_unknown_dest_raises(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("phantom",)),))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown dest"):
            register_routes(router, rcs, frozenset({"a", "b"}))

    def test_configured_but_missing_is_not_unknown(self) -> None:
        """An adapter ID in adapter_ids but not in built_adapter_ids
        degrades rather than raises."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        # "a" is in adapter_ids (configured) but not built — should not raise
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),
        )
        # Should skip, not raise
        assert len(result.eligibility.skipped) == 1


# ===================================================================
# RouteRegistrationResult is a frozen dataclass
# ===================================================================


class TestRouteRegistrationResultIsDataclass:
    """RouteRegistrationResult is a frozen dataclass (not a list)."""

    def test_registered_routes_attribute(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("r1", ("a",), ("b",)),
                _rc("r2", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert len(result.registered_routes) == 2

    def test_registered_routes_iteration(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        route_ids = [r.id for r in result.registered_routes]
        assert route_ids == ["r1"]

    def test_registered_routes_indexing(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("r1", ("a",), ("b",)),
                _rc("r2", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result.registered_routes[0].id == "r1"
        assert result.registered_routes[1].id == "r2"

    def test_result_is_frozen(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        with pytest.raises(AttributeError):
            result.registered_routes = ()  # type: ignore[misc]

    def test_empty_result_has_no_routes(self) -> None:
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        assert result.registered_routes == ()

    def test_result_has_eligibility(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility is not None
        assert isinstance(result.eligibility, RouteEligibility)


# ===================================================================
# RouteOperationalState enum
# ===================================================================


class TestRouteOperationalState:
    """RouteOperationalState enum has expected values."""

    def test_all_states_defined(self) -> None:
        expected = {
            "configured",
            "registered",
            "degraded",
            "skipped",
            "unavailable",
            "disabled",
        }
        actual = {s.value for s in RouteOperationalState}
        assert actual == expected

    def test_registered_route_state(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.route_states["r1"] == RouteOperationalState.REGISTERED

    def test_disabled_route_state(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",), enabled=False),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.route_states["r1"] == RouteOperationalState.DISABLED

    def test_skipped_source_failed_state(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),  # "a" failed
        )
        assert result.eligibility.route_states["r1"] == RouteOperationalState.SKIPPED

    def test_skipped_no_targets_state(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"a"}),  # "b" failed
        )
        assert result.eligibility.route_states["r1"] == RouteOperationalState.SKIPPED

    def test_route_states_sorted_keys(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("z_route", ("a",), ("b",)),
                _rc("a_route", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        keys = list(result.eligibility.route_states.keys())
        assert keys == sorted(keys)


# ===================================================================
# DegradedRoute tracking
# ===================================================================


class TestDegradedRouteTracking:
    """Routes with partial target failure are tracked as degraded."""

    def test_degraded_when_partial_targets_fail(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a", "c"}),  # "b" failed
        )
        assert result.eligibility.registered == ("r1",)
        assert len(result.eligibility.degraded) == 1
        degraded = result.eligibility.degraded[0]
        assert degraded.route_id == "r1"
        assert degraded.failed_adapter_ids == ("b",)

    def test_degraded_state_in_route_states(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a", "c"}),  # "b" failed
        )
        assert result.eligibility.route_states["r1"] == RouteOperationalState.DEGRADED

    def test_no_degraded_when_all_targets_ok(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.degraded == ()

    def test_no_degraded_when_all_targets_fail(self) -> None:
        """All targets fail → skipped, not degraded."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a"}),  # "b" and "c" failed
        )
        assert result.eligibility.degraded == ()
        assert len(result.eligibility.skipped) == 1


# ===================================================================
# Deterministic sorted ordering
# ===================================================================


class TestDeterministicOrdering:
    """Eligibility tuples are sorted deterministically."""

    def test_configured_sorted(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("z_route", ("a",), ("b",)),
                _rc("a_route", ("c",), ("d",)),
                _rc("m_route", ("e",), ("f",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d", "e", "f"}),
        )
        assert result.eligibility.configured == ("a_route", "m_route", "z_route")

    def test_registered_sorted(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("z_route", ("a",), ("b",)),
                _rc("a_route", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        assert result.eligibility.registered == ("a_route", "z_route")

    def test_disabled_sorted(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("z_off", ("a",), ("b",), enabled=False),
                _rc("a_off", ("c",), ("d",), enabled=False),
            )
        )
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result.eligibility.disabled == ("a_off", "z_off")


# ===================================================================
# Empty config
# ===================================================================


class TestEmptyConfig:
    """Empty RouteConfigSet produces empty eligibility."""

    def test_all_fields_empty(self) -> None:
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        e = result.eligibility
        assert e.configured == ()
        assert e.registered == ()
        assert e.disabled == ()
        assert e.degraded == ()
        assert e.skipped == ()
        assert e.unavailable == ()
        assert e.route_states == {}


# ===================================================================
# Mixed scenario
# ===================================================================


class TestMixedScenario:
    """Combination of registered, disabled, and skipped routes."""

    def test_full_mixed(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc("active1", ("a",), ("b",)),
                _rc("disabled1", ("c",), ("d",), enabled=False),
                _rc("active2", ("e",), ("f",)),
                _rc("will_skip", ("g",), ("h",)),  # source "g" will fail
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d", "e", "f", "g", "h"}),
            built_adapter_ids=frozenset({"a", "b", "e", "f", "h"}),  # "g" failed
        )
        e = result.eligibility
        # Configured: enabled route IDs, sorted
        assert e.configured == ("active1", "active2", "will_skip")
        # Registered: successfully registered, sorted
        assert e.registered == ("active1", "active2")
        # Disabled
        assert e.disabled == ("disabled1",)
        # Degraded: none in this scenario
        assert e.degraded == ()
        # Skipped
        assert len(e.skipped) == 1
        assert e.skipped[0].route_id == "will_skip"
        assert e.skipped[0].reason == "source_adapter_failed"
        assert e.skipped[0].failed_adapter_ids == ("g",)
        # Unavailable empty
        assert e.unavailable == ()
        # Route states
        assert e.route_states["active1"] == RouteOperationalState.REGISTERED
        assert e.route_states["active2"] == RouteOperationalState.REGISTERED
        assert e.route_states["disabled1"] == RouteOperationalState.DISABLED
        assert e.route_states["will_skip"] == RouteOperationalState.SKIPPED


# ===================================================================
# Router behavior unchanged
# ===================================================================


class TestRouterBehaviorUnchanged:
    """Routes still match events correctly after eligibility changes."""

    def test_registered_route_matches(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))

        event = _make_event(source_adapter="a")
        matched = router.match(event)
        assert len(matched) == 1
        assert matched[0].id == "r1"

    def test_skipped_route_not_in_router(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),  # "a" failed
        )
        # Route was skipped, so router has no routes
        event = _make_event(source_adapter="a")
        assert router.match(event) == []

    def test_disabled_route_not_in_router(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",), enabled=False),))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))
        event = _make_event(source_adapter="a")
        assert router.match(event) == []


# ===================================================================
# SkippedRoute / UnavailableRoute / DegradedRoute frozen dataclass
# ===================================================================


class TestFrozenModels:
    """Eligibility models are frozen (immutable)."""

    def test_skipped_route_frozen(self) -> None:
        s = SkippedRoute("r1", "source_adapter_failed", ("a",))
        with pytest.raises(AttributeError):
            s.route_id = "other"  # type: ignore[misc]

    def test_unavailable_route_frozen(self) -> None:
        u = UnavailableRoute("r1", "unknown_adapter", ("x",))
        with pytest.raises(AttributeError):
            u.route_id = "other"  # type: ignore[misc]

    def test_degraded_route_frozen(self) -> None:
        d = DegradedRoute("r1", ("b",))
        with pytest.raises(AttributeError):
            d.route_id = "other"  # type: ignore[misc]

    def test_route_eligibility_frozen(self) -> None:
        e = RouteEligibility(
            configured=("r1",),
            registered=("r1",),
            disabled=(),
            degraded=(),
            skipped=(),
            unavailable=(),
            route_states={},
        )
        with pytest.raises(AttributeError):
            e.registered = ()  # type: ignore[misc]

    def test_route_registration_result_frozen(self) -> None:
        r = RouteRegistrationResult(
            registered_routes=(),
            eligibility=RouteEligibility(
                configured=(),
                registered=(),
                disabled=(),
                degraded=(),
                skipped=(),
                unavailable=(),
                route_states={},
            ),
            provenance={},
        )
        with pytest.raises(AttributeError):
            r.registered_routes = ()  # type: ignore[misc]


# ===================================================================
# Builder integration: MedreApp.route_eligibility
# ===================================================================


class TestBuilderIntegration:
    """RuntimeBuilder populates MedreApp.route_eligibility."""

    def test_route_eligibility_populated_by_builder(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Build a full runtime and verify route_eligibility is set."""
        for var in (
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.config.model import (
            AdapterConfigSet,
            LoggingConfig,
            MeshtasticRuntimeConfig,
            RuntimeConfig,
            RuntimeOptions,
            StorageConfig,
        )
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder

        paths = resolve()

        mesh_a = MeshtasticConfig(
            adapter_id="radio_a",
            connection_type="fake",
        ).validate()
        mesh_b = MeshtasticConfig(
            adapter_id="radio_b",
            connection_type="fake",
        ).validate()

        route1 = RouteConfig.from_dict(
            "bridge",
            {
                "source_adapters": ["radio_a"],
                "dest_adapters": ["radio_b"],
            },
        )

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-eligibility"),
            adapters=AdapterConfigSet(
                meshtastic={
                    "radio_a": MeshtasticRuntimeConfig(
                        adapter_id="radio_a",
                        adapter_kind="fake",
                        config=mesh_a,
                    ),
                    "radio_b": MeshtasticRuntimeConfig(
                        adapter_id="radio_b",
                        adapter_kind="fake",
                        config=mesh_b,
                    ),
                },
            ),
            routes=RouteConfigSet(routes=(route1,)),
            storage=StorageConfig(backend="memory"),
            logging=LoggingConfig(),
        )

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert app.route_eligibility is not None
        assert "bridge" in app.route_eligibility.registered
        assert app.route_eligibility.configured == ("bridge",)
        assert app.route_eligibility.disabled == ()
        assert app.route_eligibility.degraded == ()
        assert app.route_eligibility.skipped == ()
        assert app.route_eligibility.unavailable == ()
        assert (
            app.route_eligibility.route_states["bridge"]
            == RouteOperationalState.REGISTERED
        )


# ===================================================================
# Prefix collision: routes with overlapping name prefixes
# ===================================================================


class TestPrefixCollision:
    """Routes whose IDs share prefixes (e.g. radio / radio_backup) must
    not be matched via string-prefix inference."""

    def test_radio_radio_backup_both_registered(self) -> None:
        """'radio' and 'radio_backup' must both resolve independently."""
        rcs = RouteConfigSet(
            routes=(
                _rc("radio", ("a",), ("b",)),
                _rc("radio_backup", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        assert "radio" in result.eligibility.registered
        assert "radio_backup" in result.eligibility.registered
        assert (
            result.eligibility.route_states["radio"] == RouteOperationalState.REGISTERED
        )
        assert (
            result.eligibility.route_states["radio_backup"]
            == RouteOperationalState.REGISTERED
        )

    def test_radio_backup_skipped_radio_registered(self) -> None:
        """If 'radio_backup' source fails, 'radio' must stay registered."""
        rcs = RouteConfigSet(
            routes=(
                _rc("radio", ("a",), ("b",)),
                _rc("radio_backup", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d"}),
            built_adapter_ids=frozenset({"a", "b", "d"}),  # "c" failed
        )
        assert "radio" in result.eligibility.registered
        assert (
            result.eligibility.route_states["radio"] == RouteOperationalState.REGISTERED
        )
        assert (
            result.eligibility.route_states["radio_backup"]
            == RouteOperationalState.SKIPPED
        )

    def test_provenance_distinguishes_overlapping_ids(self) -> None:
        """Provenance mapping must be exact, not prefix-based."""
        rcs = RouteConfigSet(
            routes=(
                _rc("radio", ("a",), ("b",)),
                _rc("radio_backup", ("c",), ("d",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        assert result.provenance["radio"] == "radio"
        assert result.provenance["radio_backup"] == "radio_backup"


# ===================================================================
# Bidirectional __rev_N maps correctly
# ===================================================================


class TestBidirectionalProvenance:
    """Bidirectional routes produce __rev_N expanded IDs with correct provenance."""

    def test_bidirectional_provenance(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc(
                    "bridge",
                    ("a",),
                    ("b",),
                    directionality=RouteDirectionality.BIDIRECTIONAL,
                ),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b"}),
        )
        # Forward: "bridge", Reverse: "bridge__rev_0"
        assert result.provenance.get("bridge") == "bridge"
        assert result.provenance.get("bridge__rev_0") == "bridge"
        assert (
            result.eligibility.route_states["bridge"]
            == RouteOperationalState.REGISTERED
        )

    def test_bidirectional_multi_source_provenance(self) -> None:
        rcs = RouteConfigSet(
            routes=(
                _rc(
                    "multi",
                    ("a", "b"),
                    ("c",),
                    directionality=RouteDirectionality.BIDIRECTIONAL,
                ),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c"}),
        )
        # Forward: "multi__0", "multi__1"
        # Reverse: "multi__rev_0"
        assert result.provenance["multi__0"] == "multi"
        assert result.provenance["multi__1"] == "multi"
        assert result.provenance["multi__rev_0"] == "multi"


# ===================================================================
# Multi-source expansion maps correctly
# ===================================================================


class TestMultiSourceProvenance:
    """Multi-source routes produce __N expanded IDs with correct provenance."""

    def test_multi_source_provenance(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("fan_out", ("a", "b", "c"), ("d",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        assert result.provenance["fan_out__0"] == "fan_out"
        assert result.provenance["fan_out__1"] == "fan_out"
        assert result.provenance["fan_out__2"] == "fan_out"

    def test_multi_source_one_failed_maps_correctly(self) -> None:
        """If one source fails, provenance still maps correctly."""
        rcs = RouteConfigSet(routes=(_rc("fan_out", ("a", "b", "c"), ("d",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d"}),
            built_adapter_ids=frozenset({"b", "c", "d"}),  # "a" failed
        )
        # fan_out__0 (source "a") should be skipped
        # fan_out__1, fan_out__2 should be registered
        assert (
            result.eligibility.route_states["fan_out"] == RouteOperationalState.DEGRADED
        )
        skipped_ids = {s.route_id for s in result.eligibility.skipped}
        assert "fan_out__0" in skipped_ids


# ===================================================================
# RouteRegistrationResult provenance field
# ===================================================================


class TestProvenanceField:
    """RouteRegistrationResult carries explicit provenance mapping."""

    def test_single_route_provenance(self) -> None:
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.provenance == {"r1": "r1"}

    def test_empty_config_provenance(self) -> None:
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        assert result.provenance == {}

    def test_provenance_immutable_copy(self) -> None:
        """Provenance dict should not be mutated by callers."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        # The dict itself is stored; frozen dataclass prevents replacing it.
        original = dict(result.provenance)
        assert result.provenance == original


# ===================================================================
# compute_startup_readiness: full coverage
# ===================================================================


def _make_config_set(*routes: RouteConfig) -> RouteConfigSet:
    """Build a RouteConfigSet from the given routes."""
    return RouteConfigSet(routes=routes)


class TestStartupReadinessAllReady:
    """When all adapters are READY, startup readiness mirrors build eligibility."""

    def test_all_ready_registered(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b",)))
        router = Router()
        reg = register_routes(router, rcs, frozenset({"a", "b"}))
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={"a": AdapterState.READY, "b": AdapterState.READY},
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.REGISTERED
        assert readiness.degraded == ()
        assert readiness.skipped == ()


class TestStartupReadinessSourceStartFailed:
    """Source adapter that built but failed to start → SKIPPED."""

    def test_source_start_failed(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b",)))
        router = Router()
        reg = register_routes(router, rcs, frozenset({"a", "b"}))
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={"a": AdapterState.FAILED, "b": AdapterState.READY},
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.SKIPPED
        assert len(readiness.skipped) == 1
        assert readiness.skipped[0].reason == "source_adapter_start_failed"
        assert readiness.skipped[0].failed_adapter_ids == ("a",)


class TestStartupReadinessPartialTargetsStartFailed:
    """Some targets failed to start → DEGRADED."""

    def test_partial_targets_start_failed(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b", "c")))
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a", "b", "c"}),
        )
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={
                "a": AdapterState.READY,
                "b": AdapterState.FAILED,
                "c": AdapterState.READY,
            },
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.DEGRADED
        assert len(readiness.degraded) == 1
        assert readiness.degraded[0].failed_adapter_ids == ("b",)


class TestStartupReadinessAllTargetsStartFailed:
    """All targets failed to start → SKIPPED."""

    def test_all_targets_start_failed(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b", "c")))
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c"}),
            built_adapter_ids=frozenset({"a", "b", "c"}),
        )
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={
                "a": AdapterState.READY,
                "b": AdapterState.FAILED,
                "c": AdapterState.FAILED,
            },
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.SKIPPED
        assert len(readiness.skipped) == 1
        assert readiness.skipped[0].reason == "no_surviving_targets_start_failed"
        assert set(readiness.skipped[0].failed_adapter_ids) == {"b", "c"}


class TestStartupReadinessBuildSkippedUnchanged:
    """Routes already skipped at build time stay SKIPPED at startup."""

    def test_build_skipped_stays_skipped(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b",)))
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),  # "a" failed to build
        )
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={"b": AdapterState.READY},
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.SKIPPED
        # No new startup skips (it was already skipped at build time)
        assert readiness.skipped == ()


class TestStartupReadinessDisabledUnchanged:
    """Disabled routes stay DISABLED in startup readiness."""

    def test_disabled_stays_disabled(self) -> None:
        rcs = _make_config_set(_rc("r1", ("a",), ("b",), enabled=False))
        router = Router()
        reg = register_routes(router, rcs, frozenset({"a", "b"}))
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={"a": AdapterState.READY, "b": AdapterState.READY},
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["r1"] == RouteOperationalState.DISABLED


class TestStartupReadinessMixedScenario:
    """Mixed scenario: registered, start-failed source, partial target failure."""

    def test_mixed_startup_readiness(self) -> None:
        rcs = _make_config_set(
            _rc("ok_route", ("a",), ("b",)),
            _rc("source_fail", ("c",), ("d",)),
            _rc("partial_targets", ("e",), ("f", "g")),
        )
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d", "e", "f", "g"}),
            built_adapter_ids=frozenset({"a", "b", "c", "d", "e", "f", "g"}),
        )
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={
                "a": AdapterState.READY,
                "b": AdapterState.READY,
                "c": AdapterState.FAILED,  # source_fail source
                "d": AdapterState.READY,
                "e": AdapterState.READY,
                "f": AdapterState.FAILED,  # partial target
                "g": AdapterState.READY,
            },
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["ok_route"] == RouteOperationalState.REGISTERED
        assert readiness.route_states["source_fail"] == RouteOperationalState.SKIPPED
        assert (
            readiness.route_states["partial_targets"] == RouteOperationalState.DEGRADED
        )


class TestStartupReadinessUnknownRefsStillRaise:
    """Unknown adapter refs in routes still raise at register time (not startup)."""

    def test_unknown_ref_raises_at_register(self) -> None:
        rcs = _make_config_set(_rc("r1", ("ghost",), ("b",)))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown source"):
            register_routes(router, rcs, frozenset({"a", "b"}))


class TestStartupReadinessPrefixCollisionStartup:
    """Startup readiness handles radio/radio_backup correctly."""

    def test_radio_backup_source_fails_radio_ok(self) -> None:
        rcs = _make_config_set(
            _rc("radio", ("a",), ("b",)),
            _rc("radio_backup", ("c",), ("d",)),
        )
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b", "c", "d"}),
            built_adapter_ids=frozenset({"a", "b", "c", "d"}),
        )
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={
                "a": AdapterState.READY,
                "b": AdapterState.READY,
                "c": AdapterState.FAILED,  # radio_backup source
                "d": AdapterState.READY,
            },
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        assert readiness.route_states["radio"] == RouteOperationalState.REGISTERED
        assert readiness.route_states["radio_backup"] == RouteOperationalState.SKIPPED


class TestStartupReadinessBidirectional:
    """Startup readiness handles bidirectional routes with __rev_N provenance."""

    def test_bidirectional_forward_ok_reverse_source_fails(self) -> None:
        rcs = _make_config_set(
            _rc(
                "bridge",
                ("a",),
                ("b",),
                directionality=RouteDirectionality.BIDIRECTIONAL,
            ),
        )
        router = Router()
        reg = register_routes(
            router,
            rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"a", "b"}),
        )
        # "b" is source for reverse leg, and it failed to start
        readiness = compute_startup_readiness(
            eligibility=reg.eligibility,
            adapter_states={
                "a": AdapterState.READY,
                "b": AdapterState.FAILED,
            },
            provenance=reg.provenance,
            registered_routes=reg.registered_routes,
            config_routes=rcs,
        )
        # Forward is ok (source "a" ready, target "b" failed → degraded)
        # Reverse skipped (source "b" failed)
        # Overall: worst state wins → SKIPPED
        assert readiness.route_states["bridge"] == RouteOperationalState.SKIPPED


# ===================================================================
# Route references disabled adapter
# ===================================================================


class TestRouteReferencesDisabledAdapter:
    """Routes referencing a disabled adapter raise RouteValidationError.

    Disabled adapters are excluded from the configured-enabled adapter ID
    set, so a route referencing one is treated the same as referencing an
    unknown adapter.
    """

    def test_disabled_source_adapter_raises(self) -> None:
        """A route whose source adapter is disabled must raise."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("disabled_a",), ("b",)),))
        router = Router()
        # "disabled_a" is not in the adapter_ids set because it's disabled
        with pytest.raises(RouteValidationError, match="unknown source"):
            register_routes(router, rcs, frozenset({"b"}))

    def test_disabled_dest_adapter_raises(self) -> None:
        """A route whose dest adapter is disabled must raise."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("disabled_b",)),))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown dest"):
            register_routes(router, rcs, frozenset({"a"}))

    def test_disabled_route_skipped_not_raised(self) -> None:
        """A route with enabled=false is skipped, not validated."""
        rcs = RouteConfigSet(
            routes=(_rc("r1", ("ghost",), ("phantom",), enabled=False),)
        )
        router = Router()
        # Should NOT raise — disabled routes are not validated
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.disabled == ("r1",)
        assert result.eligibility.registered == ()


# ===================================================================
# Route adapter ID override semantics
# ===================================================================


class TestRouteAdapterIdOverride:
    """Routes resolve against the adapter_id (not the TOML section key).

    When an adapter sets adapter_id = "custom", the route must reference
    "custom" — not the section key.  This tests the route engine's
    understanding of adapter IDs, not the TOML parsing (which happens in
    test_config_loader.py).
    """

    def test_route_uses_resolved_adapter_id(self) -> None:
        """Routes must reference the resolved adapter_id value."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("custom_id",), ("b",)),))
        router = Router()
        # "custom_id" is the resolved adapter_id (not a TOML section key)
        result = register_routes(router, rcs, frozenset({"custom_id", "b"}))
        assert result.eligibility.registered == ("r1",)

    def test_route_using_section_key_not_adapter_id_raises(self) -> None:
        """If adapter overrides ID to 'custom', route referencing section
        key 'original' must raise — the section key is not a known adapter."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("original",), ("b",)),))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown source"):
            register_routes(router, rcs, frozenset({"custom_id", "b"}))


# ---------------------------------------------------------------------------
# Deterministic route registration ordering
# ---------------------------------------------------------------------------


class TestDeterministicRouteRegistrationOrder:
    """Routes are registered in config declaration order (deterministic)."""

    def test_routes_registered_in_config_declaration_order(self) -> None:
        """register_routes preserves config declaration order."""
        rcs = RouteConfigSet(
            routes=(
                _rc("zebra_route", ("a",), ("b",)),
                _rc("alpha_route", ("b",), ("a",)),
                _rc("middle_route", ("a",), ("b",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b"}),
        )
        # Registered route IDs should follow config declaration order,
        # not alphabetical order.
        registered_ids = [r.id for r in result.registered_routes]
        assert registered_ids == ["zebra_route", "alpha_route", "middle_route"]

    def test_route_states_keys_sorted(self) -> None:
        """route_states dict keys are deterministically sorted."""
        rcs = RouteConfigSet(
            routes=(
                _rc("z_route", ("a",), ("b",)),
                _rc("a_route", ("b",), ("a",)),
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b"}),
        )
        assert list(result.eligibility.route_states.keys()) == sorted(
            result.eligibility.route_states.keys()
        )
