"""Regression: env override round-trip preserves ``channel_room_map`` entries.

The round-trip in ``_build_route_toml_data_from_env_fields`` (env.py) must
serialize normalized ``ChannelRoomMapEntry`` objects back to plain dicts so
that ``RouteConfig.from_toml_dict`` can re-parse them after an env override
is applied.

The existing ``test_channel_room_map_preserved_on_override`` in
``test_config_env_first.py`` constructs ``RouteConfig`` directly with a bare
``dict[str, str]`` channel_room_map. That bypasses ``from_toml_dict``
normalization, so ``existing.channel_room_map`` stays as ``dict[str, str]``
and the round-trip works even though the production parser path is broken.
These tests exercise the real parser path (``from_toml_dict``), which
normalizes every entry to ``ChannelRoomMapEntry`` — the shape that triggered
the regression.
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
    """Build a RuntimeConfig whose route goes through ``from_toml_dict``.

    Using the parser ensures ``channel_room_map`` is normalized to
    ``dict[str, ChannelRoomMapEntry]`` — the shape that broke the env
    round-trip before the fix. Both a structured entry (with labels) and a
    legacy bare-string entry are included so the regression covers both
    shapes after normalization.
    """
    route = RouteConfig.from_toml_dict(
        "toml-route",
        {
            "source_adapters": ["adapter-a"],
            "dest_adapters": ["adapter-b"],
            "directionality": "source_to_dest",
            "channel_room_map": {
                "0": {
                    "room": "!room1:matrix.org",
                    "source_origin_label": "Radio A",
                },
                "1": "!room2:matrix.org",
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
    ``dict[str, ChannelRoomMapEntry]`` straight into ``toml_data``, and the
    re-parse rejected the entry objects (they are neither ``str`` nor
    ``dict``).
    """
    monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")
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
    # Channel 1: legacy bare-string entry, normalized to a label-less entry.
    entry1 = route.channel_room_map["1"]
    assert isinstance(entry1, ChannelRoomMapEntry)
    assert entry1.room == "!room2:matrix.org"
    assert entry1.source_origin_label is None
    assert entry1.dest_origin_label is None
