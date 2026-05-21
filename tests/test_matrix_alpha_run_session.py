"""Opt-in Matrix alpha run-session live test.

Exercises the run_session orchestration path with a real Matrix adapter
config derived from environment variables.  This test validates the full
config→build→start→health→send→stop→evidence pipeline against a live
Matrix homeserver.

**Opt-in only.** Skipped unless all four required MATRIX_* env vars are set.
Also tagged with the ``live`` marker so it is excluded from default pytest
runs.

This test does NOT test Meshtastic, MeshCore, LXMF, or any non-Matrix
connectivity.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-level marker
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Environment gate
# ---------------------------------------------------------------------------
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.environ.get("MATRIX_USER_ID")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

_LIVE_ENV_SET = all(
    [MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID]
)

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=(
        "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
        "and MATRIX_ROOM_ID env vars to run Matrix alpha run-session tests"
    ),
)

# Timeout bounds for live operations (seconds).
_START_TIMEOUT: float = 30.0
_STOP_TIMEOUT: float = 10.0
_DELIVER_TIMEOUT: float = 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_matrix_config(tmp_path: Path) -> str:
    """Write a TOML config with real Matrix adapter + fake Meshtastic."""
    assert MATRIX_HOMESERVER and MATRIX_USER_ID and MATRIX_ACCESS_TOKEN
    assert MATRIX_ROOM_ID

    config_content = f"""\
[runtime]
name = "matrix-alpha-run-session"
shutdown_timeout_seconds = 10

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{tmp_path / "alpha-session.db"}"

[adapters.matrix.matrix_alpha]
enabled = true
adapter_kind = "real"
homeserver = "{MATRIX_HOMESERVER}"
user_id = "{MATRIX_USER_ID}"
access_token = "{MATRIX_ACCESS_TOKEN}"
room_allowlist = ["{MATRIX_ROOM_ID}"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "AlphaTest"

[routes.alpha_bridge]
source_adapters = ["matrix_alpha"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = "{MATRIX_ROOM_ID}"
dest_channel = "1"
"""
    config_file = tmp_path / "alpha-config.toml"
    config_file.write_text(config_content)
    return str(config_file)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@require_live
class TestMatrixAlphaRunSession:
    """Matrix alpha run-session live tests.

    These tests exercise the full orchestration pipeline with a real Matrix
    adapter: config loading, runtime building, adapter startup, health
    verification, message delivery, and graceful shutdown.

    Requires all four MATRIX_* environment variables.
    """

    async def test_alpha_session_config_builds_and_starts(self, tmp_path) -> None:
        """Config with adapter_kind='real' for Matrix builds and starts."""
        from medre.config.env import apply_env_overrides
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder

        config_path = _write_matrix_config(tmp_path)
        config, source, paths = load_config(config_path)
        config = apply_env_overrides(config, paths)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert (
            len(app.adapters) > 0
        ), f"No adapters built. Build failures: {app.build_failures}"
        assert (
            "matrix_alpha" in app.adapters
        ), f"Matrix adapter not built. Available: {list(app.adapters.keys())}"

        try:
            await asyncio.wait_for(app.start(), timeout=_START_TIMEOUT)

            # Verify adapter is healthy
            adapter = app.adapters["matrix_alpha"]
            info = await adapter.health_check()
            assert (
                info.health == "healthy"
            ), f"Matrix adapter not healthy after start: {info.health!r}"
        finally:
            await asyncio.wait_for(app.stop(), timeout=_STOP_TIMEOUT)

    async def test_alpha_session_send_and_verify_event_id(self, tmp_path) -> None:
        """Send a message through the real Matrix adapter and verify event_id."""
        import time
        from unittest.mock import AsyncMock

        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        assert MATRIX_HOMESERVER is not None
        assert MATRIX_USER_ID is not None
        assert MATRIX_ACCESS_TOKEN is not None
        assert MATRIX_ROOM_ID is not None

        config = MatrixConfig(
            adapter_id="matrix-alpha-session",
            homeserver=MATRIX_HOMESERVER,
            user_id=MATRIX_USER_ID,
            access_token=MATRIX_ACCESS_TOKEN,
            room_allowlist={MATRIX_ROOM_ID},
        )

        ctx = AdapterContext(
            adapter_id="matrix-alpha-session",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.alpha-session"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        adapter = MatrixAdapter(config)
        await asyncio.wait_for(adapter.start(ctx), timeout=_START_TIMEOUT)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"alpha-session-{ts}",
                target_adapter="matrix-alpha-session",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE alpha session test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "alpha-session"},
            )
            delivery = await asyncio.wait_for(
                adapter.deliver(result), timeout=_DELIVER_TIMEOUT
            )
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, "native_message_id is None"
            assert delivery.native_message_id.startswith(
                "$"
            ), f"Expected event_id starting with '$', got {delivery.native_message_id!r}"
            assert delivery.native_channel_id == MATRIX_ROOM_ID
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_STOP_TIMEOUT)

    async def test_alpha_session_diagnostics_after_send(self, tmp_path) -> None:
        """Verify Matrix adapter diagnostics are complete after a send operation."""
        import time
        from unittest.mock import AsyncMock

        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        assert MATRIX_HOMESERVER is not None
        assert MATRIX_USER_ID is not None
        assert MATRIX_ACCESS_TOKEN is not None

        config = MatrixConfig(
            adapter_id="matrix-alpha-diag",
            homeserver=MATRIX_HOMESERVER,
            user_id=MATRIX_USER_ID,
            access_token=MATRIX_ACCESS_TOKEN,
        )

        ctx = AdapterContext(
            adapter_id="matrix-alpha-diag",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.alpha-diag"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        adapter = MatrixAdapter(config)
        await asyncio.wait_for(adapter.start(ctx), timeout=_START_TIMEOUT)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"alpha-diag-{ts}",
                target_adapter="matrix-alpha-diag",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE alpha diag test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "alpha-diag"},
            )
            await asyncio.wait_for(adapter.deliver(result), timeout=_DELIVER_TIMEOUT)

            diag = adapter.diagnostics()

            # Verify connected state
            assert diag["connected"] is True
            assert diag["logged_in"] is True
            assert diag["sync_task_running"] is True

            # Verify no secrets
            diag_str = str(diag)
            assert MATRIX_ACCESS_TOKEN is not None
            assert MATRIX_ACCESS_TOKEN not in diag_str

            # Verify delivery counters are integers
            assert isinstance(diag["transient_delivery_failures"], int)
            assert isinstance(diag["permanent_delivery_failures"], int)
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_STOP_TIMEOUT)
