"""Shared helpers for Matrix session tests.

Provides mock nio module injection, config/context builders, and sleep patches.
No live Matrix/nio dependencies are imported.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_matrix_config(**overrides: Any) -> MatrixConfig:
    """Build a MatrixConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_123",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def make_matrix_context(adapter_id: str = "matrix-test") -> AdapterContext:
    """Build an AdapterContext with minimal fakes."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def sync_forever_stub(*args: object, **kwargs: object) -> None:
    """Stub for sync_forever — blocks until cancelled."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


def fast_sleep_patch():
    """Return a mock sleep that yields for delay=0 but skips backoff delays.

    Usage::

        with fast_sleep_patch():
            await session.start()
            for _ in range(N):
                await asyncio.sleep(0)
    """
    original_sleep = asyncio.sleep

    async def _fast_sleep(delay: float) -> None:
        if delay <= 0:
            await original_sleep(0)
        # positive delays are skipped (instant backoff for tests)

    return patch("asyncio.sleep", side_effect=_fast_sleep)


def build_mock_nio_module() -> MagicMock:
    """Create a mock nio module with AsyncClient and message types."""
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.sync_forever = sync_forever_stub
    client.room_send = AsyncMock()
    client.rooms = {}
    # whoami() returns a response with device_id for device discovery.
    whoami_resp = MagicMock(name="whoami_response")
    whoami_resp.device_id = "MOCK_DISCOVERED_DEVICE"
    client.whoami = AsyncMock(return_value=whoami_resp)
    client.join = AsyncMock()
    join_resp = MagicMock(name="join_response")
    join_resp.room_id = "!auto:server"
    client.join.return_value = join_resp
    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")
    mock.ReactionEvent = MagicMock(name="ReactionEvent")
    mock.InviteMemberEvent = MagicMock(name="InviteMemberEvent")
    # nio.events.MegolmEvent for undecryptable event callback
    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock_events.InviteMemberEvent = MagicMock(name="InviteMemberEvent")
    # Explicitly provide room_events submodule WITHOUT ReactionEvent
    # so auto-created MagicMock doesn't create a false match.
    mock_room_events = MagicMock(name="nio.events.room_events")
    del mock_room_events.ReactionEvent
    mock_events.room_events = mock_room_events
    mock.events = mock_events
    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module into sys.modules and patch HAS_NIO."""
    mock = build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    # Restore
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events
