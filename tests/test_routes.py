"""Tests for medre.config.routes: route model parsing, validation, ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
    RouteRetryConfig,
)
from medre.core.routing.router import Router

# ---------------------------------------------------------------------------
# BridgePolicy
# ---------------------------------------------------------------------------


class TestBridgePolicy:
    """BridgePolicy construction and defaults."""

    def test_defaults_all_empty_tuples(self) -> None:
        p = BridgePolicy()
        assert p.allowed_event_types == ()
        assert p.allowed_source_adapters == ()
        assert p.allowed_dest_adapters == ()
        assert p.room_allowlist == ()
        assert p.channel_allowlist == ()
        assert p.sender_allowlist == ()

    def test_from_toml_dict_full(self) -> None:
        data = {
            "allowed_event_types": ["message", "reaction"],
            "allowed_source_adapters": ["main"],
            "allowed_dest_adapters": ["radio"],
            "room_allowlist": ["!room:example.com"],
            "channel_allowlist": ["1", "2"],
            "sender_allowlist": ["@alice:example.com"],
        }
        p = BridgePolicy.from_toml_dict(data)
        assert p.allowed_event_types == ("message", "reaction")
        assert p.allowed_source_adapters == ("main",)
        assert p.allowed_dest_adapters == ("radio",)
        assert p.room_allowlist == ("!room:example.com",)
        assert p.channel_allowlist == ("1", "2")
        assert p.sender_allowlist == ("@alice:example.com",)

    def test_from_toml_dict_empty(self) -> None:
        p = BridgePolicy.from_toml_dict({})
        assert p == BridgePolicy()

    def test_frozen(self) -> None:
        p = BridgePolicy()
        with pytest.raises(AttributeError):
            p.allowed_event_types = ("message",)  # type: ignore[misc]

    def test_from_toml_dict_partial(self) -> None:
        data = {"allowed_event_types": ["message"]}
        p = BridgePolicy.from_toml_dict(data)
        assert p.allowed_event_types == ("message",)
        assert p.allowed_source_adapters == ()


# ---------------------------------------------------------------------------
# RouteConfig — valid construction
# ---------------------------------------------------------------------------


class TestRouteConfigValid:
    """RouteConfig construction from valid TOML data."""

    def test_minimal_route(self) -> None:
        data = {
            "source_adapters": ["main"],
            "dest_adapters": ["radio"],
        }
        r = RouteConfig.from_toml_dict("test_route", data)
        assert r.route_id == "test_route"
        assert r.source_adapters == ("main",)
        assert r.dest_adapters == ("radio",)
        assert r.directionality == RouteDirectionality.SOURCE_TO_DEST
        assert r.enabled is True
        assert r.filter_hooks == ()
        assert r.source_channel is None
        assert r.dest_channel is None
        assert r.source_room is None
        assert r.dest_room is None
        assert r.policy is None

    def test_full_route(self) -> None:
        data = {
            "source_adapters": ["main", "alt"],
            "dest_adapters": ["radio", "lxmf_local"],
            "directionality": "bidirectional",
            "enabled": False,
            "source_room": "!room_a:example.com",
            "dest_room": "!room_b:example.com",
            "policy": {
                "allowed_event_types": ["message"],
            },
        }
        r = RouteConfig.from_toml_dict("full_route", data)
        assert r.route_id == "full_route"
        assert r.source_adapters == ("main", "alt")
        assert r.dest_adapters == ("radio", "lxmf_local")
        assert r.directionality == RouteDirectionality.BIDIRECTIONAL
        assert r.enabled is False
        assert r.filter_hooks == ()
        # source_room aliases to source_channel when source_channel is absent
        assert r.source_channel == "!room_a:example.com"
        assert r.dest_channel == "!room_b:example.com"
        assert r.source_room == "!room_a:example.com"
        assert r.dest_room == "!room_b:example.com"
        assert r.policy is not None
        assert r.policy.allowed_event_types == ("message",)
        assert r.policy.sender_allowlist == ()

    def test_directionality_values(self) -> None:
        base: dict[str, object] = {"source_adapters": ["a"], "dest_adapters": ["b"]}
        for val in ("source_to_dest", "dest_to_source", "bidirectional"):
            base["directionality"] = val
            r = RouteConfig.from_toml_dict(f"route_{val}", base)
            assert r.directionality == RouteDirectionality(val)

    def test_route_id_with_hyphens_and_underscores(self) -> None:
        data = {"source_adapters": ["a"], "dest_adapters": ["b"]}
        r = RouteConfig.from_toml_dict("my-route_id-123", data)
        assert r.route_id == "my-route_id-123"

    def test_frozen(self) -> None:
        data = {"source_adapters": ["a"], "dest_adapters": ["b"]}
        r = RouteConfig.from_toml_dict("frozen_test", data)
        with pytest.raises(AttributeError):
            r.route_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RouteConfig — validation errors
# ---------------------------------------------------------------------------


class TestRouteConfigValidation:
    """RouteConfig raises ConfigValidationError for invalid input."""

    def test_missing_source_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="missing required 'source_adapters'"
        ):
            RouteConfig.from_toml_dict("bad", {"dest_adapters": ["b"]})

    def test_missing_dest_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="missing required 'dest_adapters'"
        ):
            RouteConfig.from_toml_dict("bad", {"source_adapters": ["a"]})

    def test_empty_source_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="'source_adapters' must not be empty"
        ):
            RouteConfig.from_toml_dict(
                "bad", {"source_adapters": [], "dest_adapters": ["b"]}
            )

    def test_empty_source_adapters_names_route(self) -> None:
        """Empty source_adapters error names the route."""
        with pytest.raises(ConfigValidationError, match="Route 'empty_src'"):
            RouteConfig.from_toml_dict(
                "empty_src", {"source_adapters": [], "dest_adapters": ["b"]}
            )

    def test_empty_dest_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="'dest_adapters' must not be empty"
        ):
            RouteConfig.from_toml_dict(
                "bad", {"source_adapters": ["a"], "dest_adapters": []}
            )

    def test_empty_dest_adapters_names_route(self) -> None:
        """Empty dest_adapters error names the route."""
        with pytest.raises(ConfigValidationError, match="Route 'empty_dst'"):
            RouteConfig.from_toml_dict(
                "empty_dst", {"source_adapters": ["a"], "dest_adapters": []}
            )

    def test_invalid_directionality(self) -> None:
        with pytest.raises(ConfigValidationError, match="invalid directionality"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "directionality": "invalid",
                },
            )

    def test_empty_route_id(self) -> None:
        with pytest.raises(ConfigValidationError, match="must not be empty"):
            RouteConfig.from_toml_dict(
                "", {"source_adapters": ["a"], "dest_adapters": ["b"]}
            )

    def test_invalid_route_id_spaces(self) -> None:
        with pytest.raises(ConfigValidationError, match="Invalid route ID"):
            RouteConfig.from_toml_dict(
                "bad id", {"source_adapters": ["a"], "dest_adapters": ["b"]}
            )

    def test_invalid_route_id_special_chars(self) -> None:
        with pytest.raises(ConfigValidationError, match="Invalid route ID"):
            RouteConfig.from_toml_dict(
                "bad@id!", {"source_adapters": ["a"], "dest_adapters": ["b"]}
            )

    def test_self_route_overlap(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="source and destination adapters overlap"
        ):
            RouteConfig.from_toml_dict(
                "self_route",
                {
                    "source_adapters": ["main", "alt"],
                    "dest_adapters": ["alt", "radio"],
                },
            )

    def test_duplicate_dest_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="duplicate entries in 'dest_adapters'"
        ):
            RouteConfig.from_toml_dict(
                "dup_dest",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b", "b"],
                },
            )

    def test_duplicate_source_adapters(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="duplicate entries in 'source_adapters'"
        ):
            RouteConfig.from_toml_dict(
                "dup_src",
                {
                    "source_adapters": ["a", "a"],
                    "dest_adapters": ["b"],
                },
            )

    def test_source_adapters_not_list(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="'source_adapters' must be a list"
        ):
            RouteConfig.from_toml_dict(
                "bad", {"source_adapters": "not_a_list", "dest_adapters": ["b"]}
            )

    def test_dest_adapters_not_list(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="'dest_adapters' must be a list"
        ):
            RouteConfig.from_toml_dict(
                "bad", {"source_adapters": ["a"], "dest_adapters": 42}
            )

    def test_policy_not_dict(self) -> None:
        with pytest.raises(ConfigValidationError, match="'policy' must be a table"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": "not_a_table",
                },
            )

    def test_filter_hooks_not_list(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="'filter_hooks' must be a list"
        ):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "filter_hooks": "not_a_list",
                },
            )

    # --- filter_hooks rejection (reserved/unsupported) ---

    def test_filter_hooks_nonempty_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="filter_hooks.*reserved"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "filter_hooks": ["spam_filter"],
                },
            )

    # --- room/channel aliasing ---

    def test_source_room_aliases_to_source_channel(self) -> None:
        r = RouteConfig.from_toml_dict(
            "alias",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "source_room": "!room:test",
            },
        )
        assert r.source_room == "!room:test"
        assert r.source_channel == "!room:test"

    def test_dest_room_aliases_to_dest_channel(self) -> None:
        r = RouteConfig.from_toml_dict(
            "alias",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "dest_room": "!room2:test",
            },
        )
        assert r.dest_room == "!room2:test"
        assert r.dest_channel == "!room2:test"

    def test_room_channel_same_value_ok(self) -> None:
        r = RouteConfig.from_toml_dict(
            "same",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "source_room": "!room:test",
                "source_channel": "!room:test",
            },
        )
        assert r.source_channel == "!room:test"
        assert r.source_room == "!room:test"

    def test_source_room_source_channel_conflict(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="source_room.*source_channel.*differ"
        ):
            RouteConfig.from_toml_dict(
                "conflict",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "source_room": "!room_a:test",
                    "source_channel": "ch-1",
                },
            )

    def test_source_room_source_channel_conflict_names_values(self) -> None:
        """Conflicting alias error names both fields and their values."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict(
                "conflict",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "source_room": "!room_a:test",
                    "source_channel": "ch-1",
                },
            )
        msg = str(exc_info.value)
        assert "'!room_a:test'" in msg
        assert "'ch-1'" in msg

    def test_dest_room_dest_channel_conflict(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="dest_room.*dest_channel.*differ"
        ):
            RouteConfig.from_toml_dict(
                "conflict",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "dest_room": "!room_b:test",
                    "dest_channel": "ch-2",
                },
            )

    def test_dest_room_dest_channel_conflict_names_values(self) -> None:
        """Conflicting alias error names both fields and their values."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict(
                "conflict",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "dest_room": "!room_b:test",
                    "dest_channel": "ch-2",
                },
            )
        msg = str(exc_info.value)
        assert "'!room_b:test'" in msg
        assert "'ch-2'" in msg

    # --- unsupported policy field rejection ---

    def test_sender_allowlist_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="sender_allowlist.*reserved"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"sender_allowlist": ["@alice:test"]},
                },
            )

    def test_unsupported_policy_fields_names_field(self) -> None:
        """Unsupported policy field error names the specific field."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"sender_allowlist": ["@alice:test"]},
                },
            )
        msg = str(exc_info.value)
        assert "sender_allowlist" in msg

    def test_unsupported_policy_fields_names_route(self) -> None:
        """Unsupported policy field error names the route."""
        with pytest.raises(ConfigValidationError, match="Route 'bad'"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"room_allowlist": ["!room:test"]},
                },
            )

    def test_allowed_source_adapters_rejected(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="allowed_source_adapters.*reserved"
        ):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"allowed_source_adapters": ["main"]},
                },
            )

    def test_allowed_dest_adapters_rejected(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="allowed_dest_adapters.*reserved"
        ):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"allowed_dest_adapters": ["radio"]},
                },
            )

    def test_room_allowlist_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="room_allowlist.*reserved"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"room_allowlist": ["!room:test"]},
                },
            )

    def test_channel_allowlist_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="channel_allowlist.*reserved"):
            RouteConfig.from_toml_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "policy": {"channel_allowlist": ["1", "2"]},
                },
            )

    def test_policy_allowed_event_types_still_supported(self) -> None:
        r = RouteConfig.from_toml_dict(
            "ok",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "policy": {"allowed_event_types": ["message"]},
            },
        )
        assert r.policy is not None
        assert r.policy.allowed_event_types == ("message",)


