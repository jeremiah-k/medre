"""Wait for specific provider event identities in an independently read timeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from tests.helpers.async_utils import wait_until


async def wait_for_body_events(
    fetch_messages: Callable[[], list[dict[str, Any]]],
    body: str,
    *,
    expected_event_ids: set[str],
    timeout: float = 20.0,
    interval: float = 0.5,
) -> list[dict[str, Any]]:
    """Require all expected identities, including a distinct replay event."""
    found: list[dict[str, Any]] = []

    async def poll() -> bool:
        nonlocal found
        try:
            chunk = await asyncio.to_thread(fetch_messages)
        except (OSError, json.JSONDecodeError):
            return False
        found = [
            event
            for event in chunk
            if event.get("type") == "m.room.message"
            and isinstance(event.get("content"), dict)
            and event["content"].get("body") == body
        ]
        observed_ids = {event.get("event_id") for event in found}
        return bool(found) and expected_event_ids <= observed_ids

    assert await wait_until(poll, timeout=timeout, interval=interval), (
        f"the Synapse timeline never showed expected events {expected_event_ids!r} "
        f"for body {body!r}; observed {[event.get('event_id') for event in found]!r}"
    )
    return found
