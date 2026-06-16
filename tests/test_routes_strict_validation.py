"""Strict-validation tests for route and retry unknown-key rejection (F-014 / TC-011).

Moved from test_routes.py to keep that file under the line ceiling.
Also covers filter_hooks runtime rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.routes import RouteConfig


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


# ---------------------------------------------------------------------------
# filter_hooks runtime rejection
# ---------------------------------------------------------------------------


def test_non_empty_filter_hooks_rejected() -> None:
    """filter_hooks with entries raises ConfigValidationError (reserved)."""
    with pytest.raises(ConfigValidationError, match="filter_hooks.*reserved"):
        RouteConfig.from_dict(
            "bad",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "filter_hooks": ["my_hook"],
            },
        )


def test_non_list_filter_hooks_rejected() -> None:
    """filter_hooks that is not a list raises ConfigValidationError."""
    with pytest.raises(
        ConfigValidationError, match="'filter_hooks' must be a list"
    ):
        RouteConfig.from_dict(
            "bad",
            {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "filter_hooks": "not-a-list",
            },
        )