# ---------------------------------------------------------------------------
# RouteConfigSet — ordering and validation
# ---------------------------------------------------------------------------


class TestRouteConfigSet:
    """RouteConfigSet ordering, validation, and TOML parsing."""

    def test_empty_set(self) -> None:
        rs = RouteConfigSet.from_toml_dict({})
        assert rs.routes == ()

    def test_empty_routes_section(self) -> None:
        rs = RouteConfigSet.from_toml_dict({"routes": {}})
        assert rs.routes == ()

    def test_none_routes_section(self) -> None:
        rs = RouteConfigSet.from_toml_dict({"routes": None})
        assert rs.routes == ()

    def test_deterministic_ordering(self) -> None:
        """Routes are returned in TOML definition order."""
        data = {
            "routes": {
                "zebra": {"source_adapters": ["a"], "dest_adapters": ["b"]},
                "alpha": {"source_adapters": ["c"], "dest_adapters": ["d"]},
                "middle": {"source_adapters": ["e"], "dest_adapters": ["f"]},
            },
        }
        rs = RouteConfigSet.from_toml_dict(data)
        ids = [r.route_id for r in rs.routes]
        assert ids == ["zebra", "alpha", "middle"]

    def test_duplicate_route_ids(self) -> None:
        """Duplicate route IDs raise ConfigValidationError."""
        # First, create manually to test set-level validation
        r1 = RouteConfig.from_toml_dict(
            "dup", {"source_adapters": ["a"], "dest_adapters": ["b"]}
        )
        r2 = RouteConfig.from_toml_dict(
            "dup", {"source_adapters": ["c"], "dest_adapters": ["d"]}
        )
        rs = RouteConfigSet(routes=(r1, r2))
        with pytest.raises(ConfigValidationError, match="Duplicate route ID"):
            rs.validate()

    def test_non_table_route_section(self) -> None:
        """A route section that isn't a table raises an error."""
        data = {
            "routes": {
                "bad": "not_a_table",
            },
        }
        with pytest.raises(ConfigValidationError, match="must be a TOML table"):
            RouteConfigSet.from_toml_dict(data)

    def test_single_route(self) -> None:
        data = {
            "routes": {
                "r1": {"source_adapters": ["a"], "dest_adapters": ["b"]},
            },
        }
        rs = RouteConfigSet.from_toml_dict(data)
        assert len(rs.routes) == 1
        assert rs.routes[0].route_id == "r1"


