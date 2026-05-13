"""Focused tests for route eligibility/readiness metadata.

Covers:
* Registered route IDs are captured in eligibility
* Disabled route IDs are captured in eligibility
* Skipped routes (source adapter failed) are captured with reason and IDs
* Skipped routes (no surviving targets) are captured
* Unavailable is always empty in normal flow
* Unknown adapter refs still raise RouteValidationError
* Backward compat: RouteRegistrationResult behaves as a list
* Deterministic sorted ordering in configured/registered/disabled tuples
* Empty config produces empty eligibility
* Mixed scenario with registered, disabled, and skipped routes
* Builder integration: MedreApp.route_eligibility is populated
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.routing import Router
from medre.runtime.route_engine import (
    RouteEligibility,
    RouteRegistrationResult,
    RouteValidationError,
    SkippedRoute,
    UnavailableRoute,
    register_routes,
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.registered == ("r1",)

    def test_multiple_routes_registered_sorted(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("beta", ("a",), ("b",)),
            _rc("alpha", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        # Sorted deterministically
        assert result.eligibility.registered == ("alpha", "beta")

    def test_configured_captures_enabled_ids(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
            _rc("r2", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result.eligibility.configured == ("r1", "r2")


# ===================================================================
# Disabled route IDs captured
# ===================================================================


class TestDisabledIdsCaptured:
    """Eligibility.disabled contains disabled config route IDs."""

    def test_disabled_route_in_eligibility(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), enabled=False),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.disabled == ("r1",)
        assert result.eligibility.registered == ()

    def test_mixed_enabled_disabled(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("active", ("a",), ("b",), enabled=True),
            _rc("off", ("c",), ("d",), enabled=False),
        ))
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        # Adapter "a" is configured but failed to build
        result = register_routes(
            router, rcs,
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
        rcs = RouteConfigSet(routes=(
            _rc("bad_route", ("a",), ("b",)),
            _rc("good_route", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(
            router, rcs,
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b", "c")),
        ))
        router = Router()
        # Source "a" is fine, but both dests failed to build
        result = register_routes(
            router, rcs,
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b", "c")),
        ))
        router = Router()
        # "b" failed but "c" survived
        result = register_routes(
            router, rcs,
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert result.eligibility.unavailable == ()

    def test_unavailable_empty_when_degraded(self) -> None:
        """Even with degraded routes, unavailable remains empty."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(
            router, rcs,
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
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("ghost",), ("b",)),
        ))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown source"):
            register_routes(router, rcs, frozenset({"a", "b"}))

    def test_unknown_dest_raises(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("phantom",)),
        ))
        router = Router()
        with pytest.raises(RouteValidationError, match="unknown dest"):
            register_routes(router, rcs, frozenset({"a", "b"}))

    def test_configured_but_missing_is_not_unknown(self) -> None:
        """An adapter ID in adapter_ids but not in built_adapter_ids
        degrades rather than raises."""
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        # "a" is in adapter_ids (configured) but not built — should not raise
        result = register_routes(
            router, rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),
        )
        # Should skip, not raise
        assert len(result.eligibility.skipped) == 1


# ===================================================================
# Backward compat: RouteRegistrationResult is a list
# ===================================================================


