"""Regression: env override round-trip preserves ``context_map`` entries.

The round-trip in ``_build_route_data_from_env_fields`` (env.py) must
serialize normalized ``ContextMapEntry`` objects back to plain dicts —
including nested ``dest_destination`` tables — so that
``RouteConfig.from_dict`` can re-parse them after an env override is
applied.

These tests exercise the real parser path (``from_dict``), which normalizes
every structured entry to ``ContextMapEntry`` before the environment
override round-trip.
"""

from __future__ import annotations

import os

import pytest

from medre.config.env import apply_env_overrides
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_config_with_parsed_context_map() -> RuntimeConfig:
    """Build a RuntimeConfig whose route goes through ``from_dict``.

    Using the parser ensures ``context_map`` is normalized to
    ``dict[str, ContextMapEntry]`` — the shape that must survive the env
    round-trip. Covers all three entry shapes: labeled ``dest_context``,
    plain ``dest_context``, and nested ``dest_destination``.
    """
    route = RouteConfig.from_dict(
        "config-route",
        {
            "source_adapters": ["adapter-a"],
            "dest_adapters": ["adapter-b"],
            "directionality": "source_to_dest",
            "context_map": {
                "0": {
                    "dest_context": "!room1:matrix.org",
                    "source_origin_label": "Radio A",
                },
                "1": {"dest_context": "!room2:matrix.org"},
                "!room3:matrix.org": {
                    "dest_destination": {
                        "kind": "lxmf_destination",
                        "destination_hash": "21c0c1b9aabbccddeeff001122334455",
                        "destination_name": "bob",
                        "metadata": {"gate": "north"},
                    },
                },
            },
        },
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route,)),
    )


def test_env_override_preserves_parsed_context_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override on a route whose map is normalized ContextMapEntry.

    Regression: the round-trip must re-serialize the normalized
    ``dict[str, ContextMapEntry]`` (with nested destination tables)
    instead of feeding entry objects back into ``from_dict``.
    """
    monkeypatch.setenv("MEDRE_ROUTE__CONFIG_ROUTE__ENABLED", "false")
    base = _make_config_with_parsed_context_map()
    result = apply_env_overrides(base)

    assert len(result.routes.routes) == 1
    route = result.routes.routes[0]
    # The env override took effect.
    assert route.enabled is False
    # The context_map survived the round-trip with normalized entries.
    assert route.context_map is not None
    # Context "0": structured entry with a per-entry source_origin_label.
    entry0 = route.context_map["0"]
    assert isinstance(entry0, ContextMapEntry)
    assert entry0.dest_context == "!room1:matrix.org"
    assert entry0.source_origin_label == "Radio A"
    assert entry0.dest_origin_label is None
    # Context "1": plain dest_context-only entry.
    entry1 = route.context_map["1"]
    assert isinstance(entry1, ContextMapEntry)
    assert entry1.dest_context == "!room2:matrix.org"
    assert entry1.source_origin_label is None
    assert entry1.dest_origin_label is None
    # Context "!room3:...": nested structured destination survives intact.
    entry2 = route.context_map["!room3:matrix.org"]
    assert entry2.dest_context is None
    assert entry2.dest_destination is not None
    assert entry2.dest_destination.kind == "lxmf_destination"
    assert entry2.dest_destination.destination_hash == (
        "21c0c1b9aabbccddeeff001122334455"
    )
    assert entry2.dest_destination.destination_name == "bob"
    assert entry2.dest_destination.metadata == {"gate": "north"}