# ---------------------------------------------------------------------------
# Integration: routes through TOML loader
# ---------------------------------------------------------------------------


ROUTES_TOML = """\
[runtime]
name = "test_routes"

[routes.matrix_to_radio]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:example.com"
dest_channel = "1"

[routes.radio_to_lxmf]
source_adapters = ["radio"]
dest_adapters = ["lxmf_local"]
directionality = "dest_to_source"
enabled = false

[routes.matrix_to_radio.policy]
allowed_event_types = ["message"]
"""

ROUTES_NO_ROUTES_TOML = """\
[runtime]
name = "no_routes"
"""


class TestRouteLoaderIntegration:
    """Routes are parsed correctly through load_config."""

    def test_routes_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_TOML)
        config, _, _ = load_config(str(p))
        assert len(config.routes.routes) == 2

    def test_route_ordering_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_TOML)
        config, _, _ = load_config(str(p))
        ids = [r.route_id for r in config.routes.routes]
        assert ids == ["matrix_to_radio", "radio_to_lxmf"]

    def test_first_route_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_TOML)
        config, _, _ = load_config(str(p))
        r = config.routes.routes[0]
        assert r.route_id == "matrix_to_radio"
        assert r.source_adapters == ("main",)
        assert r.dest_adapters == ("radio",)
        assert r.directionality == RouteDirectionality.SOURCE_TO_DEST
        assert r.enabled is True
        assert r.source_room == "!room:example.com"
        assert r.source_channel == "!room:example.com"  # aliased from source_room
        assert r.dest_channel == "1"
        assert r.policy is not None
        assert r.policy.allowed_event_types == ("message",)

    def test_second_route_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_TOML)
        config, _, _ = load_config(str(p))
        r = config.routes.routes[1]
        assert r.route_id == "radio_to_lxmf"
        assert r.directionality == RouteDirectionality.DEST_TO_SOURCE
        assert r.enabled is False
        assert r.filter_hooks == ()
        assert r.policy is None

    def test_no_routes_section(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_NO_ROUTES_TOML)
        config, _, _ = load_config(str(p))
        assert config.routes.routes == ()

    def test_existing_config_no_routes_unchanged(self, tmp_path: Path) -> None:
        """Existing configs without routes still load correctly."""
        minimal = "[runtime]\nname = 'legacy'\n"
        p = tmp_path / "config.toml"
        p.write_text(minimal)
        config, _, _ = load_config(str(p))
        assert config.routes.routes == ()
        assert config.runtime.name == "legacy"