class TestBackwardCompat:
    """RouteRegistrationResult behaves as a list for existing callers."""

    def test_len_works(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
            _rc("r2", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert len(result) == 2

    def test_iteration_works(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        route_ids = [r.id for r in result]
        assert route_ids == ["r1"]

    def test_indexing_works(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
            _rc("r2", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b", "c", "d"}))
        assert result[0].id == "r1"
        assert result[1].id == "r2"

    def test_empty_result_equality(self) -> None:
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        assert result == []

    def test_bool_truthy(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert bool(result) is True

    def test_bool_falsy(self) -> None:
        rcs = RouteConfigSet()
        router = Router()
        result = register_routes(router, rcs, frozenset({"a"}))
        assert bool(result) is False


# ===================================================================
# Deterministic sorted ordering
# ===================================================================


class TestDeterministicOrdering:
    """Eligibility tuples are sorted deterministically."""

    def test_configured_sorted(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("z_route", ("a",), ("b",)),
            _rc("a_route", ("c",), ("d",)),
            _rc("m_route", ("e",), ("f",)),
        ))
        router = Router()
        result = register_routes(
            router, rcs,
            frozenset({"a", "b", "c", "d", "e", "f"}),
        )
        assert result.eligibility.configured == ("a_route", "m_route", "z_route")

    def test_registered_sorted(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("z_route", ("a",), ("b",)),
            _rc("a_route", ("c",), ("d",)),
        ))
        router = Router()
        result = register_routes(
            router, rcs,
            frozenset({"a", "b", "c", "d"}),
        )
        assert result.eligibility.registered == ("a_route", "z_route")

    def test_disabled_sorted(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("z_off", ("a",), ("b",), enabled=False),
            _rc("a_off", ("c",), ("d",), enabled=False),
        ))
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
        assert e.skipped == ()
        assert e.unavailable == ()


# ===================================================================
# Mixed scenario
# ===================================================================


class TestMixedScenario:
    """Combination of registered, disabled, and skipped routes."""

    def test_full_mixed(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("active1", ("a",), ("b",)),
            _rc("disabled1", ("c",), ("d",), enabled=False),
            _rc("active2", ("e",), ("f",)),
            _rc("will_skip", ("g",), ("h",)),  # source "g" will fail
        ))
        router = Router()
        result = register_routes(
            router, rcs,
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
        # Skipped
        assert len(e.skipped) == 1
        assert e.skipped[0].route_id == "will_skip"
        assert e.skipped[0].reason == "source_adapter_failed"
        assert e.skipped[0].failed_adapter_ids == ("g",)
        # Unavailable empty
        assert e.unavailable == ()


# ===================================================================
# Router behavior unchanged
# ===================================================================


class TestRouterBehaviorUnchanged:
    """Routes still match events correctly after eligibility changes."""

    def test_registered_route_matches(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))

        event = _make_event(source_adapter="a")
        matched = router.match(event)
        assert len(matched) == 1
        assert matched[0].id == "r1"

    def test_skipped_route_not_in_router(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",)),
        ))
        router = Router()
        register_routes(
            router, rcs,
            adapter_ids=frozenset({"a", "b"}),
            built_adapter_ids=frozenset({"b"}),  # "a" failed
        )
        # Route was skipped, so router has no routes
        event = _make_event(source_adapter="a")
        assert router.match(event) == []

    def test_disabled_route_not_in_router(self) -> None:
        rcs = RouteConfigSet(routes=(
            _rc("r1", ("a",), ("b",), enabled=False),
        ))
        router = Router()
        register_routes(router, rcs, frozenset({"a", "b"}))
        event = _make_event(source_adapter="a")
        assert router.match(event) == []


# ===================================================================
# SkippedRoute / UnavailableRoute frozen dataclass
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

    def test_route_eligibility_frozen(self) -> None:
        e = RouteEligibility(
            configured=("r1",),
            registered=("r1",),
            disabled=(),
            skipped=(),
            unavailable=(),
        )
        with pytest.raises(AttributeError):
            e.registered = ()  # type: ignore[misc]


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
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                    "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        from medre.adapters.meshtastic.config import MeshtasticConfig
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

        route1 = RouteConfig.from_toml_dict("bridge", {
            "source_adapters": ["radio_a"],
            "dest_adapters": ["radio_b"],
        })

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-eligibility"),
            adapters=AdapterConfigSet(
                meshtastic={
                    "radio_a": MeshtasticRuntimeConfig(
                        adapter_id="radio_a", config=mesh_a,
                    ),
                    "radio_b": MeshtasticRuntimeConfig(
                        adapter_id="radio_b", config=mesh_b,
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
        assert app.route_eligibility.skipped == ()
        assert app.route_eligibility.unavailable == ()
