"""Matrix test helpers.

Provides duck-typed nio event/room builders, a mock nio module factory,
a ``mock_nio`` pytest fixture, and a MatrixConfig factory.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.config import MatrixConfig


def make_nio_event(
    sender: str = "@alice:example.com",
    event_id: str = "$bridge-evt-001",
    body: str = "hello from matrix",
    content: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build a duck-typed nio RoomMessageText event."""
    final_content = content or {"msgtype": "m.text", "body": body}
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "content": final_content,
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        },
    )


def make_nio_room(room_id: str = "!bridge_room:example.com") -> SimpleNamespace:
    """Build a duck-typed nio Room object."""
    return SimpleNamespace(room_id=room_id)


def build_mock_nio_module() -> MagicMock:
    """Create a mock nio module suitable for MatrixSession/MatrixAdapter.

    The returned mock exposes ``AsyncClient``, ``ClientConfig``, event types,
    and an ``nio.events`` submodule with ``MegolmEvent`` and
    ``RoomEncryptionEvent``.
    """
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.rooms = {}

    async def _sync_forever_stub(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    client.sync_forever = _sync_forever_stub

    async def _room_send(
        room_id: str, message_type: str, content: dict, **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            event_id=f"$sent-{content.get('body', 'msg')[:12]}",
            transport_response=None,
        )

    client.room_send = AsyncMock(side_effect=_room_send)

    whoami_resp = MagicMock(name="whoami_response")
    whoami_resp.device_id = "BRIDGE_MOCK_DEVICE"
    client.whoami = AsyncMock(return_value=whoami_resp)

    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")

    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events

    return mock


@pytest.fixture
def mock_nio() -> Any:
    """Inject a mock nio module into ``sys.modules`` and patch ``HAS_NIO``.

    Restores original modules (or removes injected ones) on teardown.
    """
    mock = build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


def make_matrix_config(**overrides: Any) -> MatrixConfig:
    """Build a valid MatrixConfig for bridge tests.

    Accepts keyword overrides merged on top of sensible defaults.
    """
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-bridge",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_bridge",
        "encryption_mode": "plaintext",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)