# ---------------------------------------------------------------------------
# RouteRetryConfig — construction and validation
# ---------------------------------------------------------------------------


class TestRouteRetryConfig:
    """RouteRetryConfig parsing, defaults, and validation."""

    def test_defaults(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {},
            route_id="test",
            section_path="routes.test",
        )
        assert rc.enabled is True
        assert rc.max_attempts == 3
        assert rc.backoff_base == 2.0
        assert rc.max_delay_seconds == 60.0
        assert rc.jitter is False

    def test_full_config(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {
                "enabled": True,
                "max_attempts": 5,
                "backoff_base": 1.5,
                "max_delay_seconds": 120.0,
                "jitter": True,
            },
            route_id="test",
            section_path="routes.test",
        )
        assert rc.enabled is True
        assert rc.max_attempts == 5
        assert rc.backoff_base == 1.5
        assert rc.max_delay_seconds == 120.0
        assert rc.jitter is True

    def test_disabled(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {"enabled": False},
            route_id="test",
            section_path="routes.test",
        )
        assert rc.enabled is False

    def test_frozen(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {},
            route_id="test",
            section_path="routes.test",
        )
        with pytest.raises(AttributeError):
            rc.enabled = False  # type: ignore[misc]

    # --- validation errors ---

    def test_enabled_not_bool(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.enabled must be a boolean"
        ):
            RouteRetryConfig.from_toml_dict(
                {"enabled": "yes"},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_attempts_not_int(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_attempts must be an integer"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_attempts": 3.5},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_attempts_bool_rejected(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_attempts must be an integer"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_attempts": True},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_attempts_zero(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_attempts must be > 0"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_attempts": 0},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_attempts_negative(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_attempts must be > 0"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_attempts": -1},
                route_id="test",
                section_path="routes.test",
            )

    def test_backoff_base_negative(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.backoff_base must be >= 0"
        ):
            RouteRetryConfig.from_toml_dict(
                {"backoff_base": -0.1},
                route_id="test",
                section_path="routes.test",
            )

    def test_backoff_base_not_number(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.backoff_base must be a number"
        ):
            RouteRetryConfig.from_toml_dict(
                {"backoff_base": "fast"},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_delay_seconds_negative(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_delay_seconds must be >= 0"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_delay_seconds": -1.0},
                route_id="test",
                section_path="routes.test",
            )

    def test_max_delay_seconds_not_number(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_delay_seconds must be a number"
        ):
            RouteRetryConfig.from_toml_dict(
                {"max_delay_seconds": "forever"},
                route_id="test",
                section_path="routes.test",
            )

    def test_jitter_not_bool(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.jitter must be a boolean"
        ):
            RouteRetryConfig.from_toml_dict(
                {"jitter": "yes"},
                route_id="test",
                section_path="routes.test",
            )

    def test_zero_backoff_base_ok(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {"backoff_base": 0},
            route_id="test",
            section_path="routes.test",
        )
        assert rc.backoff_base == 0.0

    def test_zero_max_delay_ok(self) -> None:
        rc = RouteRetryConfig.from_toml_dict(
            {"max_delay_seconds": 0},
            route_id="test",
            section_path="routes.test",
        )
        assert rc.max_delay_seconds == 0.0


