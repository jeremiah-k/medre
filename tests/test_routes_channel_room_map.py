"""Tests for channel_room_map: config validation and runtime expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.core.routing.router import Router
from medre.config.routes import (
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)


# ---------------------------------------------------------------------------
# channel_room_map — config validation
# ---------------------------------------------------------------------------


class TestChannelRoomMapConfig:
    """RouteConfig channel_room_map parsing and validation."""

    def _base(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "source_adapters": ["matrix_adapter"],
            "dest_adapters": ["mesh_adapter"],
        }
        data.update(overrides)
        return data

    # --- valid construction ---

    def test_valid_map_parsed(self) -> None:
        data = self._base(
            channel_room_map={"0": "!room0:example.com", "1": "!room1:example.com"},
        )
        r = RouteConfig.from_toml_dict("crm_route", data)
        assert r.channel_room_map == {
            "0": "!room0:example.com",
            "1": "!room1:example.com",
        }

    def test_int_channel_keys_normalized(self) -> None:
        """TOML inline tables can produce int keys; normalize to str."""
        data = self._base(
            channel_room_map={0: "!room0:example.com", 1: "!room1:example.com"},
        )
        r = RouteConfig.from_toml_dict("crm_route", data)
        assert r.channel_room_map == {
            "0": "!room0:example.com",
            "1": "!room1:example.com",
        }

    def test_none_when_absent(self) -> None:
        data = self._base()
        r = RouteConfig.from_toml_dict("no_map", data)
        assert r.channel_room_map is None

    def test_single_channel(self) -> None:
        data = self._base(channel_room_map={"3": "!room3:example.com"})
        r = RouteConfig.from_toml_dict("crm_route", data)
        assert r.channel_room_map == {"3": "!room3:example.com"}

    def test_string_channel_key_accepted(self) -> None:
        """String channel key like "3" is accepted as-is (lines 520-523)."""
        data = self._base(channel_room_map={"3": "!room:example.com"})
        r = RouteConfig.from_toml_dict("str_key_route", data)
        assert r.channel_room_map == {"3": "!room:example.com"}

    def test_all_channels_0_through_7(self) -> None:
        crm = {str(i): f"!room{i}:example.com" for i in range(8)}
        data = self._base(channel_room_map=crm)
        r = RouteConfig.from_toml_dict("crm_route", data)
        assert r.channel_room_map is not None
        assert len(r.channel_room_map) == 8

    def test_reject_empty_channel_room_map(self) -> None:
        """Empty channel_room_map dict is rejected."""
        with pytest.raises(ConfigValidationError):
            RouteConfig.from_toml_dict(
                "empty",
                self._base(channel_room_map={}),
            )

    # --- rejection: non-dict ---

    def test_reject_non_dict(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a table"):
            RouteConfig.from_toml_dict("bad", self._base(channel_room_map="not_a_dict"))

    def test_reject_list(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a table"):
            RouteConfig.from_toml_dict("bad", self._base(channel_room_map=[{"0": "!r:t"}]))

    # --- rejection: channel key validation ---

    def test_reject_bool_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="boolean"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={True: "!room:example.com"}),
            )

    def test_reject_negative_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="out of range"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={-1: "!room:example.com"}),
            )

    def test_reject_channel_8(self) -> None:
        with pytest.raises(ConfigValidationError, match="out of range"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={8: "!room:example.com"}),
            )

    def test_reject_non_integer_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="not a valid integer"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"abc": "!room:example.com"}),
            )

    # --- rejection: room value validation ---

    def test_reject_blank_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"0": "  "}),
            )

    def test_reject_empty_string_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"0": ""}),
            )

    def test_reject_alias_room(self) -> None:
        """Room aliases starting with '#' are rejected at config time."""
        with pytest.raises(ConfigValidationError):
            RouteConfig.from_toml_dict(
                "bad_alias",
                self._base(channel_room_map={"0": "#room:example.com"}),
            )

    def test_reject_non_canonical_general(self) -> None:
        """Plain names without sigils are not valid room IDs."""
        with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"0": "general"}),
            )

    def test_reject_non_canonical_bare_domain(self) -> None:
        """Bare domain-style strings are not valid room IDs."""
        with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"0": "room:example.com"}),
            )

    def test_reject_non_canonical_event_id(self) -> None:
        """Event IDs (starting with '$') are not valid room IDs."""
        with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(channel_room_map={"0": "$event:example.com"}),
            )

    def test_accepts_canonical_room(self) -> None:
        """Canonical room IDs starting with '!' are accepted."""
        r = RouteConfig.from_toml_dict(
            "ok",
            self._base(channel_room_map={"0": "!room:example.com"}),
        )
        assert r.channel_room_map == {"0": "!room:example.com"}

    # --- rejection: duplicate normalized channel ---

    def test_reject_duplicate_channel(self) -> None:
        """String '1' and int 1 normalize to the same channel."""
        with pytest.raises(ConfigValidationError, match="duplicate channel"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    channel_room_map={
                        "1": "!room1:example.com",
                        1: "!room1_dup:example.com",
                    }
                ),
            )

    # --- rejection: mutual exclusion with targeting fields ---

    def test_reject_with_source_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    source_channel="ch0",
                    channel_room_map={"0": "!room:example.com"},
                ),
            )

    def test_reject_with_dest_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    dest_channel="ch1",
                    channel_room_map={"0": "!room:example.com"},
                ),
            )

    def test_reject_with_source_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    source_room="!room:example.com",
                    channel_room_map={"0": "!other:example.com"},
                ),
            )

    def test_reject_with_dest_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    dest_room="!room:example.com",
                    channel_room_map={"0": "!other:example.com"},
                ),
            )

    # --- rejection: multiple adapters ---

    def test_reject_multiple_source_adapters(self) -> None:
        with pytest.raises(ConfigValidationError, match="one source adapter"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a", "b"],
                    "dest_adapters": ["c"],
                    "channel_room_map": {"0": "!room:example.com"},
                },
            )

    def test_reject_multiple_dest_adapters(self) -> None:
        with pytest.raises(ConfigValidationError, match="one dest adapter"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b", "c"],
                    "channel_room_map": {"0": "!room:example.com"},
                },
            )

    # --- rejection: duplicate rooms ---

    def test_reject_duplicate_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="duplicate room"):
            RouteConfig.from_toml_dict(
                "bad",
                self._base(
                    channel_room_map={
                        "0": "!room:example.com",
                        "1": "!room:example.com",
                    }
                ),
            )

    # --- integration: TOML loader ---

    def test_toml_integration(self, tmp_path: Path) -> None:
        toml_content = """\
