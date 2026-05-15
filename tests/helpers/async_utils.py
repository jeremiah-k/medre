"""Async testing utilities.

Provides polling helpers that replace ad-hoc ``asyncio.sleep`` calls
in tests with deterministic condition-based waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


async def wait_until(
    condition: Callable[[], Any],
    timeout: float = 5.0,
    interval: float = 0.05,
) -> bool:
    """Poll *condition* until it returns truthy or *timeout* expires.

    Supports both sync and async callables: if ``condition()`` returns a
    coroutine, it is awaited automatically.

    Returns ``True`` if the condition was met within *timeout*, ``False``
    otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(interval, remaining))
