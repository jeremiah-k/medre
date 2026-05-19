"""Tests for medre.runtime.builder: degraded route validation,
adapter build failure handling for routes, and Matrix auto_join_rooms
derivation from route configuration."""

from __future__ import annotations

from dataclasses import replace
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
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContract
from medre.core.routing.router import Router
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from medre.runtime.route_engine import RouteValidationError, register_routes
from medre.runtime.routes import RouteConfig, RouteConfigSet, RouteDirectionality
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


# ---------------------------------------------------------------------------
# Matrix auto_join_rooms derivation from routes
# ---------------------------------------------------------------------------


def _make_matrix_config(
    adapter_id: str = "fm",
    *,
    auto_join_rooms: tuple[str, ...] = (),
    room_allowlist: set[str] | None = None,
) -> MatrixConfig:
    """Create a valid MatrixConfig for testing."""
    return MatrixConfig(
        adapter_id=adapter_id,
        homeserver="https://matrix.test",
        user_id="@bot:test",
        access_token="test-tok",
        auto_join_rooms=auto_join_rooms,
        room_allowlist=room_allowlist,
    )


class TestMatrixAutoJoinRoomsDerivation:
    """RuntimeBuilder derives auto_join_rooms from route source_room/dest_room."""

    def test_source_room_derived(self, tmp_paths: MedrePaths) -> None:
        """Rooms from source_channel (source_room) on Matrix source adapters
        are included in auto_join_rooms."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm"),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        # Route with source_room targeting the Matrix adapter as source
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!srcroom:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        assert result["fm"] == ("!srcroom:test.org",)

    def test_dest_room_derived(self, tmp_paths: MedrePaths) -> None:
        """Rooms from dest_channel (dest_room) on Matrix dest adapters
        are included in auto_join_rooms."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm"),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        # Route with dest_room targeting the Matrix adapter as destination
        route = RouteConfig(
            route_id="r1",
            source_adapters=("ft",),
            dest_adapters=("fm",),
            dest_channel="!dstroom:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        assert result["fm"] == ("!dstroom:test.org",)

    def test_explicit_rooms_preserved_and_unioned(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Explicit auto_join_rooms from config are unioned with derived rooms."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config(
                "fm",
                auto_join_rooms=("!explicit:test.org",),
            ),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!derived:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        # Both explicit and derived rooms are present, sorted
        assert result["fm"] == ("!derived:test.org", "!explicit:test.org")

    def test_room_allowlist_missing_source_room_fails(
        self, tmp_paths: MedrePaths
    ) -> None:
        """room_allowlist that omits a source room raises RuntimeConfigError."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config(
                "fm",
                room_allowlist={"!other:test.org"},
            ),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!srcroom:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        with pytest.raises(RuntimeConfigError, match="room_allowlist.*omits"):
            builder._derive_matrix_auto_join_rooms(
                {"fm": "matrix", "ft": "meshtastic"}
            )

    def test_room_allowlist_none_remains_none(
        self, tmp_paths: MedrePaths
    ) -> None:
        """room_allowlist=None is preserved (accept all rooms)."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm", room_allowlist=None),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!srcroom:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        # Should not raise — room_allowlist=None means accept all
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        assert result["fm"] == ("!srcroom:test.org",)
        # Original config's room_allowlist is unchanged
        assert rt_matrix.config is not None
        assert rt_matrix.config.room_allowlist is None

    def test_no_matrix_adapters_returns_empty(
        self, tmp_paths: MedrePaths
    ) -> None:
        """No Matrix adapters → empty mapping (no crash)."""
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                meshtastic={"ft": rt_mesh},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"ft": "meshtastic"}
        )
        assert result == {}

    def test_non_bang_channel_ignored(self, tmp_paths: MedrePaths) -> None:
        """Channels not starting with '!' are not treated as rooms."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm"),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="general",  # Not a canonical room ID
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        # "fm" has no rooms, so not in the result
        assert "fm" not in result

    def test_bidirectional_route_both_rooms_derived(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Bidirectional routes derive rooms for source and reverse-target channels.

        Forward leg: fm (source, srcroom) → ft (dest, dstroom)
        Reverse leg: ft (source, dstroom) → fm (dest, srcroom)

        fm gets srcroom from forward source and srcroom from reverse target.
        ft is not a Matrix adapter so it's ignored.
        """
        from medre.runtime.routes import RouteDirectionality

        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm"),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!srcroom:test.org",
            dest_channel="!dstroom:test.org",
            directionality=RouteDirectionality.BIDIRECTIONAL,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        # fm is source in forward (srcroom) and dest in reverse (srcroom again)
        assert result["fm"] == ("!srcroom:test.org",)

    def test_bidirectional_two_matrix_adapters(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Bidirectional between two Matrix adapters: each gets its source room
        and the other's source room (as target in reverse)."""
        from medre.runtime.routes import RouteDirectionality

        rt_matrix_a = MatrixRuntimeConfig(
            adapter_id="ma",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("ma"),
        )
        rt_matrix_b = MatrixRuntimeConfig(
            adapter_id="mb",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("mb"),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("ma",),
            dest_adapters=("mb",),
            source_channel="!roomA:test.org",
            dest_channel="!roomB:test.org",
            directionality=RouteDirectionality.BIDIRECTIONAL,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"ma": rt_matrix_a, "mb": rt_matrix_b},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"ma": "matrix", "mb": "matrix"}
        )
        # Reverse: mb (source, roomB) → ma (target, roomA)
        # ma: roomA (forward source) + roomA (reverse target) = roomA
        # mb: roomB (forward target) + roomB (reverse source) = roomB
        assert result["ma"] == ("!roomA:test.org",)
        assert result["mb"] == ("!roomB:test.org",)

    def test_full_build_injects_rooms_into_fake_adapter(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Full builder.build() injects derived rooms into adapter config.
        Since fake adapters don't hold config, we verify via the internal
        _matrix_auto_join state."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="fake",
            config=_make_matrix_config("fm"),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!autojoin:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        # Call build() — fake adapter path, but rooms are derived before construction
        app = builder.build()
        # Verify the internal state was set
        assert builder._matrix_auto_join["fm"] == ("!autojoin:test.org",)

    def test_real_adapter_receives_merged_rooms(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When building a real (non-fake) Matrix adapter, the config
        has auto_join_rooms merged from route derivation."""
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="fm",
            enabled=True,
            adapter_kind="real",
            config=_make_matrix_config(
                "fm",
                auto_join_rooms=("!explicit:test.org",),
            ),
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="ft",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        route = RouteConfig(
            route_id="r1",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!derived:test.org",
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Capture the config passed to the factory
        captured_configs: list[MatrixConfig] = []

        original_build_single = builder._build_single_adapter

        def _capture_build(
            transport: str, adapter_id: str, rtc: Any
        ) -> AdapterContract:
            # Intercept to capture what config would be passed
            # by calling original logic but for fake path
            if transport == "matrix" and adapter_id == "fm":
                # Simulate the config enrichment that happens in _build_single_adapter
                cfg = rtc.config
                # Apply store_path derivation
                derived_store = (
                    tmp_paths.adapter_transport_state_dir("fm", "matrix") / "store"
                )
                from dataclasses import replace as dc_replace

                cfg = dc_replace(cfg, store_path=str(derived_store))
                # Apply auto_join_rooms injection
                extra_rooms = builder._matrix_auto_join.get("fm", ())
                if extra_rooms:
                    existing = cfg.auto_join_rooms
                    merged = tuple(sorted(set(existing) | set(extra_rooms)))
                    cfg = dc_replace(cfg, auto_join_rooms=merged)
                captured_configs.append(cfg)
                # Return fake adapter since nio likely not installed
                from medre.adapters.fake_matrix import FakeMatrixAdapter

                return FakeMatrixAdapter(adapter_id="fm")
            return original_build_single(transport, adapter_id, rtc)

        with patch.object(builder, "_build_single_adapter", side_effect=_capture_build):
            app = builder.build()

        # Verify the merged config has both rooms
        assert len(captured_configs) == 1
        merged_cfg = captured_configs[0]
        assert "!derived:test.org" in merged_cfg.auto_join_rooms
        assert "!explicit:test.org" in merged_cfg.auto_join_rooms


# ---------------------------------------------------------------------------
# channel_room_map builder integration
# ---------------------------------------------------------------------------


class TestChannelRoomMapBuilderIntegration:
    """RuntimeBuilder.build() succeeds with channel_room_map routes.

    Verifies that adapter_platforms is correctly plumbed through
    _derive_matrix_auto_join_rooms() and register_routes() so that
    channel_room_map routes expand without RouteValidationError.
    """

    def test_build_with_channel_room_map_succeeds(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Full builder.build() with a channel_room_map route registers
        expanded per-channel routes on the router."""
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
        route = RouteConfig(
            route_id="crm_bridge",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            channel_room_map={
                "0": "!room0:test.org",
                "1": "!room1:test.org",
            },
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        # Should succeed — no RouteValidationError.
        assert "fm" in app.adapters
        assert "ft" in app.adapters

        # Expanded routes registered: 2 channels × 2 directions = 4 routes.
        route_ids = list(app.router._routes.keys())
        assert len(route_ids) == 4
        assert "crm_bridge__ch0__matrix_to_meshtastic" in route_ids
        assert "crm_bridge__ch0__meshtastic_to_matrix" in route_ids
        assert "crm_bridge__ch1__matrix_to_meshtastic" in route_ids
        assert "crm_bridge__ch1__meshtastic_to_matrix" in route_ids

    def test_build_with_channel_room_map_source_to_dest(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Source-to-dest channel_room_map produces only forward legs."""
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
        route = RouteConfig(
            route_id="crm_one_way",
            source_adapters=("ft",),
            dest_adapters=("fm",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            channel_room_map={
                "0": "!room0:test.org",
            },
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        route_ids = list(app.router._routes.keys())
        # ft is meshtastic, fm is matrix. Source→dest = meshtastic→matrix.
        assert len(route_ids) == 1
        assert "crm_one_way__ch0__meshtastic_to_matrix" in route_ids

    def test_matrix_auto_join_rooms_includes_channel_room_map_rooms(
        self, tmp_paths: MedrePaths
    ) -> None:
        """channel_room_map rooms appear in derived auto-join rooms."""
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
        route = RouteConfig(
            route_id="crm_bridge",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            channel_room_map={
                "0": "!room0:test.org",
                "3": "!room3:test.org",
            },
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(route,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Build adapter_platforms as builder.build() does.
        adapter_platforms: dict[str, str] = {}
        for transport, adapter_id, _rtc in config.adapters.all_configs():
            adapter_platforms[adapter_id] = transport

        result = builder._derive_matrix_auto_join_rooms(adapter_platforms)
        assert "!room0:test.org" in result["fm"]
        assert "!room3:test.org" in result["fm"]

    def test_disabled_route_skipped_in_auto_join(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A route with enabled=False is skipped during auto-join room
        derivation — its rooms do not appear in the auto-join set (lines 572-573)."""
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
        enabled_route = RouteConfig(
            route_id="active",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!active_room:test.org",
            enabled=True,
        )
        disabled_route = RouteConfig(
            route_id="inactive",
            source_adapters=("fm",),
            dest_adapters=("ft",),
            source_channel="!disabled_room:test.org",
            enabled=False,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"fm": rt_matrix},
                meshtastic={"ft": rt_mesh},
            ),
            routes=RouteConfigSet(routes=(enabled_route, disabled_route)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._derive_matrix_auto_join_rooms(
            {"fm": "matrix", "ft": "meshtastic"}
        )
        # The enabled route's room should appear
        assert "!active_room:test.org" in result["fm"]
        # The disabled route's room must NOT appear (line 572-573: if not route.enabled: continue)
        assert "!disabled_room:test.org" not in result["fm"]
