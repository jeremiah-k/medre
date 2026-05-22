"""Runtime-level multi-adapter integration test.

Builds a runtime from TOML config with three fake adapters (two Meshtastic,
one Matrix) and two unidirectional routes.  Verifies that:

1. RuntimeBuilder builds successfully with all three adapters.
2. Both Meshtastic adapters exist in ``app.adapters``.
3. Config for radio-a carries ``meshnet_name="RadioA"``.
4. Config for radio-b carries ``meshnet_name="RadioB"``.
5. radio-a and radio-b have independent configs (different meshnet_name,
   different default_channel).
6. Route resolution finds a route from radio-a to matrix-fake.
7. Route resolution finds a route from matrix-fake to radio-b.

All adapters are fake — no live hardware or network required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# TOML config template
# ---------------------------------------------------------------------------

_MULTI_ADAPTER_CONFIG = """\
[runtime]
name = "multi-adapter-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "memory"

[adapters.meshtastic.radio-a]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "RadioA"
default_channel = 0

[adapters.meshtastic.radio-b]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "RadioB"
default_channel = 1

[adapters.matrix.matrix-fake]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@fake:local"
access_token = "fake_token"

[routes.radio-a-to-matrix]
source_adapters = ["radio-a"]
dest_adapters = ["matrix-fake"]
directionality = "source_to_dest"
enabled = true

[routes.matrix-to-radio-b]
source_adapters = ["matrix-fake"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "multi_adapter.toml"
    config_path.write_text(_MULTI_ADAPTER_CONFIG)
    return config_path


def _make_stub_event(source_adapter: str) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for route matching.

    The router's ``match()`` checks ``source_adapter`` against each route's
    ``RouteSource.adapter``, so we only need that field populated.
    """
    from datetime import datetime, timezone
    from medre.core.events.canonical import EventMetadata
    import uuid

    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="fake",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={},
        metadata=EventMetadata(),
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestMeshtasticRuntimeMulti:
    """Multi-adapter runtime integration test with three fake adapters."""

    @pytest.mark.asyncio
    async def test_runtime_builds_three_adapters(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        config, _source, paths = load_config(str(config_path))

        # -- Assertion 1: RuntimeBuilder builds successfully --------------------
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert app is not None

        try:
            await app.start()

            # -- Assertion 2: Both Meshtastic adapters exist --------------------
            assert "radio-a" in app.adapters, (
                "radio-a not found in app.adapters; "
                f"available: {sorted(app.adapters.keys())}"
            )
            assert "radio-b" in app.adapters, (
                "radio-b not found in app.adapters; "
                f"available: {sorted(app.adapters.keys())}"
            )
            assert "matrix-fake" in app.adapters, (
                "matrix-fake not found in app.adapters; "
                f"available: {sorted(app.adapters.keys())}"
            )

            radio_a = app.adapters["radio-a"]
            radio_b = app.adapters["radio-b"]
            matrix_fake = app.adapters["matrix-fake"]

            # Verify adapter types.
            assert isinstance(radio_a, FakeMeshtasticAdapter)
            assert isinstance(radio_b, FakeMeshtasticAdapter)
            assert isinstance(matrix_fake, FakeMatrixAdapter)

            # -- Assertion 3: radio-a config meshnet_name == "RadioA" ----------
            cfg_a = config.adapters.meshtastic["radio-a"].config
            assert cfg_a is not None, "radio-a has no MeshtasticConfig"
            assert cfg_a.meshnet_name == "RadioA", (
                f"radio-a meshnet_name={cfg_a.meshnet_name!r}, expected 'RadioA'"
            )

            # -- Assertion 4: radio-b config meshnet_name == "RadioB" ----------
            cfg_b = config.adapters.meshtastic["radio-b"].config
            assert cfg_b is not None, "radio-b has no MeshtasticConfig"
            assert cfg_b.meshnet_name == "RadioB", (
                f"radio-b meshnet_name={cfg_b.meshnet_name!r}, expected 'RadioB'"
            )

            # -- Assertion 5: Independent configs (meshnet_name and channel) ---
            assert cfg_a.meshnet_name != cfg_b.meshnet_name, (
                f"meshnet_name should differ: "
                f"radio-a={cfg_a.meshnet_name!r}, radio-b={cfg_b.meshnet_name!r}"
            )
            assert cfg_a.default_channel != cfg_b.default_channel, (
                f"default_channel should differ: "
                f"radio-a={cfg_a.default_channel}, radio-b={cfg_b.default_channel}"
            )

            # -- Assertion 6: Route radio-a → matrix-fake exists ----------------
            event_from_a = _make_stub_event("radio-a")
            matched_a = app.router.match(event_from_a)
            assert len(matched_a) >= 1, (
                "No route matched for events from radio-a; "
                f"registered routes: {sorted(app.router._routes.keys())}"
            )
            route_a = matched_a[0]
            target_ids_a = [t.adapter for t in route_a.targets]
            assert "matrix-fake" in target_ids_a, (
                f"radio-a route targets {target_ids_a} do not include "
                f"'matrix-fake'"
            )

            # -- Assertion 7: Route matrix-fake → radio-b exists ---------------
            event_from_matrix = _make_stub_event("matrix-fake")
            matched_m = app.router.match(event_from_matrix)
            assert len(matched_m) >= 1, (
                "No route matched for events from matrix-fake; "
                f"registered routes: {sorted(app.router._routes.keys())}"
            )
            route_m = matched_m[0]
            target_ids_m = [t.adapter for t in route_m.targets]
            assert "radio-b" in target_ids_m, (
                f"matrix-fake route targets {target_ids_m} do not include "
                f"'radio-b'"
            )

        finally:
            try:
                await app.stop()
            except Exception:
                pass
