"""Finite live-peer budgets shared by the parent harness and LXMF child."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Collection
from typing import Any

PEER_STARTUP_SECONDS = 40.0
PEER_RECALL_SECONDS = 45.0
PEER_PACING_SECONDS = 2.5
PEER_EXIT_SECONDS = 10.0


def delivery_timeout_seconds() -> float:
    """Read the per-message ceiling, independently of SDK retry intervals.

    Slow Reticulum media can require more than the bench's 90-second window.
    Operators can enlarge it with LXMF_PEER_DELIVERY_TIMEOUT_SECONDS; all
    surrounding subprocess and observation budgets derive from this value.
    """
    value = float(os.environ.get("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS", "90"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS must be finite and > 0")
    return value


def send_timeout_seconds(message_count: int = 1) -> float:
    """Bound startup, identity recall, each delivery/pacing, and process exit."""
    if isinstance(message_count, bool) or not isinstance(message_count, int):
        raise ValueError("message_count must be a positive integer")
    if message_count < 1:
        raise ValueError("message_count must be a positive integer")
    return (
        PEER_STARTUP_SECONDS
        + PEER_RECALL_SECONDS
        + message_count * (delivery_timeout_seconds() + PEER_PACING_SECONDS)
        + PEER_EXIT_SECONDS
    )


def wait_terminal(
    message: Any, terminal_states: Collection[int], timeout: float
) -> int:
    """Observe a terminal state within a finite, nonnegative time budget."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and >= 0")
    deadline = time.monotonic() + timeout
    while True:
        state = message.state
        if state in terminal_states:
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        time.sleep(min(0.25, remaining))
