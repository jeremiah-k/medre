"""Tests for medre.runtime.builder: degraded route validation
and adapter build failure handling for routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContract
from medre.core.routing.router import Router
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from medre.runtime.route_engine import RouteValidationError, register_routes
from medre.runtime.routes import RouteConfig, RouteConfigSet
from tests.helpers.runtime_builder import (
    clean_path_env,
    make_fake_matrix_config,
    make_fake_meshcore_config,
    make_fake_meshtastic_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_path_env(monkeypatch)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Route config helper
# ---------------------------------------------------------------------------


def _rc(
    route_id: str,
    source_adapters: tuple[str, ...],
    dest_adapters: tuple[str, ...],
    *,
    enabled: bool = True,
) -> RouteConfig:
    """Helper to create a RouteConfig with minimal boilerplate."""
    return RouteConfig(
        route_id=route_id,
        source_adapters=source_adapters,
        dest_adapters=dest_adapters,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Degraded route validation (adapter build failures)
# ---------------------------------------------------------------------------


class TestDegradedRouteValidation:
    """Routes referencing adapters that failed to build are degraded,
    not fatal, as long as at least one adapter remains usable.

    Unknown/typo adapter IDs still raise RouteValidationError.
    """

    def test_route_with_all_working_adapters_survives(self) -> None:
        """Route referencing only successfully built adapters is registered."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(
            router,
            rcs,
            _configured_adapter_ids := frozenset({"a", "b"}),
            _built_adapter_ids := frozenset({"a", "b"}),
        )
        assert len(result.registered_routes) == 1
        assert result.registered_routes[0].id == "r1"

    def test_route_with_failed_source_adapter_skipped(self) -> None:
        """Route whose source adapter failed to build is entirely skipped."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        # "a" is configured but failed to build; "b" built OK
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b"}),
            frozenset({"b"}),
        )
        assert result.registered_routes == ()

    def test_route_with_failed_dest_adapter_degraded(self) -> None:
        """Route with a failed dest adapter gets that target removed."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b", "c")),))
        router = Router()
        # "a" and "b" built OK; "c" failed to build
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c"}),
            frozenset({"a", "b"}),
        )
        assert len(result.registered_routes) == 1
        route = result.registered_routes[0]
        assert route.source.adapter == "a"
        assert [t.adapter for t in route.targets] == ["b"]

    def test_route_all_dests_failed_skipped(self) -> None:
        """Route with all dest adapters failed is skipped entirely."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        # "a" built OK; "b" failed
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b"}),
            frozenset({"a"}),
        )
        assert result.registered_routes == ()

    def test_mixed_routes_partial_degradation(self) -> None:
        """Multiple routes: some survive, some degraded, some skipped."""
        rcs = RouteConfigSet(
            routes=(
                _rc("good_route", ("a",), ("b",)),  # both OK → survives
                _rc("degraded_route", ("a",), ("b", "c")),  # c failed → degraded
                _rc("dead_route", ("c",), ("b",)),  # c source failed → skipped
            )
        )
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"a", "b", "c"}),
            frozenset({"a", "b"}),
        )
        ids = [r.id for r in result.registered_routes]
        assert "good_route" in ids
        assert "degraded_route" in ids
        assert "dead_route" not in ids
        # Verify degraded_route has only "b" as target
        degraded = next(r for r in result.registered_routes if r.id == "degraded_route")
        assert [t.adapter for t in degraded.targets] == ["b"]

    def test_unknown_adapter_still_raises(self) -> None:
        """Route referencing a truly unknown adapter ID still raises."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("typo_id",)),))
        router = Router()
        with pytest.raises(RouteValidationError, match="typo_id"):
            register_routes(
                router,
                rcs,
                frozenset({"a"}),  # "typo_id" not configured at all
                frozenset({"a"}),
            )

    def test_no_built_adapter_ids_falls_back(self) -> None:
        """Calling register_routes without built_adapter_ids uses adapter_ids for both."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("b",)),))
        router = Router()
        result = register_routes(router, rcs, frozenset({"a", "b"}))
        assert len(result.registered_routes) == 1

    def test_unknown_adapter_ids_raise_without_built_ids(self) -> None:
        """Without built_adapter_ids, unknown adapter IDs still raise."""
        rcs = RouteConfigSet(routes=(_rc("r1", ("a",), ("unknown",)),))
        router = Router()
        with pytest.raises(RouteValidationError):
            register_routes(router, rcs, frozenset({"a"}))

    def test_full_build_one_good_one_failed_adapter(
        self, tmp_paths: MedrePaths
    ) -> None:
        """RuntimeBuilder.build() with one fake adapter succeeding and one
        failing produces a degraded runtime with build_failures recorded
        and routes involving only the working adapter surviving."""
        # Adapter "fm" (fake matrix) will build fine.
        # Adapter "ft" (fake meshtastic) will be made to fail.
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        # Route from fm → ft (involves failed adapter as dest)
        route_fm_to_ft = RouteConfig(
            route_id="fm_to_ft",
            source_adapters=("fm",),
            dest_adapters=("ft",),
        )
        # Route from ft → fm (involves failed adapter as source)
        route_ft_to_fm = RouteConfig(
            route_id="ft_to_fm",
            source_adapters=("ft",),
            dest_adapters=("fm",),
        )
        routes = RouteConfigSet(routes=(route_fm_to_ft, route_ft_to_fm))

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=routes,
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Patch _build_single_adapter to make ft fail
        original_build = builder._build_single_adapter
        call_count = 0

        def _selective_build(
            transport: str, adapter_id: str, rtc: Any
        ) -> AdapterContract:
            nonlocal call_count
            call_count += 1
            if adapter_id == "ft":
                raise RuntimeConfigError("simulated build failure for ft")
            return original_build(transport, adapter_id, rtc)

        with patch.object(
            builder, "_build_single_adapter", side_effect=_selective_build
        ):
            app = builder.build()

        # fm built successfully
        assert "fm" in app.adapters
        # ft did NOT build
        assert "ft" not in app.adapters
        # Build failures recorded
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "ft"

        # Routes: fm_to_ft has no surviving targets (ft failed) → skipped
        # Routes: ft_to_fm has failed source → skipped
        # So no routes registered
        assert len(app.router._routes) == 0

    def test_full_build_one_good_one_failed_unrelated_route_survives(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter builds, one fails; a route using only the good
        adapter as both source and dest survives, while routes involving
        the failed adapter are degraded."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        # Unrelated route: fm → fm (self-route not allowed by config, so use two matrix instances)
        # Actually self-route check prevents this. Let's use a third adapter.
        rt_core = MeshCoreRuntimeConfig(
            adapter_id="fc",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshcore_config(),
        )
        # Route between two working adapters
        route_good = RouteConfig(
            route_id="good_route",
            source_adapters=("fm",),
            dest_adapters=("fc",),
        )
        # Route involving failed adapter
        route_degraded = RouteConfig(
            route_id="degraded_route",
            source_adapters=("fm",),
            dest_adapters=("ft",),  # ft will fail
        )
        routes = RouteConfigSet(routes=(route_good, route_degraded))

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
                meshcore={"fc": rt_core},
            ),
            routes=routes,
        )
        builder = RuntimeBuilder(config, tmp_paths)

        original_build = builder._build_single_adapter

        def _selective_build(
            transport: str, adapter_id: str, rtc: Any
        ) -> AdapterContract:
            if adapter_id == "ft":
                raise RuntimeConfigError("simulated build failure for ft")
            return original_build(transport, adapter_id, rtc)

        with patch.object(
            builder, "_build_single_adapter", side_effect=_selective_build
        ):
            app = builder.build()

        # Good adapters built
        assert "fm" in app.adapters
        assert "fc" in app.adapters
        assert "ft" not in app.adapters
        assert len(app.build_failures) == 1

        # good_route (fm→fc) should be registered
        # degraded_route (fm→ft) has no surviving targets → skipped
        route_ids = list(app.router._routes.keys())
        assert "good_route" in route_ids
        assert "degraded_route" not in route_ids
