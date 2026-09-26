"""Strict-validation tests for route and retry unknown-key rejection (F-014 / TC-011).

Moved from test_routes.py to keep that file under the line ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.route_expansion import expand_route_config
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteDestinationConfig,
    RouteDirectionality,
)

# ---------------------------------------------------------------------------
# Unknown route-level key rejection
# ---------------------------------------------------------------------------


def test_unknown_route_key_rejected() -> None:
    """Unknown keys at the route level are rejected, not silently dropped."""
    with pytest.raises(ConfigValidationError, match=r"unknown key\(s\)"):
        RouteConfig.from_dict(
            "bad",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "bogusextra": True,
            },
        )


def test_unknown_route_key_error_names_route_and_keys() -> None:
    """The error names the route id and lists the unknown key(s)."""
    with pytest.raises(ConfigValidationError) as exc_info:
        RouteConfig.from_dict(
            "typo_route",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "totally_unknown": 123,
            },
        )
    msg = str(exc_info.value)
    assert "Route 'typo_route'" in msg
    assert "'totally_unknown'" in msg
    assert exc_info.value.section_path == "routes.typo_route"


def test_unknown_route_key_rejected_alongside_known_fields() -> None:
    """Unknown keys are rejected even when all known fields are present."""
    with pytest.raises(ConfigValidationError, match=r"unknown key\(s\)"):
        RouteConfig.from_dict(
            "bad",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "directionality": "bidirectional",
                "enabled": True,
                "source_origin_label": "X",
                "dest_origin_label": "Y",
                "leftover_field": "should be caught",
            },
        )


def test_unknown_route_key_rejected_via_load_config(tmp_path: Path) -> None:
    """Unknown route-level keys are rejected through the full loader (TC-011)."""
    yaml_text = (
        "runtime:\n"
        "  name: bad_route_key\n"
        "routes:\n"
        "  bad:\n"
        "    source_adapters: [a]\n"
        "    dest_adapters: [b]\n"
        "    bogus_field: true\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ConfigValidationError, match=r"unknown key\(s\)"):
        load_config(str(p))


# ---------------------------------------------------------------------------
# Unknown retry key rejection
# ---------------------------------------------------------------------------


def test_unknown_retry_key_rejected() -> None:
    """Unknown keys in the retry section are rejected, not silently dropped."""
    with pytest.raises(ConfigValidationError, match="unknown retry key"):
        RouteConfig.from_dict(
            "test",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": True, "bogus_retry_field": 42},
            },
        )


def test_unknown_retry_key_error_names_route_and_path() -> None:
    """The unknown-retry-key error names the route and the retry section_path."""
    with pytest.raises(ConfigValidationError) as exc_info:
        RouteConfig.from_dict(
            "my_route",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": True, "bogus_retry_field": 42},
            },
        )
    msg = str(exc_info.value)
    assert "Route 'my_route'" in msg
    assert "'bogus_retry_field'" in msg
    assert exc_info.value.section_path == "routes.my_route.retry"


def test_unknown_retry_key_checked_before_field_coercion() -> None:
    """A typo'd retry field produces the unknown-key error, not a coercion one.

    Operators who write ``max_attempt`` (singular) instead of
    ``max_attempts`` must see "unknown retry key 'max_attempt'" rather
    than the confusing downstream "retry.max_attempts must be >0, got
    None" — the unknown-key rejection happens FIRST, before any field
    default-lookup or type-coercion.
    """
    with pytest.raises(ConfigValidationError) as exc_info:
        RouteConfig.from_dict(
            "typo_route",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"max_attempt": 3},
            },
        )
    msg = str(exc_info.value)
    assert "max_attempt" in msg
    assert "unknown retry key" in msg


# ---------------------------------------------------------------------------
# Removed legacy route keys are rejected as unknown
# ---------------------------------------------------------------------------


def test_removed_route_key_meshnet_name_rejected() -> None:
    """``meshnet_name`` as a route-level key is rejected as unknown."""
    with pytest.raises(ConfigValidationError, match=r"unknown key\(s\)") as exc_info:
        RouteConfig.from_dict(
            "migrate",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "meshnet_name": "old-style",
            },
        )
    msg = str(exc_info.value)
    assert "meshnet_name" in msg
    assert exc_info.value.section_path == "routes.migrate"


def test_direct_route_config_rejects_bare_context_map_entry() -> None:
    """Direct construction enforces the normalized structured map shape."""
    with pytest.raises(
        ConfigValidationError, match="direct construction requires ContextMapEntry"
    ):
        RouteConfig(
            route_id="direct-bare-map",
            source_adapters=("radio",),
            dest_adapters=("chat",),
            context_map={"0": "!room:example.org"},  # type: ignore[dict-item]
        )


def test_direct_route_config_rejects_unstripped_context_key() -> None:
    """Direct construction requires stripped/normalized context keys."""
    with pytest.raises(ConfigValidationError, match="stripped/normalized"):
        RouteConfig(
            route_id="direct-key-shape",
            source_adapters=("radio",),
            dest_adapters=("chat",),
            context_map={
                " 0": ContextMapEntry(dest_context="!room:example.org"),
            },
        )


def test_context_map_entry_validates_direct_construction() -> None:
    """Direct entry construction enforces the parsed context/label shape."""
    with pytest.raises(ConfigValidationError, match="exactly one of"):
        ContextMapEntry()
    with pytest.raises(ConfigValidationError, match="must be a non-empty string"):
        ContextMapEntry(dest_context="")
    with pytest.raises(
        ConfigValidationError, match="must already be stripped/normalized"
    ):
        ContextMapEntry(dest_context="  !room:example.org  ")
    with pytest.raises(ConfigValidationError, match="source_origin_label"):
        ContextMapEntry(
            dest_context="!room:example.org",
            source_origin_label=True,  # type: ignore[arg-type]
        )
    entry = ContextMapEntry(dest_context="!room:example.org")
    assert entry.dest_context == "!room:example.org"


def test_direct_route_config_rejects_empty_context_map() -> None:
    """Direct route construction matches parser rejection of an empty map."""
    with pytest.raises(ConfigValidationError, match="must not be empty"):
        RouteConfig(
            route_id="direct-empty-map",
            source_adapters=("radio",),
            dest_adapters=("chat",),
            context_map={},
        )


def test_expand_route_config_rejects_multi_adapter_context_map() -> None:
    """The compiler re-checks the one-source/one-dest fence even for a
    directly constructed route that mutated after config validation."""
    rc = RouteConfig(
        route_id="direct-fanout",
        source_adapters=("radio_adapter",),
        dest_adapters=("chat_adapter",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map={"a": ContextMapEntry(dest_context="!room:example.org")},
    )
    object.__setattr__(rc, "dest_adapters", ("chat_adapter", "chat_adapter_2"))

    with pytest.raises(
        ConfigValidationError,
        match="exactly one source adapter and one dest adapter",
    ):
        expand_route_config(rc)


def test_expand_route_config_rejects_expansion_token_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two source contexts may never share an expansion token. The SHA-256
    derivation makes natural collisions implausible, so the fence is pinned
    by forcing the token derivation and asserting the split-routes error."""
    rc = RouteConfig(
        route_id="direct-collide",
        source_adapters=("radio_adapter",),
        dest_adapters=("chat_adapter",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map={
            "a": ContextMapEntry(dest_context="!room-a:example.org"),
            "b": ContextMapEntry(dest_context="!room-b:example.org"),
        },
    )
    monkeypatch.setattr(
        "medre.config.route_expansion._context_map_token",
        lambda _ctx: "h_dead",
    )

    with pytest.raises(
        ConfigValidationError, match="collide on expansion token 'h_dead'"
    ):
        expand_route_config(rc)


def test_expand_route_config_rejects_context_map_channel_conflict() -> None:
    """The compiler re-checks context_map/channel mutual exclusion even for
    a directly constructed route that mutated after config validation."""
    rc = RouteConfig(
        route_id="direct-conflict",
        source_adapters=("radio_adapter",),
        dest_adapters=("chat_adapter",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map={
            "a": ContextMapEntry(
                dest_destination=RouteDestinationConfig(
                    kind="lxmf_destination",
                    destination_hash="21c0c1b9aabbccddeeff001122334455",
                )
            )
        },
    )
    object.__setattr__(rc, "dest_channel", "#leak")

    with pytest.raises(
        ConfigValidationError, match="context_map is mutually exclusive with"
    ):
        expand_route_config(rc)