[runtime]
name = "crm_test"

[routes.bridge]
source_adapters = ["matrix_adapter"]
dest_adapters = ["mesh_adapter"]
directionality = "bidirectional"

[routes.bridge.channel_room_map]
0 = "!room0:example.com"
1 = "!room1:example.com"
"""
        p = tmp_path / "config.toml"
        p.write_text(toml_content)
        config, _, _ = load_config(str(p))
        r = config.routes.routes[0]
        assert r.channel_room_map == {
            "0": "!room0:example.com",
            "1": "!room1:example.com",
        }


# ---------------------------------------------------------------------------
# channel_room_map — runtime expansion
# ---------------------------------------------------------------------------


class TestChannelRoomMapExpansion:
    """Runtime expansion of channel_room_map routes."""

    def _crm_config(
        self,
        route_id: str = "bridge",
        directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL,
        channel_room_map: dict[str, str] | None = None,
    ) -> RouteConfig:
        if channel_room_map is None:
            channel_room_map = {"0": "!room0:example.com", "1": "!room1:example.com"}
        return RouteConfig(
            route_id=route_id,
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            directionality=directionality,
            channel_room_map=channel_room_map,
        )

    @staticmethod
    def _platforms() -> dict[str, str]:
        return {"matrix_adapter": "matrix", "mesh_adapter": "meshtastic"}

    def test_bidirectional_2_channels_4_routes(self) -> None:
        """2 channels x bidirectional = 4 routes."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 4

    def test_matrix_to_meshtastic_route_fields(self) -> None:
        """Matrix→Meshtastic route: source is Matrix with room, target is Mesh with channel."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        fwd = [r for r in routes if "matrix_to_meshtastic" in r.id]
        assert len(fwd) == 2
        for r in fwd:
            assert r.source.adapter == "matrix_adapter"
            ch = r.targets[0].channel
            assert ch in ("0", "1")
            assert r.source.channel in ("!room0:example.com", "!room1:example.com")

    def test_meshtastic_to_matrix_route_fields(self) -> None:
        """Meshtastic→Matrix route: source is Mesh with channel, target is Matrix with room."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        rev = [r for r in routes if "meshtastic_to_matrix" in r.id]
        assert len(rev) == 2
        for r in rev:
            assert r.source.adapter == "mesh_adapter"
            assert r.targets[0].adapter == "matrix_adapter"
            assert r.source.channel in ("0", "1")
            assert r.targets[0].channel in ("!room0:example.com", "!room1:example.com")

    def test_channel_1_resolves_correctly(self) -> None:
        """Meshtastic source channel '1' resolves to target Matrix room."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        ch1_rev = next(
            r
            for r in routes
            if r.source.channel == "1" and "meshtastic_to_matrix" in r.id
        )
        assert ch1_rev.targets[0].channel == "!room1:example.com"

    def test_matrix_room_resolves_to_channel(self) -> None:
        """Matrix source room ID resolves to target Meshtastic channel."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        fwd_ch1 = next(
            r
            for r in routes
            if r.source.channel == "!room1:example.com"
            and "matrix_to_meshtastic" in r.id
        )
        assert fwd_ch1.targets[0].channel == "1"

    def test_unmapped_channel_no_route(self) -> None:
        """Meshtastic channel '2' (not in map) produces no matched route."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config(channel_room_map={"0": "!room0:example.com"})
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        # Only channel 0 should have routes
        ch2_routes = [r for r in routes if r.source.channel == "2"]
        assert len(ch2_routes) == 0
        assert len(routes) == 2  # 1 channel x bidirectional

    def test_source_to_dest_only(self) -> None:
        """source_to_dest creates only Matrix→Meshtastic legs."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config(
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            channel_room_map={"0": "!room0:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 1
        assert routes[0].source.adapter == "matrix_adapter"
        assert "matrix_to_meshtastic" in routes[0].id

    def test_dest_to_source_only(self) -> None:
        """dest_to_source creates only Meshtastic→Matrix legs."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config(
            directionality=RouteDirectionality.DEST_TO_SOURCE,
            channel_room_map={"0": "!room0:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 1
        assert routes[0].source.adapter == "mesh_adapter"
        assert "meshtastic_to_matrix" in routes[0].id

    def test_explicit_route_unchanged(self) -> None:
        """Explicit source_room/dest_channel route still expands as before."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = RouteConfig(
            route_id="explicit",
            source_adapters=("a",),
            dest_adapters=("b",),
            source_channel="!room:example.com",
            dest_channel="1",
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, {})
        assert len(routes) == 1
        assert routes[0].id == "explicit"
        assert routes[0].source.channel == "!room:example.com"
        assert routes[0].targets[0].channel == "1"

    def test_deterministic_route_ids(self) -> None:
        """Expanded route IDs follow the deterministic naming pattern."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = self._crm_config(
            channel_room_map={"0": "!r0:e.com", "1": "!r1:e.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        ids = sorted(r.id for r in routes)
        assert ids == [
            "bridge__ch0__matrix_to_meshtastic",
            "bridge__ch0__meshtastic_to_matrix",
            "bridge__ch1__matrix_to_meshtastic",
            "bridge__ch1__meshtastic_to_matrix",
        ]

    def test_provenance_maps_expanded_to_config(self) -> None:
        """Provenance maps expanded channel routes to config route ID."""
        from medre.runtime.route_engine import register_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        router = Router()
        result = register_routes(
            router,
            rcs,
            frozenset({"matrix_adapter", "mesh_adapter"}),
            adapter_platforms=self._platforms(),
        )
        for expanded_id in result.provenance:
            assert result.provenance[expanded_id] == "bridge"

    def test_reversed_source_dest_platforms(self) -> None:
        """Meshtastic source, Matrix dest still works correctly."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = RouteConfig(
            route_id="rev_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            channel_room_map={"0": "!room0:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 1
        r = routes[0]
        # source_to_dest from mesh perspective = meshtastic_to_matrix
        assert r.source.adapter == "mesh_adapter"
        assert r.targets[0].adapter == "matrix_adapter"
        assert "meshtastic_to_matrix" in r.id

    def test_platform_lookup_fails_raises(self) -> None:
        """Missing platform info raises RouteValidationError."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="cannot determine platform"):
            build_runtime_routes(rcs, {})  # empty platforms

    def test_wrong_platforms_raises(self) -> None:
        """Two non-matrix/meshtastic platforms raises RouteValidationError."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = self._crm_config()
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="one Matrix and one Meshtastic"):
            build_runtime_routes(rcs, {"matrix_adapter": "lxmf", "mesh_adapter": "meshcore"})

    def test_explicit_route_ignores_adapter_platforms(self) -> None:
        """Non-channel_room_map routes work fine with empty adapter_platforms."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = RouteConfig(
            route_id="plain",
            source_adapters=("a",),
            dest_adapters=("b",),
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, {})
        assert len(routes) == 1
        assert routes[0].id == "plain"

    # --- runtime cardinality checks (FIX 2) ---

    def test_empty_source_adapters_raises(self) -> None:
        """Directly constructed RouteConfig with empty source_adapters raises RouteValidationError."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = RouteConfig(
            route_id="empty_src",
            source_adapters=(),
            dest_adapters=("mesh_adapter",),
            channel_room_map={"0": "!room:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="exactly one source"):
            build_runtime_routes(rcs, self._platforms())

    def test_multiple_source_adapters_raises(self) -> None:
        """Multiple source_adapters raises RouteValidationError at runtime."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = RouteConfig(
            route_id="multi_src",
            source_adapters=("a", "b"),
            dest_adapters=("mesh_adapter",),
            channel_room_map={"0": "!room:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="exactly one source"):
            build_runtime_routes(rcs, {"a": "matrix", "b": "matrix", "mesh_adapter": "meshtastic"})

    def test_empty_dest_adapters_raises(self) -> None:
        """Directly constructed RouteConfig with empty dest_adapters raises RouteValidationError."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = RouteConfig(
            route_id="empty_dst",
            source_adapters=("matrix_adapter",),
            dest_adapters=(),
            channel_room_map={"0": "!room:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="exactly one source"):
            build_runtime_routes(rcs, self._platforms())

    def test_multiple_dest_adapters_raises(self) -> None:
        """Multiple dest_adapters raises RouteValidationError at runtime."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = RouteConfig(
            route_id="multi_dst",
            source_adapters=("matrix_adapter",),
            dest_adapters=("a", "b"),
            channel_room_map={"0": "!room:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError, match="exactly one source"):
            build_runtime_routes(rcs, {"matrix_adapter": "matrix", "a": "meshtastic", "b": "meshtastic"})

    def test_valid_one_source_one_dest_expands(self) -> None:
        """Valid one-source/one-dest config still expands correctly."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = RouteConfig(
            route_id="valid",
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            channel_room_map={"0": "!room0:example.com", "1": "!room1:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 2
        assert all("matrix_to_meshtastic" in r.id for r in routes)

    # --- Tests A, B, C: targeted line coverage ---

    def test_dest_adapter_missing_from_adapter_platforms_raises(self) -> None:
        """Dest adapter not in adapter_platforms raises RouteValidationError
        with 'cannot determine platform for dest adapter' (lines 491-492)."""
        from medre.runtime.route_engine import RouteValidationError, build_runtime_routes

        rc = RouteConfig(
            route_id="dest_missing",
            source_adapters=("matrix_adapter",),
            dest_adapters=("unknown_adapter",),
            channel_room_map={"0": "!room0:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(
            RouteValidationError,
            match=r"cannot determine platform for dest adapter.*unknown_adapter",
        ):
            # Source platform is found, but dest is missing → hits lines 491-492
            build_runtime_routes(
                rcs, {"matrix_adapter": "matrix"}
            )

    def test_reversed_platform_order_expands_correctly(self) -> None:
        """Meshtastic source, Matrix dest (reversed order) with bidirectional
        produces both legs in the correct direction (lines 512-519)."""
        from medre.runtime.route_engine import build_runtime_routes

        rc = RouteConfig(
            route_id="rev_bidir",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            channel_room_map={"0": "!room0:example.com"},
        )
        rcs = RouteConfigSet(routes=(rc,))
        platforms = {"mesh_adapter": "meshtastic", "matrix_adapter": "matrix"}
        routes = build_runtime_routes(rcs, platforms)

        assert len(routes) == 2

        fwd = [r for r in routes if "meshtastic_to_matrix" in r.id]
        rev = [r for r in routes if "matrix_to_meshtastic" in r.id]
        assert len(fwd) == 1
        assert len(rev) == 1

        # Forward (source→dest = meshtastic→matrix): mesh source, matrix target
        assert fwd[0].source.adapter == "mesh_adapter"
        assert fwd[0].targets[0].adapter == "matrix_adapter"
        assert fwd[0].source.channel == "0"
        assert fwd[0].targets[0].channel == "!room0:example.com"

        # Reverse (matrix→meshtastic): matrix source, mesh target
        assert rev[0].source.adapter == "matrix_adapter"
        assert rev[0].targets[0].adapter == "mesh_adapter"
        assert rev[0].source.channel == "!room0:example.com"
        assert rev[0].targets[0].channel == "0"

    def test_policy_allowed_event_types_sets_event_kinds(self) -> None:
        """channel_room_map route with policy.allowed_event_types=["message"]
        produces routes with event_kinds=("message",) (lines 526-527)."""
        from medre.runtime.route_engine import build_runtime_routes
        from medre.config.routes import BridgePolicy

        policy = BridgePolicy(allowed_event_types=("message",))
        rc = RouteConfig(
            route_id="policy_crm",
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            channel_room_map={"0": "!room0:example.com"},
            policy=policy,
        )
        rcs = RouteConfigSet(routes=(rc,))
        routes = build_runtime_routes(rcs, self._platforms())
        assert len(routes) == 1
        assert routes[0].source.event_kinds == ("message",)
