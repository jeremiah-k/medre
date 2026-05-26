"""Shared helpers for MatrixAdapter tests.

Extracted from test_matrix_adapter.py so that test_matrix_adapter_startup.py
(and future splits) can import them without violating the no-cross-import rule.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import CanonicalEvent
from tests.helpers.matrix import to_event_dict  # noqa: F401 — re-export


def make_matrix_config(**overrides: Any) -> MatrixConfig:
    """Build a valid MatrixConfig for adapter tests."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-1",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def make_fake_nio_event(
    sender: str = "@alice:example.com",
    event_id: str = "$evt-001",
    body: str = "hello",
    content: dict | None = None,
) -> SimpleNamespace:
    """Build a minimal fake nio RoomMessageText event."""
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "content": content or {"msgtype": "m.text", "body": body},
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        },
    )


def make_fake_room(room_id: str = "!room:server") -> SimpleNamespace:
    """Build a minimal fake nio Room object."""
    return SimpleNamespace(room_id=room_id)


def make_adapter_context(
    adapter_id: str = "matrix-1",
) -> tuple[list[CanonicalEvent], AdapterContext]:
    """Create an AdapterContext that collects published events."""
    published: list[CanonicalEvent] = []

    async def _publish(event: CanonicalEvent) -> None:
        published.append(event)

    ctx = AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    return published, ctx


def make_fake_reaction_event(
    sender: str = "@alice:example.com",
    event_id: str = "$react-001",
    target_event_id: str = "$original-001",
    key: str = "\U0001f44d",
    content: dict | None = None,
) -> SimpleNamespace:
    """Build a minimal fake nio ReactionEvent (m.annotation)."""
    reaction_content = content or {
        "msgtype": "m.reaction",
        "body": key,
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target_event_id,
            "key": key,
        },
    }
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=key,
        source={
            "content": reaction_content,
            "event_id": event_id,
            "sender": sender,
            "type": "m.reaction",
        },
    )
