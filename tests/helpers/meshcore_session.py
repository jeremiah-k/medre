"""Shared mock helpers for MeshCore session tests.

Provides mock SDK types and module builder utilities used by
test_meshcore_session_startup.py and test_meshcore_session_recovery.py.
"""

from __future__ import annotations

import sys
from enum import Enum
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class MockEventType(Enum):
    """Minimal EventType subset used by session._subscribe_events."""

    CONTACT_MSG_RECV = "contact_message"
    CHANNEL_MSG_RECV = "channel_message"
    DISCONNECTED = "disconnected"
    MSG_SENT = "message_sent"
    OK = "command_ok"
    ERROR = "command_error"


class MockEvent:
    """Mimics meshcore.events.Event (type, payload, attributes, is_error)."""

    def __init__(
        self,
        event_type: MockEventType,
        payload: Any = None,
        attributes: dict | None = None,
    ) -> None:
        self.type = event_type
        self.payload = payload
        self.attributes = attributes or {}

    def is_error(self) -> bool:
        return self.type == MockEventType.ERROR


def build_mock_meshcore_module() -> tuple[MagicMock, AsyncMock]:
    """Build a mock ``meshcore`` module and return (module, meshcore_instance).

    The instance is what ``await MeshCore.create_tcp(...)`` (etc.) returns —
    it carries ``disconnect``, ``subscribe``, ``unsubscribe``,
    and ``commands.send_msg`` / ``commands.send_chan_msg``.
    """
    mock_mc = MagicMock()
    mock_mc.EventType = MockEventType

    # The MeshCore instance returned by factory methods.
    instance = AsyncMock()
    instance.disconnect = AsyncMock()
    instance.subscribe = MagicMock(return_value=MagicMock())
    instance.unsubscribe = MagicMock()
    instance.commands = AsyncMock()
    instance.commands.send_msg = AsyncMock()
    instance.commands.send_chan_msg = AsyncMock()
    instance.commands.send_appstart = AsyncMock(
        return_value=MockEvent(event_type=MockEventType.OK, payload={})
    )

    # Auto message fetching methods.
    instance.start_auto_message_fetching = AsyncMock()
    instance.stop_auto_message_fetching = AsyncMock()

    # Factory methods: MeshCore.create_tcp/create_serial/create_ble
    # These are async class methods that return the instance.
    mock_mc.MeshCore = MagicMock()
    mock_mc.MeshCore.create_tcp = AsyncMock(return_value=instance)
    mock_mc.MeshCore.create_serial = AsyncMock(return_value=instance)
    mock_mc.MeshCore.create_ble = AsyncMock(return_value=instance)

    return mock_mc, instance


def install_mock_module(mock_mc: MagicMock) -> None:
    """Insert mock meshcore module into sys.modules so deferred import finds it."""
    sys.modules["meshcore"] = mock_mc


def remove_mock_module() -> None:
    sys.modules.pop("meshcore", None)
