"""Live E2EE harness tests for Matrix adapter.

These tests require a real Matrix homeserver with E2EE-capable user
credentials.  They are **skipped by default** and only run when all
required environment variables are set:

  - MATRIX_HOMESERVER
  - MATRIX_USER_ID
  - MATRIX_ACCESS_TOKEN
  - MATRIX_ROOM_ID
  - MATRIX_DEVICE_ID
  - MATRIX_STORE_PATH

These tests do NOT pollute the standard test run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Skip entire module unless all env vars are set.
_REQUIRED_ENV = [
    "MATRIX_HOMESERVER",
    "MATRIX_USER_ID",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_ROOM_ID",
    "MATRIX_DEVICE_ID",
    "MATRIX_STORE_PATH",
]

_live_e2ee_ok = all(os.environ.get(v) for v in _REQUIRED_ENV)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _live_e2ee_ok,
        reason="Live E2EE tests require MATRIX_HOMESERVER, MATRIX_USER_ID, "
        "MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID, MATRIX_DEVICE_ID, "
        "MATRIX_STORE_PATH environment variables",
    ),
]


def _live_config(**overrides: Any):
    """Build a MatrixConfig from environment variables."""
    from medre.config.adapters.matrix import MatrixConfig

    defaults = {
        "adapter_id": "matrix-e2ee-live",
        "homeserver": os.environ["MATRIX_HOMESERVER"],
        "user_id": os.environ["MATRIX_USER_ID"],
        "access_token": os.environ["MATRIX_ACCESS_TOKEN"],
        "room_allowlist": {os.environ["MATRIX_ROOM_ID"]},
        "device_id": os.environ["MATRIX_DEVICE_ID"],
        "store_path": os.environ["MATRIX_STORE_PATH"],
        "encryption_mode": "e2ee_required",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


class TestLiveE2EEStart:
    """E2EE-required mode start with valid config."""

    async def test_e2ee_required_starts_with_valid_config(self) -> None:
        """Start in e2ee_required mode with real credentials and verify."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            await adapter.start(ctx)
            assert adapter._session is not None
            # crypto_enabled depends on whether vodozemac is installed
            diag = adapter.diagnostics()
            assert diag["connected"] is True
            assert diag["logged_in"] is True
        finally:
            await adapter.stop()


class TestLiveE2EESend:
    """Send encrypted text in E2EE mode."""

    async def test_send_encrypted_text(self) -> None:
        """Send a text message in an encrypted room."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            await adapter.start(ctx)
            room_id = os.environ["MATRIX_ROOM_ID"]
            result = RenderingResult(
                event_id="$live-test-event",
                target_adapter="matrix-e2ee-live",
                payload={"msgtype": "m.text", "body": "meshnet e2ee live test"},
                target_channel=room_id,
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
            native_message_id = deliver_result.native_message_id
            assert native_message_id, "expected non-empty event_id from delivery"
            native_channel_id = deliver_result.native_channel_id
            assert native_channel_id == os.environ["MATRIX_ROOM_ID"]
        finally:
            await adapter.stop()


class TestLiveE2EERestart:
    """Restart with same store/device and verify no catastrophic failure."""

    async def test_restart_same_store_device(self) -> None:
        """Stop and restart with same store_path/device_id."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            await adapter.start(ctx)
            assert adapter._session is not None
            await adapter.stop()

            # Restart with same config
            await adapter.start(ctx)
            assert adapter._session is not None
            diag = adapter.diagnostics()
            assert diag["connected"] is True
        finally:
            await adapter.stop()

    async def test_restart_preserves_crypto_state(self) -> None:
        """Restart preserves crypto_enabled and crypto_store_loaded."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee.restart"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            # First start
            await adapter.start(ctx)
            first_crypto_enabled = adapter.diagnostics()["crypto_enabled"]
            first_crypto_store = adapter.diagnostics()["crypto_store_loaded"]
            await adapter.stop()

            # Second start — same store/device
            await adapter.start(ctx)
            diag = adapter.diagnostics()
            assert diag["crypto_enabled"] == first_crypto_enabled
            assert diag["crypto_store_loaded"] == first_crypto_store
        finally:
            await adapter.stop()

    async def test_restart_send_encrypted(self) -> None:
        """After restart, can still send encrypted messages."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee.send"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            await adapter.start(ctx)
            await adapter.stop()

            # Restart and send
            await adapter.start(ctx)
            room_id = os.environ["MATRIX_ROOM_ID"]
            result = RenderingResult(
                event_id="$live-restart-event",
                target_adapter="matrix-e2ee-live",
                payload={"msgtype": "m.text", "body": "meshnet e2ee restart test"},
                target_channel=room_id,
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
        finally:
            await adapter.stop()


class TestLiveE2EEStartStopCycles:
    """Repeated start/stop cycles verify clean state."""

    async def test_repeated_start_stop_cycles(self) -> None:
        """2-3 start/stop cycles are safe with no leaked resources."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee.cycles"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        for cycle in range(3):
            await adapter.start(ctx)
            diag = adapter.diagnostics()
            assert diag["connected"] is True
            assert diag["sync_running"] is True
            assert diag["reconnecting"] is False
            assert diag["reconnect_attempts"] == 0
            await adapter.stop()
            assert adapter._session is None

    async def test_disconnect_reconnect(self) -> None:
        """Stop/restart adapter simulates disconnect/reconnect."""
        pytest.importorskip("nio")
        from medre.core.contracts.adapter import AdapterContext
        from medre.adapters.matrix.adapter import MatrixAdapter
        from datetime import datetime, timezone

        config = _live_config()
        adapter = MatrixAdapter(config)
        ctx = AdapterContext(
            adapter_id="matrix-e2ee-live",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.live.e2ee.disco"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        try:
            # Start → verify running
            await adapter.start(ctx)
            assert adapter.diagnostics()["connected"] is True

            # "Disconnect" — stop
            await adapter.stop()
            assert adapter._session is None

            # "Reconnect" — start again
            await adapter.start(ctx)
            diag = adapter.diagnostics()
            assert diag["connected"] is True
            assert diag["sync_running"] is True
            assert diag["crypto_enabled"] is True
        finally:
            await adapter.stop()