# ---------------------------------------------------------------------------
# RouteConfig — retry field integration
# ---------------------------------------------------------------------------


class TestRouteConfigRetry:
    """RouteConfig correctly parses and validates the retry field."""

    def test_no_retry_defaults_none(self) -> None:
        r = RouteConfig.from_toml_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
            },
        )
        assert r.retry is None

    def test_retry_table_parsed(self) -> None:
        r = RouteConfig.from_toml_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {
                    "enabled": True,
                    "max_attempts": 3,
                    "backoff_base": 2.0,
                    "max_delay_seconds": 60.0,
                    "jitter": False,
                },
            },
        )
        assert r.retry is not None
        assert r.retry.enabled is True
        assert r.retry.max_attempts == 3
        assert r.retry.backoff_base == 2.0
        assert r.retry.max_delay_seconds == 60.0
        assert r.retry.jitter is False

    def test_retry_defaults_only_enabled(self) -> None:
        r = RouteConfig.from_toml_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": True},
            },
        )
        assert r.retry is not None
        assert r.retry.enabled is True
        assert r.retry.max_attempts == 3  # default

    def test_retry_disabled(self) -> None:
        r = RouteConfig.from_toml_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": False},
            },
        )
        assert r.retry is not None
        assert r.retry.enabled is False

    def test_retry_not_table_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="'retry' must be a table"):
            RouteConfig.from_toml_dict(
                "test",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "retry": "not_a_table",
                },
            )

    def test_retry_invalid_max_attempts_propagates(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="retry.max_attempts must be > 0"
        ):
            RouteConfig.from_toml_dict(
                "test",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "retry": {"max_attempts": 0},
                },
            )

    def test_retry_with_policy(self) -> None:
        """Retry and policy can coexist on a route."""
        r = RouteConfig.from_toml_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "policy": {"allowed_event_types": ["message"]},
                "retry": {"enabled": True, "max_attempts": 5},
            },
        )
        assert r.policy is not None
        assert r.retry is not None
        assert r.retry.max_attempts == 5

    def test_retry_preserves_route_id_in_errors(self) -> None:
        """Retry validation errors include the route ID."""
        with pytest.raises(ConfigValidationError, match="Route 'my_route'"):
            RouteConfig.from_toml_dict(
                "my_route",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "retry": {"max_attempts": -1},
                },
            )

    def test_retry_section_path_in_errors(self) -> None:
        """Retry validation errors include the section_path."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict(
                "my_route",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b"],
                    "retry": {"max_attempts": -1},
                },
            )
        assert exc_info.value.section_path == "routes.my_route.retry"


# ---------------------------------------------------------------------------
# Route retry TOML loader integration
# ---------------------------------------------------------------------------


ROUTES_WITH_RETRY_TOML = """\
[runtime]
name = "test_retry_routes"

