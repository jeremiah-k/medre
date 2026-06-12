"""Runtime-level multi-adapter integration test.

Builds a runtime from TOML config with three fake adapters (two Meshtastic,
one Matrix) and two unidirectional routes.  Verifies that:

1. RuntimeBuilder builds successfully with all three adapters.
2. Both Meshtastic adapters exist in ``app.adapters``.
3. Config for radio-a carries ``origin_label="RadioA"``.
4. Config for radio-b carries ``origin_label="RadioB"``.
5. radio-a and radio-b have independent configs (different origin_label,
   different default_channel).
6. Route resolution finds a route from radio-a to matrix-fake.
7. Route resolution finds a route from matrix-fake to radio-b.
8. Matrix→radio-b render without target_channel uses radio-b config
   (default_channel, max_text_bytes, prefix).

All adapters are fake — no live hardware or network required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent, EventMetadata
from medre.core.events.metadata import NativeMetadata
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
origin_label = "RadioA"
default_channel = 0

[adapters.meshtastic.radio-b]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
origin_label = "RadioB"
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

# TOML config with custom radio-b settings for render verification.
_RENDER_VERIFY_CONFIG = """\
[runtime]
name = "render-verify-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "memory"

[adapters.meshtastic.radio-b]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
origin_label = "RenderNet"
default_channel = 3
max_text_bytes = 50
radio_relay_prefix = "[{origin_label}] "

[adapters.matrix.matrix-fake]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@fake:local"
access_token = "fake_token"
origin_label = "RenderNet"

[routes.matrix-to-radio-b]
source_adapters = ["matrix-fake"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, content: str = _MULTI_ADAPTER_CONFIG) -> Path:
    config_path = tmp_path / "multi_adapter.toml"
    config_path.write_text(content)
    return config_path


def _make_stub_event(source_adapter: str) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for route matching.

    The router's ``match()`` checks ``source_adapter`` against each route's
    ``RouteSource.adapter``, so we only need that field populated.
    """
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


def _make_matrix_event(
    body: str = "hello from matrix",
) -> CanonicalEvent:
    """Create a CanonicalEvent simulating Matrix origin with display metadata."""
    native_data: dict[str, object] = {
        "longname": "MatrixUser",
        "shortname": "MUser",
        "from_id": "@user:example.com",
    }
    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="matrix-fake",
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


# ---------------------------------------------------------------------------
# Tests
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

            # -- Assertion 3: radio-a config origin_label == "RadioA" ----------
            cfg_a = config.adapters.meshtastic["radio-a"].config
            assert cfg_a is not None, "radio-a has no MeshtasticConfig"
            assert (
                cfg_a.origin_label == "RadioA"
            ), f"radio-a origin_label={cfg_a.origin_label!r}, expected 'RadioA'"

            # -- Assertion 4: radio-b config origin_label == "RadioB" ----------
            cfg_b = config.adapters.meshtastic["radio-b"].config
            assert cfg_b is not None, "radio-b has no MeshtasticConfig"
            assert (
                cfg_b.origin_label == "RadioB"
            ), f"radio-b origin_label={cfg_b.origin_label!r}, expected 'RadioB'"

            # -- Assertion 5: Independent configs (origin_label and channel) ---
            assert cfg_a.origin_label != cfg_b.origin_label, (
                f"origin_label should differ: "
                f"radio-a={cfg_a.origin_label!r}, radio-b={cfg_b.origin_label!r}"
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
                f"radio-a route targets {target_ids_a} do not include " f"'matrix-fake'"
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
                f"matrix-fake route targets {target_ids_m} do not include " f"'radio-b'"
            )

        finally:
            try:
                await app.stop()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_render_to_radio_b_uses_config_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matrix→radio-b without target_channel uses radio-b config.

        Verifies that the rendering pipeline honours radio-b's
        ``default_channel``, ``max_text_bytes``, and ``radio_relay_prefix``
        when no explicit target_channel is provided.
        """
        # The config uses [{origin_label}] template syntax in
        # radio_relay_prefix.  Bypass _expand_paths_in_dict so the
        # template string survives config loading intact.
        monkeypatch.setattr(
            "medre.config.loader._expand_paths_in_dict",
            lambda d, _p: d,
        )
        config_path = _write_config(tmp_path, content=_RENDER_VERIFY_CONFIG)
        config, _source, paths = load_config(str(config_path))

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await app.start()

            # Verify radio-b config values before rendering.
            cfg_b = config.adapters.meshtastic["radio-b"].config
            assert cfg_b is not None
            assert cfg_b.default_channel == 3
            assert cfg_b.max_text_bytes == 50
            assert cfg_b.radio_relay_prefix == "[{origin_label}] "
            assert cfg_b.origin_label == "RenderNet"

            # Render a Matrix event targeting radio-b without target_channel.
            event = _make_matrix_event(body="test message content")
            result = await app.rendering_pipeline.render(
                event,
                "radio-b",
                target_platform="meshtastic",
            )

            # channel_index must come from radio-b's default_channel (3),
            # not hardcoded 0.
            assert result.payload["channel_index"] == 3, (
                f"Expected channel_index=3 (radio-b default_channel), "
                f"got {result.payload['channel_index']}"
            )

            # max_text_bytes (50) is enforced on the output.
            assert result.metadata["max_text_bytes"] == 50
            assert len(result.payload["text"].encode("utf-8")) <= 50

            # radio_relay_prefix template resolved with radio-b's origin_label.
            text = result.payload["text"]
            assert (
                "[RenderNet] " in text
            ), f"Expected '[RenderNet] ' prefix in text, got: {text!r}"

        finally:
            try:
                await app.stop()
            except Exception:
                pass
