"""Shared MeshCore live-runtime launch policy for physical test harnesses."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Protocol

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.live_harness import bounded

pytestmark = [pytest.mark.live, pytest.mark.hardware]


class _HealthInfo(Protocol):
    health: str


class _HealthAdapter(Protocol):
    async def health_check(self) -> _HealthInfo: ...


class MeshCoreRuntime(Protocol):
    adapters: Mapping[str, _HealthAdapter]

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


async def launch_healthy_meshcore_runtime(
    build_runtime: Callable[[], MeshCoreRuntime],
    *,
    start_timeout: float,
    start_label: str,
    stop_timeout: float,
    stop_label: str,
    adapter_id: str = "mc_radio",
    health_timeout: float = 20.0,
    health_check_timeout: float = 15.0,
    health_label: str = "mc_radio health_check",
    attempts: int = 2,
) -> MeshCoreRuntime:
    """Start a fresh runtime until its MeshCore adapter reports healthy."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error = "no launch attempt completed"
    for attempt in range(1, attempts + 1):
        app = build_runtime()
        try:
            await bounded(app.start(), start_timeout, start_label)
            deadline = time.monotonic() + health_timeout
            last_health: str | None = None

            async def _healthy(
                deadline: float = deadline,
                app: MeshCoreRuntime = app,
            ) -> bool:
                nonlocal last_health
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                info = await bounded(
                    app.adapters[adapter_id].health_check(),
                    min(health_check_timeout, remaining),
                    health_label,
                )
                last_health = info.health
                return last_health == "healthy"

            if await wait_until(_healthy, timeout=health_timeout, interval=1.0):
                return app
            last_error = f"health stayed {last_health!r}"
        except Exception as exc:
            last_error = f"launch attempt {attempt} failed: {exc}"

        try:
            await bounded(app.stop(), stop_timeout, stop_label)
        except Exception as cleanup_exc:
            last_error = f"{last_error}; cleanup failed: {cleanup_exc}"

        # ``app.stop()`` is the deterministic ownership boundary for the
        # failed attempt.  A fresh runtime is built immediately on retry; no
        # fixed host-side settle delay is required or permitted here.

    raise RuntimeError(f"MeshCore runtime never reached healthy ({last_error})")
