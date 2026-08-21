"""Regression: env override round-trip preserves ``channel_room_map`` entries.

The round-trip in ``_build_route_data_from_env_fields`` (env.py) must
serialize normalized ``ChannelRoomMapEntry`` objects back to plain dicts so
that ``RouteConfig.from_dict`` can re-parse them after an env override
is applied.

These tests exercise the real parser path (``from_dict``), which normalizes
every structured entry to ``ChannelRoomMapEntry`` before the environment
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
    ChannelRoomMapEntry,
    RouteConfig,
    RouteConfigSet,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_config_with_parsed_channel_room_map() -> RuntimeConfig:
    """Build a RuntimeConfig whose route goes through ``from_dict``.

    Using the parser ensures ``channel_room_map`` is normalized to
    ``dict[str, ChannelRoomMapEntry]`` — the shape that broke the env
    round-trip. Both a labeled and an unlabeled structured entry are included.
    """
    route = RouteConfig.from_dict(
        "config-route",
        {
            "source_adapters": ["adapter-a"],
            "dest_adapters": ["adapter-b"],
            "directionality": "source_to_dest",
            "channel_room_map": {
                "0": {
                    "room": "!room1:matrix.org",
                    "source_origin_label": "Radio A",
                },
                "1": {"room": "!room2:matrix.org"},
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


def test_env_override_preserves_parsed_channel_room_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override on a route whose map is normalized ChannelRoomMapEntry.

    Regression: previously the round-trip copied the normalized
    ``dict[str, ChannelRoomMapEntry]`` straight into ``route_data``, and the
    re-parse rejected the normalized entry objects instead of serializing
    them back to plain dictionaries.
    """
    monkeypatch.setenv("MEDRE_ROUTE__CONFIG_ROUTE__ENABLED", "false")
    base = _make_config_with_parsed_channel_room_map()
    result = apply_env_overrides(base)

    assert len(result.routes.routes) == 1
    route = result.routes.routes[0]
    # The env override took effect.
    assert route.enabled is False
    # The channel_room_map survived the round-trip with normalized entries.
    assert route.channel_room_map is not None
    # Channel 0: structured entry with a per-entry source_origin_label.
    entry0 = route.channel_room_map["0"]
    assert isinstance(entry0, ChannelRoomMapEntry)
    assert entry0.room == "!room1:matrix.org"
    assert entry0.source_origin_label == "Radio A"
    assert entry0.dest_origin_label is None
    # Channel 1: structured room-only entry.
    entry1 = route.channel_room_map["1"]
    assert isinstance(entry1, ChannelRoomMapEntry)
    assert entry1.room == "!room2:matrix.org"
    assert entry1.source_origin_label is None
    assert entry1.dest_origin_label is None