[routes.matrix_to_mesh]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"

[routes.matrix_to_mesh.retry]
enabled = true
max_attempts = 3
backoff_base = 2.0
max_delay_seconds = 60.0
jitter = false

[routes.no_retry_route]
source_adapters = ["main"]
dest_adapters = ["lxmf"]
"""

ROUTES_WITH_DISABLED_RETRY_TOML = """\
[runtime]
name = "disabled_retry"

[routes.disabled_retry]
source_adapters = ["a"]
dest_adapters = ["b"]

[routes.disabled_retry.retry]
enabled = false
max_attempts = 5
"""


class TestRouteRetryLoaderIntegration:
    """Route retry config is parsed correctly through load_config."""

    def test_retry_parsed_via_loader(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_WITH_RETRY_TOML)
        config, _, _ = load_config(str(p))
        assert len(config.routes.routes) == 2

        retry_route = config.routes.routes[0]
        assert retry_route.route_id == "matrix_to_mesh"
        assert retry_route.retry is not None
        assert retry_route.retry.enabled is True
        assert retry_route.retry.max_attempts == 3
        assert retry_route.retry.backoff_base == 2.0
        assert retry_route.retry.max_delay_seconds == 60.0
        assert retry_route.retry.jitter is False

    def test_no_retry_route_still_none(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_WITH_RETRY_TOML)
        config, _, _ = load_config(str(p))
        no_retry = config.routes.routes[1]
        assert no_retry.route_id == "no_retry_route"
        assert no_retry.retry is None

    def test_disabled_retry_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(ROUTES_WITH_DISABLED_RETRY_TOML)
        config, _, _ = load_config(str(p))
        route = config.routes.routes[0]
        assert route.retry is not None
        assert route.retry.enabled is False
        assert route.retry.max_attempts == 5


# ---------------------------------------------------------------------------
# RouteDirectionality enum
# ---------------------------------------------------------------------------


class TestRouteDirectionality:
    """RouteDirectionality enum values."""

    def test_values(self) -> None:
        assert RouteDirectionality.SOURCE_TO_DEST.value == "source_to_dest"
        assert RouteDirectionality.DEST_TO_SOURCE.value == "dest_to_source"
        assert RouteDirectionality.BIDIRECTIONAL.value == "bidirectional"

    def test_from_string(self) -> None:
        assert (
            RouteDirectionality("source_to_dest") is RouteDirectionality.SOURCE_TO_DEST
        )
        assert (
            RouteDirectionality("dest_to_source") is RouteDirectionality.DEST_TO_SOURCE
        )
        assert RouteDirectionality("bidirectional") is RouteDirectionality.BIDIRECTIONAL
